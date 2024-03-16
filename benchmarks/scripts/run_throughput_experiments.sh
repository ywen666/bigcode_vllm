python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank1flora --tokenizer bigcode/starcoder
python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank2flora --tokenizer bigcode/starcoder
python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank4flora --tokenizer bigcode/starcoder
python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank8flora --tokenizer bigcode/starcoder

python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank1bmm --tokenizer bigcode/starcoder
python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank2bmm --tokenizer bigcode/starcoder
python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank4bmm --tokenizer bigcode/starcoder
python benchmark_throughput.py --dataset=ShareGPT_V3_unfiltered_cleaned_split.json --num-prompts=500 --model bigcode/starcoderrank8bmm --tokenizer bigcode/starcoder
