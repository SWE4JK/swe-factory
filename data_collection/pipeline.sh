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
# get_top_repos.py 使用 token (搜索 API 限额更高)
# print_pulls / build_dataset / get_version 不使用 token (走 proxy 轮转)
GITHUB_TOKEN_FOR_SEARCH="${GITHUB_TOKEN:-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}"

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
SKIP_FETCH_REPOS=true        # 是否跳过 get_top_repos.py 步骤，为 true 时直接使用已有的仓库列表 JSON
REPOS=""                      # 手动指定仓库列表 (逗号分隔，如 "owner1/repo1,owner2/repo2")，非空时忽略 top_n 列表
PR_WORKERS=32                 # print_pulls.py 并发线程数，控制抓取 PR 的速度
ASYNC_CONCURRENCY=20          # build_dataset_async.py 异步并发数，控制构建 instance 的并行度
VERSION_WORKERS=20            # get_version.py 并行进程数，控制 git clone + 版本提取的并行度
TESTBED="github"              # get_version.py 的临时工作目录，用于克隆仓库和提取版本号
START_FROM=0                  # 从仓库列表的第几个开始处理 (0-indexed)，用于断点续跑
END_AT=-1                     # 处理到第几个仓库为止 (不含)，-1 表示处理到列表末尾
DATA_ROOT=""                  # 数据根目录路径，在 cd 到 collect 目录后自动设置为 "data"
STEP_TIMEOUT=3600             # 每一步的超时时间 (秒)，默认 60 分钟，超时自动跳过该仓库

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
        --step-timeout)   STEP_TIMEOUT="$2";      shift 2 ;;
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

# ============================================================
# Helper: progress bar and formatting
# ============================================================
BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
CYAN='\033[36m'
BLUE='\033[34m'
RESET='\033[0m'

PIPELINE_START_TIME=$(date +%s)

format_duration() {
    local secs=$1
    if [ "$secs" -lt 60 ]; then
        echo "${secs}s"
    elif [ "$secs" -lt 3600 ]; then
        echo "$((secs / 60))m$((secs % 60))s"
    else
        echo "$((secs / 3600))h$(( (secs % 3600) / 60 ))m"
    fi
}

# draw_progress_bar <current> <total> <width>
draw_progress_bar() {
    local current=$1 total=$2 width=${3:-40}
    local pct=0
    if [ "$total" -gt 0 ]; then
        pct=$(( current * 100 / total ))
    fi
    local filled=$(( current * width / total ))
    local empty=$(( width - filled ))
    local bar=""
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty;  i++)); do bar+="░"; done
    echo -ne "  ${CYAN}${bar}${RESET} ${BOLD}${pct}%${RESET} (${current}/${total})"
}

# print_status_line — 实时状态行 (success / failed / skipped)
print_status_line() {
    local elapsed=$(( $(date +%s) - PIPELINE_START_TIME ))
    echo -e "  ${GREEN}success=$SUCCEEDED${RESET}  ${RED}failed=$FAILED${RESET}  ${YELLOW}skipped=$SKIPPED${RESET}  ${DIM}elapsed=$(format_duration $elapsed)${RESET}"
}

# print_step — 打印步骤名称带时间戳
print_step() {
    local step_label=$1 repo=$2
    echo -e "  ${DIM}$(date +%H:%M:%S)${RESET} ${BLUE}[$step_label]${RESET} $repo"
}

echo ""
echo -e "${BOLD}========================================${RESET}"
echo -e "${BOLD}  Data Collection Pipeline${RESET}"
echo -e "${BOLD}========================================${RESET}"
echo -e "  Language:    ${CYAN}$LANGUAGE${RESET}"
echo -e "  Top N:       $TOP_N"
echo -e "  Data root:   $COLLECT_DIR/$DATA_ROOT"
echo -e "  Start from:  $START_FROM"
echo -e "  End at:      $END_AT"
echo -e "${BOLD}========================================${RESET}"

# ============================================================
# 4. Step 0: Fetch top repos (optional)
# ============================================================
REPOS_FILE="$DATA_ROOT/popular_repos/${LANG_LOWER}_top_${TOP_N}_repos.json"

if [ "$SKIP_FETCH_REPOS" = false ] && [ -z "$REPOS" ]; then
    echo ""
    echo -e "${BOLD}[Step 0]${RESET} Fetching top $TOP_N $LANGUAGE repos ..."
    GITHUB_TOKEN="$GITHUB_TOKEN_FOR_SEARCH" python get_top_repos.py --language "$LANGUAGE" --output_path "$DATA_ROOT/popular_repos" --top_n "$TOP_N"
    echo -e "  ${GREEN}Done.${RESET} Repos saved to $REPOS_FILE"
fi

# ============================================================
# 5. Build repo list (with smart filtering)
# ============================================================
if [ -n "$REPOS" ]; then
    IFS=',' read -ra REPO_LIST <<< "$REPOS"
else
    if [ ! -f "$REPOS_FILE" ]; then
        echo -e "${RED}Error: Repos file not found: $REPOS_FILE${RESET}"
        echo "Run without --skip-fetch-repos first, or specify --repos"
        exit 1
    fi
    # Read repos, filter out non-code repos (awesome-lists, tutorials, etc)
    mapfile -t REPO_LIST < <(python3 -c "
import json, re, sys

# 精确白名单: 这些仓库即使名字命中 pattern 也保留
WHITELIST = {'scikit-learn/scikit-learn'}

# 仓库名中包含这些关键词的大概率不是代码库，跳过 (只匹配仓库名，不匹配描述)
SKIP_NAME_PATTERNS = [
    r'\bawesome\b',         # awesome-python, awesome-xxx
    r'free.*book',          # free-programming-books
    r'public.api',          # public-apis
    r'\binterview\b',       # coding-interview
    r'\btutorial\b',
    r'\bcheatsheet\b',
    r'\b\d+.days?\b',       # 30-Days-Of-Python, 100-days-of-code
    r'\bguide\b',
    r'\bresource\b',
    r'\bexample[s]?\b',
    r'\bsample[s]?\b',
    r'\bexercise\b',
]

with open('$REPOS_FILE') as f:
    repos = json.load(f)

skipped = []
for r in repos:
    name_lower = r['name'].lower()

    skip = False
    if r['name'] not in WHITELIST:
        for pat in SKIP_NAME_PATTERNS:
            if re.search(pat, name_lower):
                skip = True
                break

    if skip:
        skipped.append(r['name'])
    else:
        print(r['name'])

if skipped:
    print(f'[filter] Skipped {len(skipped)} non-code repos: {skipped[:5]}...', file=sys.stderr)
")
fi

TOTAL_REPOS=${#REPO_LIST[@]}

# 后续步骤不使用 token，走 anonymous + proxy 轮转
export GITHUB_TOKEN=""

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

NUM_TO_PROCESS=$((END_AT - START_FROM))
echo ""
echo -e "  Total repos: ${BOLD}$TOTAL_REPOS${RESET} | Processing: ${BOLD}[$START_FROM, $END_AT)${RESET} = ${BOLD}$NUM_TO_PROCESS${RESET} repos"
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
SKIPPED=0
TOTAL_INSTANCES=0

for (( idx=START_FROM; idx<END_AT; idx++ )); do
    REPO_FULL="${REPO_LIST[$idx]}"
    REPO_OWNER=$(echo "$REPO_FULL" | cut -d'/' -f1)
    REPO_NAME=$(echo "$REPO_FULL" | cut -d'/' -f2)

    # Directory structure: data/<language>/<owner>/<repo>/
    REPO_DIR="$DATA_ROOT/$LANG_LOWER/$REPO_OWNER/$REPO_NAME"
    mkdir -p "$REPO_DIR"

    PROCESSED=$((PROCESSED + 1))
    REPO_START_TIME=$(date +%s)

    # ---- Progress bar ----
    echo ""
    draw_progress_bar "$PROCESSED" "$NUM_TO_PROCESS"
    echo ""
    print_status_line
    echo -e "  ${BOLD}>>> [$PROCESSED/$NUM_TO_PROCESS] $REPO_FULL${RESET}  ${DIM}-> $REPO_DIR${RESET}"

    PRS_FILE="$REPO_DIR/prs.jsonl"
    INSTANCES_FILE="$REPO_DIR/instances.jsonl"
    VERSIONS_FILE="$REPO_DIR/instances_versions.jsonl"
    PR_COUNT=0
    INSTANCE_COUNT=0
    VERSION_COUNT=0

    # ----------------------------------------------------------
    # Skip if final output already exists
    # ----------------------------------------------------------
    if [ -f "$VERSIONS_FILE" ]; then
        VERSION_COUNT=$(wc -l < "$VERSIONS_FILE")
        TOTAL_INSTANCES=$((TOTAL_INSTANCES + VERSION_COUNT))
        SUCCEEDED=$((SUCCEEDED + 1))
        echo -e "  ${GREEN}SKIP${RESET} Final output already exists (${VERSION_COUNT} instances)"
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"skipped\", \"reason\": \"already_completed\", \"versions_count\": $VERSION_COUNT}" >> "$SUMMARY_FILE"
        continue
    fi

    # ----------------------------------------------------------
    # Step 1: Collect PRs
    # ----------------------------------------------------------
    print_step "Step 1/3: PRs" "$REPO_FULL"

    if [ -f "$PRS_FILE" ]; then
        PR_COUNT=$(wc -l < "$PRS_FILE")
        echo -e "  ${DIM}exists ($PR_COUNT PRs)${RESET}"
    else
        EXIT_CODE=0
        timeout "${STEP_TIMEOUT}s" python print_pulls.py "$REPO_FULL" "$PRS_FILE" --workers "$PR_WORKERS" 2>&1 || EXIT_CODE=$?
        if [ "$EXIT_CODE" -ne 0 ]; then
            FAILED=$((FAILED + 1))
            if [ "$EXIT_CODE" -eq 124 ]; then
                echo -e "  ${RED}TIMEOUT${RESET} print_pulls.py exceeded ${STEP_TIMEOUT}s"
                echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 1, \"error\": \"timeout ${STEP_TIMEOUT}s\"}" >> "$SUMMARY_FILE"
            else
                echo -e "  ${RED}FAIL${RESET} print_pulls.py error (exit code $EXIT_CODE)"
                echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 1, \"error\": \"print_pulls failed\", \"exit_code\": $EXIT_CODE}" >> "$SUMMARY_FILE"
            fi
            continue
        fi
    fi

    if [ ! -f "$PRS_FILE" ]; then
        FAILED=$((FAILED + 1))
        echo -e "  ${RED}FAIL${RESET} No PRs file generated"
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 1, \"error\": \"no prs file\"}" >> "$SUMMARY_FILE"
        continue
    fi

    PR_COUNT=$(wc -l < "$PRS_FILE")

    if [ "$PR_COUNT" -eq 0 ]; then
        SKIPPED=$((SKIPPED + 1))
        echo -e "  ${YELLOW}SKIP${RESET} 0 PRs found"
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"skipped\", \"reason\": \"no_prs\"}" >> "$SUMMARY_FILE"
        continue
    fi

    # ----------------------------------------------------------
    # Step 2: Build dataset (async)
    # ----------------------------------------------------------
    print_step "Step 2/3: Instances" "$REPO_FULL ($PR_COUNT PRs)"

    if [ -f "$INSTANCES_FILE" ]; then
        INSTANCE_COUNT=$(wc -l < "$INSTANCES_FILE")
        echo -e "  ${DIM}exists ($INSTANCE_COUNT instances)${RESET}"
    else
        EXIT_CODE=0
        timeout "${STEP_TIMEOUT}s" python build_dataset_async.py \
                "$PRS_FILE" \
                "$INSTANCES_FILE" \
                --language "$LANGUAGE" \
                --max_concurrency "$ASYNC_CONCURRENCY" 2>&1 || EXIT_CODE=$?
        if [ "$EXIT_CODE" -ne 0 ]; then
            FAILED=$((FAILED + 1))
            if [ "$EXIT_CODE" -eq 124 ]; then
                echo -e "  ${RED}TIMEOUT${RESET} build_dataset_async.py exceeded ${STEP_TIMEOUT}s"
                echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 2, \"error\": \"timeout ${STEP_TIMEOUT}s\", \"pr_count\": $PR_COUNT}" >> "$SUMMARY_FILE"
            else
                echo -e "  ${RED}FAIL${RESET} build_dataset_async.py error (exit code $EXIT_CODE)"
                echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 2, \"error\": \"build_dataset failed\", \"pr_count\": $PR_COUNT, \"exit_code\": $EXIT_CODE}" >> "$SUMMARY_FILE"
            fi
            continue
        fi
    fi

    if [ ! -f "$INSTANCES_FILE" ] || [ ! -s "$INSTANCES_FILE" ]; then
        SKIPPED=$((SKIPPED + 1))
        echo -e "  ${YELLOW}SKIP${RESET} No valid instances (from $PR_COUNT PRs)"
        echo "{\"repo\": \"$REPO_FULL\", \"status\": \"skipped\", \"reason\": \"no_instances\", \"pr_count\": $PR_COUNT}" >> "$SUMMARY_FILE"
        continue
    fi

    INSTANCE_COUNT=$(wc -l < "$INSTANCES_FILE")

    # ----------------------------------------------------------
    # Step 3: Get versions
    # ----------------------------------------------------------
    print_step "Step 3/3: Versions" "$REPO_FULL ($INSTANCE_COUNT instances)"

    if [ -f "$VERSIONS_FILE" ]; then
        VERSION_COUNT=$(wc -l < "$VERSIONS_FILE")
        echo -e "  ${DIM}exists ($VERSION_COUNT instances)${RESET}"
    else
        EXIT_CODE=0
        timeout "${STEP_TIMEOUT}s" python get_version.py \
                --instance_path "$INSTANCES_FILE" \
                --testbed "$TESTBED" \
                --max-workers "$VERSION_WORKERS" 2>&1 || EXIT_CODE=$?
        if [ "$EXIT_CODE" -ne 0 ]; then
            FAILED=$((FAILED + 1))
            if [ "$EXIT_CODE" -eq 124 ]; then
                echo -e "  ${RED}TIMEOUT${RESET} get_version.py exceeded ${STEP_TIMEOUT}s"
                echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 3, \"error\": \"timeout ${STEP_TIMEOUT}s\", \"pr_count\": $PR_COUNT, \"instance_count\": $INSTANCE_COUNT}" >> "$SUMMARY_FILE"
            else
                echo -e "  ${RED}FAIL${RESET} get_version.py error (exit code $EXIT_CODE)"
                echo "{\"repo\": \"$REPO_FULL\", \"status\": \"failed\", \"step\": 3, \"error\": \"get_version failed\", \"pr_count\": $PR_COUNT, \"instance_count\": $INSTANCE_COUNT, \"exit_code\": $EXIT_CODE}" >> "$SUMMARY_FILE"
            fi
            continue
        fi
    fi

    if [ -f "$VERSIONS_FILE" ]; then
        VERSION_COUNT=$(wc -l < "$VERSIONS_FILE")
    fi

    REPO_ELAPSED=$(( $(date +%s) - REPO_START_TIME ))
    SUCCEEDED=$((SUCCEEDED + 1))
    TOTAL_INSTANCES=$((TOTAL_INSTANCES + VERSION_COUNT))
    echo -e "  ${GREEN}OK${RESET} PRs=${PR_COUNT} Instances=${INSTANCE_COUNT} Versioned=${VERSION_COUNT} ${DIM}($(format_duration $REPO_ELAPSED))${RESET}"
    echo "{\"repo\": \"$REPO_FULL\", \"status\": \"success\", \"pr_count\": $PR_COUNT, \"instance_count\": $INSTANCE_COUNT, \"version_count\": $VERSION_COUNT, \"elapsed_seconds\": $REPO_ELAPSED}" >> "$SUMMARY_FILE"

done

# ============================================================
# 8. Final summary
# ============================================================
TOTAL_ELAPSED=$(( $(date +%s) - PIPELINE_START_TIME ))

echo ""
echo ""
draw_progress_bar "$PROCESSED" "$NUM_TO_PROCESS"
echo ""
echo ""
echo -e "${BOLD}========================================${RESET}"
echo -e "${BOLD}  Pipeline Complete${RESET}"
echo -e "${BOLD}========================================${RESET}"
echo -e "  Language:         ${CYAN}$LANGUAGE${RESET}"
echo -e "  Processed:        ${BOLD}$PROCESSED${RESET} repos"
echo -e "  ${GREEN}Succeeded:      $SUCCEEDED${RESET}"
echo -e "  ${RED}Failed:         $FAILED${RESET}"
echo -e "  ${YELLOW}Skipped:        $SKIPPED${RESET}"
echo -e "  Total instances:  ${BOLD}$TOTAL_INSTANCES${RESET}"
echo -e "  Elapsed:          ${BOLD}$(format_duration $TOTAL_ELAPSED)${RESET}"
echo -e "  Summary log:      $SUMMARY_FILE"
echo ""
echo -e "  Output: ${DIM}$DATA_ROOT/$LANG_LOWER/<owner>/<repo>/${RESET}"
echo -e "          ${DIM}  prs.jsonl / instances.jsonl / instances_versions.jsonl${RESET}"
echo -e "${BOLD}========================================${RESET}"
