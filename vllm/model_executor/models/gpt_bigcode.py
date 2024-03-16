# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/v4.28.0/src/transformers/models/gpt2/modeling_gpt2.py
# Copyright 2023 The vLLM team.
# Copyright 2023 CTranslate2, and Michael Feil
# Copyright 2018 The OpenAI Team Authors and HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Inference-only GPTBigCode model compatible with HuggingFace weights.

The input of the model is flattened to a 1D tensor of tokens. The model uses
InputMetadata to extract the original 2D shape of the input.
"""
from typing import List, Optional, Tuple

import torch
from torch import nn
from transformers import GPTBigCodeConfig

from vllm.model_executor.input_metadata import InputMetadata
from vllm.model_executor.layers.activation import get_act_fn
from vllm.model_executor.layers.attention import PagedAttention
from vllm.model_executor.layers.sampler import Sampler
from vllm.model_executor.weight_utils import (
    convert_pyslice_to_tensor, hf_model_weights_iterator,
    load_padded_tensor_parallel_vocab, load_tensor_parallel_weights)
from vllm.model_executor.parallel_utils.parallel_state import (
    get_tensor_model_parallel_rank, get_tensor_model_parallel_world_size)
from vllm.model_executor.parallel_utils.tensor_parallel import (
    VocabParallelEmbedding, ColumnParallelLinear, RowParallelLinear)
from vllm.sequence import SamplerOutput

KVCache = Tuple[torch.Tensor, torch.Tensor]


class GPTBigCodeAttention(nn.Module):

    def __init__(self, config: GPTBigCodeConfig):
        super().__init__()
        self.hidden_size = config.hidden_size
        total_num_heads = config.num_attention_heads
        self.tensor_model_parallel_world_size = (
            get_tensor_model_parallel_world_size())
        assert total_num_heads % self.tensor_model_parallel_world_size == 0
        self.num_heads = (total_num_heads //
                          self.tensor_model_parallel_world_size)
        self.head_dim = self.hidden_size // total_num_heads
        self.scale = self.head_dim**-0.5

        self.multi_query = config.multi_query
        if self.multi_query:
            self.num_kv_heads = 1
            self.kv_dim = self.head_dim
            self.c_attn_q = ColumnParallelLinear(self.hidden_size,
                                                 self.hidden_size,
                                                 bias=True,
                                                 gather_output=False,
                                                 perform_initialization=False)
            self.c_attn_kv = nn.Linear(self.hidden_size,
                                       2 * self.kv_dim,
                                       bias=True)
        else:
            self.num_kv_heads = self.num_heads
            self.kv_dim = self.num_kv_heads * self.head_dim
            self.c_attn = ColumnParallelLinear(self.hidden_size,
                                               self.hidden_size +
                                               2 * self.kv_dim,
                                               bias=True,
                                               gather_output=False,
                                               perform_initialization=False)

        self.c_proj = RowParallelLinear(self.hidden_size,
                                        self.hidden_size,
                                        bias=True,
                                        input_is_parallel=True,
                                        perform_initialization=False)
        self.attn = PagedAttention(self.num_heads,
                                   self.head_dim,
                                   scale=self.scale,
                                   num_kv_heads=self.num_kv_heads)
        self.mode = ""

    def forward(
        self,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:

        if self.mode == "bmm":
            if input_metadata.num_prompts > 0:
                padded_length = hidden_states.size(0) - input_metadata.num_valid_tokens
                sequence_lengths = torch.LongTensor(input_metadata.prompt_lens)
                sequence_lengths[-1] = sequence_lengths[-1] + padded_length
                max_seq_length = sequence_lengths.max().item()
                batch_size = input_metadata.num_prompts
            else:
                max_seq_length = 1
                batch_size = hidden_states.size(0)
                sequence_lengths = torch.LongTensor(batch_size * [1])
            hidden_size = hidden_states.size(1)

            # Generate the mask from sequence_lengths
            range_tensor = torch.arange(max_seq_length).unsqueeze(0).expand(batch_size, -1)
            mask = range_tensor < sequence_lengths.unsqueeze(1)

            # Calculate positions where data will be placed in output_tensor
            row_indices = mask.cumsum(dim=1) - 1
            cumulative_lengths = torch.cat([torch.tensor([0]), sequence_lengths.cumsum(dim=0)[:-1]])
            start_positions = cumulative_lengths.unsqueeze(1).expand(batch_size, max_seq_length)
            adjusted_row_indices = row_indices + start_positions

            # Flatten the output tensor and use adjusted_row_indices to place the values from result tensor
            inputs = torch.zeros(
                batch_size * max_seq_length, hidden_size, dtype=torch.float16).to(hidden_states.device)
            inputs[adjusted_row_indices[mask]] = hidden_states

            # Reshape the tensor back to [batch_size, max_seq_length, hidden_size]
            inputs = inputs.view(batch_size, max_seq_length, hidden_size)

            A = self.A.unsqueeze(0).expand(batch_size, -1, -1)
            B = self.B.unsqueeze(0).expand(batch_size, -1, -1)
            C = self.C.unsqueeze(0).expand(batch_size, -1, -1)
            D = self.D.unsqueeze(0).expand(batch_size, -1, -1)
            adapters_hidden = torch.bmm(inputs, A)
            adapters_out = torch.bmm(adapters_hidden, B)
            adapters_hidden = torch.bmm(adapters_out, C)
            adapters_out = torch.bmm(adapters_hidden, D)

        if self.multi_query:
            if self.mode == "flora":
                if len(hidden_states.size()) != 2:
                    raise ValueError("Number of dimensions of inputs are not 2")
                rank = self.rank

                indices = input_metadata.indices
                indices_expanded = indices.unsqueeze(0).unsqueeze(-1).expand(
                    rank, -1, hidden_states.shape[-1])
                A = torch.gather(self.A, 1, indices_expanded)
                C = torch.gather(self.C, 1, indices_expanded)
                plora1 = A * hidden_states
                plora2 = C * hidden_states
                hidden_states = hidden_states.unsqueeze(0).expand([rank, -1, -1])
                q, _ = self.c_attn_q(hidden_states)
                q = q[0]
                kv = self.c_attn_kv(hidden_states)
                kv = kv[0]
                k, v = kv.split([self.kv_dim, self.kv_dim], dim=-1)
                indices_expanded2 = indices.unsqueeze(0).unsqueeze(-1).expand(
                    rank, -1, self.D.shape[-1])
                B = torch.gather(self.B, 1, indices_expanded)
                D = torch.gather(self.D, 1, indices_expanded2)
                plora1 = torch.mean(B * q, dim=0)
                plora2 = torch.mean(D * kv, dim=0)
                hidden_states = hidden_states[0]
            else:
                q, _ = self.c_attn_q(hidden_states)
                kv = self.c_attn_kv(hidden_states)
                k, v = kv.split([self.kv_dim, self.kv_dim], dim=-1)
        else:
            if self.mode == "flora":
                raise NotImplementedError("flora not implemented in non multi query mode")

            qkv, _ = self.c_attn(hidden_states)
            q, k, v = qkv.split([
                self.hidden_size // self.tensor_model_parallel_world_size,
                self.kv_dim, self.kv_dim
            ],
                                dim=-1)
        key_cache, value_cache = kv_cache
        attn_output = self.attn(q, k, v, key_cache, value_cache,
                                input_metadata, cache_event)

        if self.mode == "flora":
            indices_expanded = indices.unsqueeze(0).unsqueeze(-1).expand(
                rank, -1, attn_output.shape[-1])
            E = torch.gather(self.E, 1, indices_expanded)
            F = torch.gather(self.F, 1, indices_expanded)
            plora1 = E * attn_output
            attn_output = attn_output.unsqueeze(0).expand([rank, -1, -1])
            attn_output, _ = self.c_proj(attn_output)
            plora2 = torch.mean(F * attn_output, dim=0)
            attn_output = attn_output[0]
        else:
            if self.mode == "bmm":
                inputs = torch.zeros(
                    batch_size * max_seq_length, attn_output.size(-1),
                    dtype=torch.float16).to(attn_output.device)
                inputs[adjusted_row_indices[mask]] = attn_output

                # Reshape the tensor back to [batch_size, max_seq_length, hidden_size]
                inputs = inputs.view(batch_size, max_seq_length, attn_output.size(-1))

                E = self.E.unsqueeze(0).expand(batch_size, -1, -1)
                F = self.F.unsqueeze(0).expand(batch_size, -1, -1)
                adapters_hidden = torch.bmm(inputs, E)
                adapters_out = torch.bmm(adapters_hidden, F)

            attn_output, _ = self.c_proj(attn_output)
        return attn_output

    def set_mode(self, mode, rank):
        self.mode = mode
        self.rank = rank
        if mode == "bmm":
            self.A = torch.nn.Parameter(
                torch.randn([self.hidden_size, rank]))
            self.B = torch.nn.Parameter(
                torch.randn([rank, self.hidden_size]))
            self.C = torch.nn.Parameter(
                torch.randn([self.hidden_size, rank]))
            self.D = torch.nn.Parameter(
                torch.randn([rank, 2 * self.kv_dim]))
            self.E = torch.nn.Parameter(
                torch.randn([self.hidden_size, rank]))
            self.F = torch.nn.Parameter(
                torch.randn([rank, self.hidden_size]))
        elif mode == "flora":
            self.A = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))
            self.B = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))
            self.C = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))
            self.D = torch.nn.Parameter(
                torch.randn([rank, 10, 2 * self.kv_dim]))
            self.E = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))
            self.F = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))

    def unset_mode(self):
        self.mode = ""

class GPTBigMLP(nn.Module):

    def __init__(
        self,
        intermediate_size: int,
        config: GPTBigCodeConfig,
    ):
        super().__init__()
        hidden_size = config.hidden_size
        self.c_fc = ColumnParallelLinear(hidden_size,
                                         intermediate_size,
                                         bias=True,
                                         gather_output=False,
                                         perform_initialization=False)
        self.c_proj = RowParallelLinear(intermediate_size,
                                        hidden_size,
                                        bias=True,
                                        input_is_parallel=True,
                                        perform_initialization=False)
        self.act = get_act_fn(config.activation_function)
        self.mode = ""
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size

    def forward(self, hidden_states: torch.Tensor, input_metadata: Optional[InputMetadata]) -> torch.Tensor:
        if self.mode == "bmm":
            if input_metadata.num_prompts > 0:
                padded_length = hidden_states.size(0) - input_metadata.num_valid_tokens
                sequence_lengths = torch.LongTensor(input_metadata.prompt_lens)
                sequence_lengths[-1] = sequence_lengths[-1] + padded_length
                max_seq_length = sequence_lengths.max().item()
                batch_size = input_metadata.num_prompts
            else:
                max_seq_length = 1
                batch_size = hidden_states.size(0)
                sequence_lengths = torch.LongTensor(batch_size * [1])
            hidden_size = hidden_states.size(1)

            # Generate the mask from sequence_lengths
            range_tensor = torch.arange(max_seq_length).unsqueeze(0).expand(batch_size, -1)
            mask = range_tensor < sequence_lengths.unsqueeze(1)

            # Calculate positions where data will be placed in output_tensor
            row_indices = mask.cumsum(dim=1) - 1
            cumulative_lengths = torch.cat([torch.tensor([0]), sequence_lengths.cumsum(dim=0)[:-1]])
            start_positions = cumulative_lengths.unsqueeze(1).expand(batch_size, max_seq_length)
            adjusted_row_indices = row_indices + start_positions

            # Flatten the output tensor and use adjusted_row_indices to place the values from result tensor
            inputs = torch.zeros(
                batch_size * max_seq_length, hidden_size,
                dtype=torch.float16).to(hidden_states.device)
           
            inputs[adjusted_row_indices[mask]] = hidden_states

            # Reshape the tensor back to [batch_size, max_seq_length, hidden_size]
            inputs = inputs.view(batch_size, max_seq_length, hidden_size)

            A = self.A.unsqueeze(0).expand(batch_size, -1, -1)
            B = self.B.unsqueeze(0).expand(batch_size, -1, -1)
            adapters_hidden = torch.bmm(inputs, A)
            adapters_out = torch.bmm(adapters_hidden, B)

        if self.mode == "flora":
            if len(hidden_states.size()) != 2:
                raise ValueError("Number of dimensions of inputs are not 2")
            rank = self.rank
            length = hidden_states.shape[0]
            indices = input_metadata.indices
            indices_expanded = indices.unsqueeze(0).unsqueeze(-1).expand(
                rank, -1, self.A.shape[-1])
            A = torch.gather(self.A, 1, indices_expanded)
            plora1 = A * hidden_states
            hidden_states = hidden_states.unsqueeze(0).expand([rank, -1, -1])
            hidden_states, _ = self.c_fc(hidden_states)
            hidden_states = hidden_states[0]

            indices_expanded = indices.unsqueeze(0).unsqueeze(-1).expand(
                rank, -1, self.B.shape[-1])
            B = torch.gather(self.B, 1, indices_expanded)
            plora1 = torch.mean(B * hidden_states, dim=0)
            hidden_states = self.act(hidden_states)

            indices_expanded = indices.unsqueeze(0).unsqueeze(-1).expand(
                rank, -1, self.C.shape[-1])
            C = torch.gather(self.C, 1, indices_expanded)
            plora2 = C * hidden_states
            hidden_states = hidden_states.unsqueeze(0).expand([rank, -1, -1])
            hidden_states, _ = self.c_proj(hidden_states)
            hidden_states = hidden_states[0]

            indices_expanded = indices.unsqueeze(0).unsqueeze(-1).expand(
                rank, -1, self.D.shape[-1])
            D = torch.gather(self.D, 1, indices_expanded)
            plora2 = torch.mean(D * hidden_states, dim=0)
        else:
            hidden_states, _ = self.c_fc(hidden_states)
            hidden_states = self.act(hidden_states)

            if self.mode == "bmm":
                inputs = torch.zeros(
                    batch_size * max_seq_length, hidden_states.size(-1),
                    dtype=torch.float16).to(hidden_states.device)
                inputs[adjusted_row_indices[mask]] = hidden_states

                # Reshape the tensor back to [batch_size, max_seq_length, hidden_size]
                inputs = inputs.view(batch_size, max_seq_length, hidden_states.size(-1))

                C = self.C.unsqueeze(0).expand(batch_size, -1, -1)
                D = self.D.unsqueeze(0).expand(batch_size, -1, -1)
                adapters_hidden = torch.bmm(inputs, C)
                adapters_out = torch.bmm(adapters_hidden, D)

            hidden_states, _ = self.c_proj(hidden_states)
        return hidden_states

    def set_mode(self, mode, rank):
        self.mode = mode
        self.rank = rank
        if mode == "bmm":
            self.A = torch.nn.Parameter(
                torch.randn([self.hidden_size, rank]))
            self.B = torch.nn.Parameter(
                torch.randn([rank, self.intermediate_size]))
            self.C = torch.nn.Parameter(
                torch.randn([self.intermediate_size, rank]))
            self.D = torch.nn.Parameter(
                torch.randn([rank, self.hidden_size]))
        elif mode == "flora":
            self.A = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))
            self.B = torch.nn.Parameter(
                torch.randn([rank, 10, self.intermediate_size]))
            self.C = torch.nn.Parameter(
                torch.randn([rank, 10, self.intermediate_size]))
            self.D = torch.nn.Parameter(
                torch.randn([rank, 10, self.hidden_size]))

    def unset_mode(self):
        self.mode = ""


class GPTBigCodeBlock(nn.Module):

    def __init__(self, config: GPTBigCodeConfig):
        super().__init__()
        hidden_size = config.hidden_size
        inner_dim = (config.n_inner if config.n_inner is not None else 4 *
                     hidden_size)

        self.ln_1 = nn.LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        self.attn = GPTBigCodeAttention(config)
        self.ln_2 = nn.LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        self.mlp = GPTBigMLP(inner_dim, config)

    def forward(
        self,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:
        residual = hidden_states
        hidden_states = self.ln_1(hidden_states)
        attn_output = self.attn(
            hidden_states=hidden_states,
            kv_cache=kv_cache,
            input_metadata=input_metadata,
            cache_event=cache_event,
        )
        # residual connection
        hidden_states = attn_output + residual

        residual = hidden_states
        hidden_states = self.ln_2(hidden_states)
        feed_forward_hidden_states = self.mlp(hidden_states, input_metadata)
        # residual connection
        hidden_states = residual + feed_forward_hidden_states
        return hidden_states


class GPTBigCodeModel(nn.Module):

    def __init__(self, config: GPTBigCodeConfig):
        super().__init__()
        self.config = config
        assert not config.add_cross_attention

        self.embed_dim = config.hidden_size

        # Optimization: While the vocab size of GPT-2 is 50257, we extend it
        # to 50304 in order to make it divisible by 64.
        # This improves performance since GPUs are faster if the dimension
        # is divisible by 64. In addition, it allows us to shard the embedding
        # layer across 2, 4, 8, or more GPUs.
        vocab_size = ((config.vocab_size + 63) // 64) * 64
        self.wte = VocabParallelEmbedding(vocab_size, self.embed_dim)
        self.wpe = nn.Embedding(config.max_position_embeddings, self.embed_dim)
        self.h = nn.ModuleList(
            [GPTBigCodeBlock(config) for _ in range(config.num_hidden_layers)])
        self.ln_f = nn.LayerNorm(self.embed_dim, eps=config.layer_norm_epsilon)

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> torch.Tensor:
        inputs_embeds = self.wte(input_ids)
        position_embeds = self.wpe(position_ids)
        hidden_states = inputs_embeds + position_embeds

        for i in range(len(self.h)):
            if cache_events is None:
                cache_event = None
            else:
                cache_event = cache_events[i]
            layer = self.h[i]
            hidden_states = layer(hidden_states, kv_caches[i], input_metadata,
                                  cache_event)

        hidden_states = self.ln_f(hidden_states)
        return hidden_states


class GPTBigCodeForCausalLM(nn.Module):

    def __init__(self, config: GPTBigCodeConfig):
        super().__init__()
        self.config = config
        self.transformer = GPTBigCodeModel(config)
        # TODO(zhuohan): create a new weight after implementing pipeline
        #                parallelism
        self.lm_head_weight = self.transformer.wte.weight
        self.sampler = Sampler(config.vocab_size)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> SamplerOutput:
        hidden_states = self.transformer(input_ids, positions, kv_caches,
                                         input_metadata, cache_events)
        next_tokens = self.sampler(self.lm_head_weight, hidden_states,
                                   input_metadata)
        return next_tokens

    _column_parallel_weights = ["c_fc.weight", "c_fc.bias"]
    _row_parallel_weights = ["c_proj.weight"]

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        tensor_model_parallel_world_size = (
            get_tensor_model_parallel_world_size())
        tensor_model_parallel_rank = get_tensor_model_parallel_rank()
        state_dict = self.state_dict()

        for name, loaded_weight in hf_model_weights_iterator(
                model_name_or_path, cache_dir, load_format, revision):
            if "lm_head.weight" in name:
                # GPT-2 ties the weights of the embedding layer and the final
                # linear layer.
                continue
            if ".attn.bias" in name:
                # Skip attention mask.
                # NOTE: "c_attn.bias" should not be skipped.
                continue

            if not name.startswith("transformer."):
                name = "transformer." + name

            # For the fused QKV linear layer, manually shard the weights.
            if "c_attn" in name:
                # GPT-2's fused QKV has the shape of
                # [3 * num_heads * head_size, hidden_size].
                # When tensor parallelism is used, we shard the weights along
                # the head dimension.
                total_num_heads = self.config.num_attention_heads
                total_num_kv_heads = (1 if self.config.multi_query else
                                      total_num_heads)
                hidden_size = self.config.hidden_size
                head_size = hidden_size // total_num_heads
                total_kv_size = head_size * total_num_kv_heads
                num_heads = total_num_heads // tensor_model_parallel_world_size
                head_start = tensor_model_parallel_rank * num_heads
                head_end = (tensor_model_parallel_rank + 1) * num_heads

                loaded_weight = convert_pyslice_to_tensor(loaded_weight)
                wq, wk, wv = torch.split(
                    loaded_weight, [hidden_size, total_kv_size, total_kv_size],
                    dim=0)

                wq = wq[head_size * head_start:head_size * head_end]
                if not self.config.multi_query:
                    # Split the heads when using normal multi-head attention
                    wk = wk[head_size * head_start:head_size * head_end]
                    wv = wv[head_size * head_start:head_size * head_end]
                    loaded_weight = torch.cat([wq, wk, wv], dim=0)
                else:
                    # For multi-query attention, we split the query
                    # but replicate the key and value.
                    loaded_weight_q = wq
                    loaded_weight_kv = torch.cat([wk, wv], dim=0)
                    q_weight_name = name.replace("c_attn", "c_attn_q")
                    kv_weight_name = name.replace("c_attn", "c_attn_kv")
                    load_tensor_parallel_weights(state_dict[q_weight_name],
                                                 loaded_weight_q,
                                                 q_weight_name,
                                                 self._column_parallel_weights,
                                                 self._row_parallel_weights,
                                                 tensor_model_parallel_rank)
                    load_tensor_parallel_weights(state_dict[kv_weight_name],
                                                 loaded_weight_kv,
                                                 kv_weight_name,
                                                 self._column_parallel_weights,
                                                 self._row_parallel_weights,
                                                 tensor_model_parallel_rank)
                    continue

            param = state_dict[name]

            if name == "transformer.wte.weight":
                load_padded_tensor_parallel_vocab(param, loaded_weight,
                                                  tensor_model_parallel_rank)
                continue

            load_tensor_parallel_weights(param, loaded_weight, name,
                                         self._column_parallel_weights,
                                         self._row_parallel_weights,
                                         tensor_model_parallel_rank)


class GPTBigCodeForCausalLMPeft(GPTBigCodeForCausalLM):

    def __init__(self, mode, rank, config: GPTBigCodeConfig):
        super().__init__(config)
        print(f"Initialize the model with rank {rank} with mode {mode}")
        self.mode = mode
        self.rank = rank
        for layer in self.transformer.h:
            layer.attn.set_mode(mode, rank)
            layer.mlp.set_mode(mode, rank)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        cache_events: Optional[List[torch.cuda.Event]],
    ) -> SamplerOutput:
        if self.mode == "flora":
            indices = torch.randint(0, 10, [input_ids.shape[0]], device=input_ids.device)
            input_metadata.indices = indices
        hidden_states = self.transformer(input_ids, positions, kv_caches,
                                         input_metadata, cache_events)
        next_tokens = self.sampler(self.lm_head_weight, hidden_states,
                                   input_metadata)
        return next_tokens