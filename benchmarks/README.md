### Downloading the ShareGPT dataset

```bash
wget https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json
```

### Throughput experiments

Before starting, ensure you have the necessary environment and dependencies set up. Adjust the `rank` and `mode` variables in the script below to experiment with different configurations. You can also modify `max_num_seqs` and `max_num_batched_tokens` as needed to fit your experimental design.

```bash
rank=1
mode=flora
python benchmark_throughput.py \
    --dataset=ShareGPT_V3_unfiltered_cleaned_split.json \
    --num-prompts=1000 \
    --model bigcode/starcoderrank${rank}${mode} \
    --tokenizer bigcode/starcoder
```
