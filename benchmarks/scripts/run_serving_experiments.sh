requestrate=8
for requestrate in {8,10}
do
    python benchmark_serving.py \
        --port 8000 \
        --dataset ShareGPT_V3_unfiltered_cleaned_split.json \
        --tokenizer bigcode/starcoder \
        --num-prompts 250 \
        --request-rate ${requestrate} \
        --model $1
done
