## vLLM package used for the FLoRA serving experiements.

The experiments reported in the paper used vLLM (0.1.3), which exhibits reduced performance with the recent PyTorch and CUDA versions (torch2.0.1+cu118). Therefore, this repo uses vLLM (0.2.0) to maintain performance closed with the paper on torch2.0.1+cu118.

Editable install the vLLM 
```bash
pip install -e .
```

See instructions under `benchmarks/` for how to initailize serving experiments.
