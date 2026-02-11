export OPENAI_API_BASE_URL=https://api.siliconflow.cn/v1
export OPENAI_API_BASE=https://api.siliconflow.cn/v1  # LiteLLM 需要这个环境变量
export OPENAI_KEY=sk-nrfkwelacfabbaikvxqflzztslbdrpmeduhzxqnzmisaulgy
export OPENAI_API_KEY=sk-nrfkwelacfabbaikvxqflzztslbdrpmeduhzxqnzmisaulgy  # LiteLLM 需要这个环境变量

cd "$(dirname "$0")/.."  # 切换到项目根目录
export PYTHONPATH="$(pwd):$PYTHONPATH"

python app/main.py swe-bench \
    --model litellm-generic-openai/Pro/MiniMaxAI/MiniMax-M2.1 \
    --tasks-map "data_collection/collect/data/python-attrs/attrs/instances_async_versions.jsonl" \
    --num-processes 10 \
    --model-temperature 0.2 \
    --conv-round-limit 10 \
    --output-dir "output/minimax/python-attrs" \
    --setup-dir "testbed" \
    --results-path "output/minimax/python-attrs/results"
