#!/bin/bash
#
# pipeline.sh — Automated data collection pipeline
#
# Reads repos from get_top_repos.py output, then for each repo runs:
#   1. print_pulls.py      → collect PRs
#   2. build_dataset_async.py → build task instances
#   3. get_version.py      → extract version info
#
# Usage:
#   bash pipeline.sh --language Python --top_n 100
#   bash pipeline.sh --language Python --top_n 50 --repos "python-attrs/attrs,psf/requests"
#   bash pipeline.sh --language Java --top_n 100 --skip-fetch-repos
#   bash pipeline.sh --language Python --top_n 100 --start-from 10 --end-at 20
#
set -euo pipefail

# ============================================================
# 1. Environment variables
# ============================================================
export GITHUB_TOKEN="${GITHUB_TOKEN:-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}"

export VORTEX_PROXY_HOST="${VORTEX_PROXY_HOST:-vortex-3v43ceqya3nk8fejice5r2la.vortexip.ap-southeast-1.volces.com}"
export VORTEX_PROXY_PASSWORD="${VORTEX_PROXY_PASSWORD:-Hq0aAZSPDJx1}"
export VORTEX_PROXY_HTTP_PORT="${VORTEX_PROXY_HTTP_PORT:-8080}"
export VORTEX_PROXY_HTTPS_PORT="${VORTEX_PROXY_HTTPS_PORT:-18080}"
export VORTEX_PROXY_COUNTRY="${VORTEX_PROXY_COUNTRY:-us}"
export VORTEX_PROXY_USE_SESSION="${VORTEX_PROXY_USE_SESSION:-true}"
export VORTEX_PROXY_MAX_REQUESTS_PER_IP="${VORTEX_PROXY_MAX_REQUESTS_PER_IP:-50}"

# ============================================================
# 2. Parse arguments
# ============================================================
LANGUAGE="Python"             # 目标编程语言，用于 GitHub 搜索和数据目录分类 (e.g. Python, Java, Go)
TOP_N=100                     # 从 GitHub 获取该语言 star 数最多的前 N 个仓库
SKIP_FETCH_REPOS=false        # 是否跳过 get_top_repos.py 步骤，为 true 时直接使用已有的仓库列表 JSON
REPOS=""                      # 手动指定仓库列表 (逗号分隔，如 "owner1/repo1,owner2/repo2")，非空时忽略 top_n 列表
PR_WORKERS=32                 # print_pulls.py 并发线程数，控制抓取 PR 的速度
ASYNC_CONCURRENCY=20          # build_dataset_async.py 异步并发数，控制构建 instance 的并行度
VERSION_WORKERS=20            # get_version.py 并行进程数，控制 git clone + 版本提取的并行度
TESTBED="github"              # get_version.py 的临时工作目录，用于克隆仓库和提取版本号
START_FROM=0                  # 从仓库列表的第几个开始处理 (0-indexed)，用于断点续跑
END_AT=-1                     # 处理到第几个仓库为止 (不含)，-1 表示处理到列表末尾
DATA_ROOT=""                  # 数据根目录路径，在 cd 到 collect 目录后自动设置为 "data"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --language)       LANGUAGE="$2";          shift 2 ;;
        --top_n)          TOP_N="$2";             shift 2 ;;
        --skip-fetch-repos) SKIP_FETCH_REPOS=true; shift ;;
        --repos)          REPOS="$2";             shift 2 ;;
        --pr-workers)     PR_WORKERS="$2";        shift 2 ;;
        --async-concurrency) ASYNC_CONCURRENCY="$2"; shift 2 ;;
        --version-workers) VERSION_WORKERS="$2";  shift 2 ;;
        --testbed)        TESTBED="$2";           shift 2 ;;
        --start-from)     START_FROM="$2";        shift 2 ;;
        --end-at)         END_AT="$2";            shift 2 ;;
        -h|--help)
            echo "Usage: bash pipeline.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --language LANG          Programming language (default: Python)"
            echo "  --top_n N                Number of top repos to fetch (default: 100)"
            echo "  --skip-fetch-repos       Skip fetching repos, use existing repos JSON"
            echo "  --repos REPOS            Comma-separated repos to process (e.g. 'owner/repo1,owner/repo2')"
            echo "  --pr-workers N           Workers for print_pulls.py (default: 32)"
            echo "  --async-concurrency N    Concurrency for build_dataset_async.py (default: 20)"
            echo "  --version-workers N      Workers for get_version.py (default: 20)"
            echo "  --testbed DIR            Testbed directory for get_version.py (default: github)"
            echo "  --start-from IDX         Start from repo index (0-indexed, default: 0)"
            echo "  --end-at IDX             Stop at repo index (exclusive, -1=all, default: -1)"
            echo "  -h, --help               Show this help"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ============================================================
# 3. cd to collect directory
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COLLECT_DIR="$SCRIPT_DIR/collect"
cd "$COLLECT_DIR"
DATA_ROOT="data"

LANG_LOWER=$(echo "$LANGUAGE" | tr '[:upper:]' '[:lower:]')

echo "========================================"
echo "  Data Collection Pipeline"
echo "========================================"
echo "  Language:    $LANGUAGE"
echo "  Top N:       $TOP_N"
echo "  Data root:   $COLLECT_DIR/$DATA_ROOT"
echo "  Start from:  $START_FROM"
echo "  End at:      $END_AT"
echo "========================================"

# ============================================================
# 4. Step 0: Fetch top repos (optional)
# ============================================================
REPOS_FILE="$DATA_ROOT/popular_repos/${LANG_LOWER}_top_${TOP_N}_repos.json"

if [ "$SKIP_FETCH_REPOS" = false ] && [ -z "$REPOS" ]; then
    echo ""
    echo "========== [Step 0] Fetching top $TOP_N $LANGUAGE repos =========="
    python get_top_repos.py --language "$LANGUAGE" --output_path "$DATA_ROOT/popular_repos" --top_n "$TOP_N"
    echo "[Step 0] Done. Repos saved to $REPOS_FILE"
fi

# ============================================================
# 5. Build repo list
# ============================================================
if [ -n "$REPOS" ]; then
    # User specified repos directly
    IFS=',' read -ra REPO_LIST <<< "$REPOS"
else
    if [ ! -f "$REPOS_FILE" ]; then
        echo "Error: Repos file not found: $REPOS_FILE"
        echo "Run without --skip-fetch-repos first, or specify --repos"
        exit 1
    fi
    # Read repo names from JSON array
    mapfile -t REPO_LIST < <(python3 -c "
import json, sys
with open('$REPOS_FILE') as f:
    repos = json.load(f)
for r in repos:
    print(r['name'])
")
fi

TOTAL_REPOS=${#REPO_LIST[@]}
echo ""
echo "Total repos found: $TOTAL_REPOS"

# Apply start-from / end-at slicing
if [ "$END_AT" -eq -1 ]; then
    END_AT=$TOTAL_REPOS
fi
if [ "$START_FROM" -ge "$TOTAL_REPOS" ]; then
    echo "start-from ($START_FROM) >= total repos ($TOTAL_REPOS). Nothing to do."
    exit 0
fi
if [ "$END_AT" -gt "$TOTAL_REPOS" ]; then
    END_AT=$TOTAL_REPOS
fi

echo "Processing repos [$START_FROM, $END_AT) ..."
echo ""

# ============================================================
# 6. Summary log
# ============================================================
SUMMARY_FILE="$DATA_ROOT/${LANG_LOWER}_pipeline_summary.jsonl"

# ============================================================
# 7. Main loop: process each repo
# ============================================================
PROCESSED=0
SUCCEEDED=0
FAILED=0

for (( idx=START_FROM; idx<END_AT; idx++ )); do
    REPO_FULL="${REPO_LIST[$idx]}"
    # owner/repo → owner__repo for directory naming
    REPO_SAFE=$(echo "$REPO_FULL" | tr '/' '__')
    # owner/repo → extract owner and repo separately
    REPO_OWNER=$(echo "$REPO_FULL" | cut -d'/' -f1)
    REPO_NAME=$(echo "$REPO_FULL" | cut -d'/' -f2)

    # Directory structure: data/<language>/<owner>/<repo>/
    REPO_DIR="$DATA_ROOT/$LANG_LOWER/$REPO_OWNER/$REPO_NAME"
    mkdir -p "$REPO_DIR"

    PROCESSED=$((PROCESSED + 1))
    echo "========================================================"
    echo "  [$PROCESSED / $((END_AT - START_FROM))] Processing: $REPO_FULL"
    echo "  Output dir: $REPO_DIR"
    echo "========================================================"

    PRS_FILE="$REPO_DIR/prs.jsonl"
    INSTANCES_FILE="$REPO_DIR/instances.jsonl"
    VERSIONS_FILE="$REPO_DIR/instances_versions.jsonl"
    REPO_STATUS="success"
    REPO_ERROR=""
    STEP_REACHED=0
    PR_COUNT=0
    INSTANCE_COUNT=0
    VERSION_COUNT=0

    # ----------------------------------------------------------
    # Step 1: Collect PRs
    # ----------------------------------------------------------
    echo ""
    echo "  [Step 1/3] Collecting PRs for $REPO_FULL ..."

    # Skip if final output already exists
    if [ -f "$VERSIONS_FILE" ]; then
        VERSION_COUNT=$(wc -l < "$VERSIONS_FILE")
        echo "  -> Final output already exists ($VERSION_COUNT instances). Skipping."
        SUCCEEDED=$((SUCCEEDED + 1))
        # Log to summary
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"skipped\", \"reason\": \"already_completed\", \"versions_count\": $VERSION_COUNT}" >> "$SUMMARY_FILE"
        continue
    fi

    if [ -f "$PRS_FILE" ]; then
        PR_COUNT=$(wc -l < "$PRS_FILE")
        echo "  -> PRs file already exists ($PR_COUNT PRs). Skipping step 1."
    else
        if ! python print_pulls.py "$REPO_FULL" "$PRS_FILE" --workers "$PR_WORKERS" 2>&1; then
            echo "  -> [WARN] print_pulls.py failed for $REPO_FULL"
            REPO_STATUS="failed"
            REPO_ERROR="print_pulls failed"
            FAILED=$((FAILED + 1))
            echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 1, \"error\": \"$REPO_ERROR\"}" >> "$SUMMARY_FILE"
            continue
        fi
    fi

    if [ ! -f "$PRS_FILE" ]; then
        echo "  -> [WARN] No PRs file generated. Skipping."
        REPO_STATUS="failed"
        REPO_ERROR="no prs file"
        FAILED=$((FAILED + 1))
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 1, \"error\": \"$REPO_ERROR\"}" >> "$SUMMARY_FILE"
        continue
    fi

    PR_COUNT=$(wc -l < "$PRS_FILE")
    echo "  -> Collected $PR_COUNT PRs."
    STEP_REACHED=1

    if [ "$PR_COUNT" -eq 0 ]; then
        echo "  -> No PRs found. Skipping repo."
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"skipped\", \"reason\": \"no_prs\"}" >> "$SUMMARY_FILE"
        continue
    fi

    # ----------------------------------------------------------
    # Step 2: Build dataset (async)
    # ----------------------------------------------------------
    echo ""
    echo "  [Step 2/3] Building dataset for $REPO_FULL ..."

    if [ -f "$INSTANCES_FILE" ]; then
        INSTANCE_COUNT=$(wc -l < "$INSTANCES_FILE")
        echo "  -> Instances file already exists ($INSTANCE_COUNT instances). Skipping step 2."
    else
        if ! python build_dataset_async.py \
                "$PRS_FILE" \
                "$INSTANCES_FILE" \
                --language "$LANGUAGE" \
                --max_concurrency "$ASYNC_CONCURRENCY" 2>&1; then
            echo "  -> [WARN] build_dataset_async.py failed for $REPO_FULL"
            REPO_STATUS="failed"
            REPO_ERROR="build_dataset failed"
            FAILED=$((FAILED + 1))
            echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 2, \"error\": \"$REPO_ERROR\", \"pr_count\": $PR_COUNT}" >> "$SUMMARY_FILE"
            continue
        fi
    fi

    if [ ! -f "$INSTANCES_FILE" ] || [ ! -s "$INSTANCES_FILE" ]; then
        echo "  -> No valid instances generated. Skipping."
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"skipped\", \"reason\": \"no_instances\", \"pr_count\": $PR_COUNT}" >> "$SUMMARY_FILE"
        continue
    fi

    INSTANCE_COUNT=$(wc -l < "$INSTANCES_FILE")
    echo "  -> Built $INSTANCE_COUNT instances."
    STEP_REACHED=2

    # ----------------------------------------------------------
    # Step 3: Get versions
    # ----------------------------------------------------------
    echo ""
    echo "  [Step 3/3] Extracting versions for $REPO_FULL ..."

    if [ -f "$VERSIONS_FILE" ]; then
        VERSION_COUNT=$(wc -l < "$VERSIONS_FILE")
        echo "  -> Versions file already exists ($VERSION_COUNT instances). Skipping step 3."
    else
        if ! python get_version.py \
                --instance_path "$INSTANCES_FILE" \
                --testbed "$TESTBED" \
                --max-workers "$VERSION_WORKERS" 2>&1; then
            echo "  -> [WARN] get_version.py failed for $REPO_FULL"
            REPO_STATUS="failed"
            REPO_ERROR="get_version failed"
            FAILED=$((FAILED + 1))
            echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 3, \"error\": \"$REPO_ERROR\", \"pr_count\": $PR_COUNT, \"instance_count\": $INSTANCE_COUNT}" >> "$SUMMARY_FILE"
            continue
        fi
    fi

    if [ -f "$VERSIONS_FILE" ]; then
        VERSION_COUNT=$(wc -l < "$VERSIONS_FILE")
    fi

    SUCCEEDED=$((SUCCEEDED + 1))
    echo ""
    echo "  -> Done: $REPO_FULL | PRs=$PR_COUNT | Instances=$INSTANCE_COUNT | Versioned=$VERSION_COUNT"
    echo "{\"repo\": \"$REPO_FULL\", \"status\": \"success\", \"pr_count\": $PR_COUNT, \"instance_count\": $INSTANCE_COUNT, \"version_count\": $VERSION_COUNT}" >> "$SUMMARY_FILE"

done

# ============================================================
# 8. Final summary
# ============================================================
echo ""
echo "========================================"
echo "  Pipeline Complete"
echo "========================================"
echo "  Language:    $LANGUAGE"
echo "  Processed:   $PROCESSED repos"
echo "  Succeeded:   $SUCCEEDED"
echo "  Failed:      $FAILED"
echo "  Summary log: $SUMMARY_FILE"
echo ""
echo "  Output structure:"
echo "    $DATA_ROOT/$LANG_LOWER/"
echo "      <owner>/"
echo "        <repo>/"
echo "          prs.jsonl"
echo "          instances.jsonl"
echo "          instances_versions.jsonl"
echo "========================================"
