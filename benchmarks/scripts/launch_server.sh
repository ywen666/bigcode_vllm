cd ..
python -m vllm.entrypoints.api_server \
    --model $1 --tokenizer bigcode/starcoder --swap-space 16 \
    --disable-log-requests
