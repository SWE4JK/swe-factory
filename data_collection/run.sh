# GitHub Token — 仅 get_top_repos.py 使用; 其他步骤走 anonymous + proxy 轮转
GITHUB_TOKEN_FOR_SEARCH="${GITHUB_TOKEN:-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}"

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

# Step 0: 获取热门仓库 (使用 token)
GITHUB_TOKEN="$GITHUB_TOKEN_FOR_SEARCH" python get_top_repos.py --language $LANGUAGE --output_path data/popular_repos --top_n 100

# 后续步骤不使用 token
export GITHUB_TOKEN=""

# Step 1: 获取 PRs
python print_pulls.py python-attrs/attrs data/python-attrs/attrs/prs.jsonl --workers 32

# Step 2: 构建数据集
python build_dataset_async.py \
    data/python-attrs/attrs/prs.jsonl \
    data/python-attrs/attrs/instances_async.jsonl \
    --language $LANGUAGE \
    --max_concurrency 20

# Step 3: 提取版本信息
python get_version.py --instance_path data/python-attrs/attrs/instances_async.jsonl --testbed github --max-workers 20
