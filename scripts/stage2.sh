# export OPENAI_API_BASE_URL=https://api.siliconflow.cn/v1
# export OPENAI_API_BASE=https://api.siliconflow.cn/v1  # LiteLLM 需要这个环境变量
export OPENAI_API_BASE=https://siflow-longmen.siflow.cn/siflow/longmen/skyinfer/skliu/minimax-litellm/v1/4000
export OPENAI_API_BASE_URL=$OPENAI_API_BASE  # app 内部读这个变量名
# export OPENAI_KEY=sk-nrfkwelacfabbaikvxqflzztslbdrpmeduhzxqnzmisaulgy
# export OPENAI_API_KEY=sk-nrfkwelacfabbaikvxqflzztslbdrpmeduhzxqnzmisaulgy  # LiteLLM 需要这个环境变量
export OPENAI_API_KEY=EMPTY

# cd "$(dirname "$0")/.."  # 切换到项目根目录
export PYTHONPATH="$(pwd):$PYTHONPATH"

python app/main.py swe-bench \
    --model litellm-generic-openai/minimax-m2.1 \
    --tasks-map "data_collection/collect/data/python/OpenHands/OpenHands/instances_versions.jsonl" \
    --num-processes 10 \
    --model-temperature 0.2 \
    --conv-round-limit 10 \
    --output-dir "output/python/OpenHands-OpenHands" \
    --setup-dir "testbed" \
    --results-path "output/python/OpenHands-OpenHands/results"
