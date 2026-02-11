# GitHub Token (optional when proxy is enabled — anonymous + proxy rotation is sufficient)
export GITHUB_TOKEN=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

# Vortex Proxy Configuration (enables IP rotation to bypass GitHub rate limits)
export VORTEX_PROXY_HOST=vortex-3v43ceqya3nk8fejice5r2la.vortexip.ap-southeast-1.volces.com
export VORTEX_PROXY_PASSWORD=Hq0aAZSPDJx1
export VORTEX_PROXY_HTTP_PORT=8080
export VORTEX_PROXY_HTTPS_PORT=18080
export VORTEX_PROXY_COUNTRY=us
export VORTEX_PROXY_USE_SESSION=true
export VORTEX_PROXY_MAX_REQUESTS_PER_IP=50

export LANGUAGE=Python
cd collect

python get_top_repos.py --language $LANGUAGE --output_path data/popular_repos --top_n 100

# todo: 从指定仓库 -> 读取上一步生成的 popular_repos.jsonl 文件 -> 获取 top_n 个仓库 -> 获取 pull requests -> 保存到指定文件
python print_pulls.py python-attrs/attrs data/python-attrs/attrs/prs.jsonl --workers 32
# python print_pulls.py python-attrs/attrs data/python-attrs/attrs/prs.jsonl

python build_dataset_async.py \
    data/python-attrs/attrs/prs.jsonl \
    data/python-attrs/attrs/instances_async.jsonl \
    --language $LANGUAGE \
    --max_concurrency 20

python get_version.py --instance_path data/python-attrs/attrs/instances_async.jsonl --testbed github --max-workers 20
