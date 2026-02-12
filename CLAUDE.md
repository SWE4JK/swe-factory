# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SWE-Factory is an automated framework for creating GitHub issue resolution training data and evaluation benchmarks. It consists of three main stages:

1. **Data Collection**: Collect raw issue/PR data from GitHub repositories
2. **SWE-Builder**: Automated evaluation environment setup using multi-agent LLM system
3. **Validation & Evaluation**: Fail2Pass validation and agent inference

The codebase supports multiple programming languages (Python, Java, JavaScript, TypeScript).

## Architecture

### Core Components

**SWE-Builder Multi-Agent System** (`app/agents/`):
- `context_retrieval_agent`: Repository Explorer - gathers environment setup and test commands
- `write_dockerfile_agent`: Environment Manager - generates Dockerfiles for reproducible environments
- `write_eval_script_agent`: Test Manager - writes evaluation scripts
- `test_analysis_agent`: Test Analyst - validates environments and orchestrates refinement
- `agents_manager.py`: Orchestrates the multi-agent workflow with memory pool for reusing successful setups

**Data Collection** (`data_collection/`):
- `collect/get_top_repos.py`: Fetches popular repositories by language
- `collect/print_pulls.py`: Collects raw PR data from GitHub
- `collect/build_dataset_async.py`: Constructs task instances from PRs
- `collect/get_version.py`: Assigns version numbers to instances
- `versioning/`: Handles data versioning and labeling
- `pipeline.sh`: Automated end-to-end data collection pipeline

**Inference** (`inference/agenthub/`):
- Two-stage pipeline: build/transfer images, then run agents
- Supports multiple scaffolds: `mini_swe_agent`, `live_swe_agent`, `r2egym` (DeepSWE), `openhands`
- `runtime/`: Docker-based runtime for agent execution
- `tools/`: Agent tools (bash, file editor, search, etc.)
- `trajectory/`: Trajectory logging and SWE-bench submission generation

**Evaluation** (`evaluation/`):
- `run_evaluation.py`: Main evaluation script for patches
- `docker_build.py`: Docker image building and management
- `test_spec.py`: Test specification handling

## Common Commands

### Environment Setup

Main environment (SWE-Builder):
```bash
conda create --name swe-factory python=3.12.5 -y
conda activate swe-factory
pip install -r requirements.txt
```

Inference environment:
```bash
conda create -n inference python=3.13 -y
conda activate inference
pip install -r requirements-inference.txt
```

### Data Collection

Set GitHub token:
```bash
export GITHUB_TOKEN=<your_token>
```

Full automated pipeline:
```bash
cd data_collection/collect
bash ../pipeline.sh --language Python --top_n 100
```

Manual steps:
```bash
# 1. Fetch top repositories
python get_top_repos.py --language Python --output_path data/popular_repos --top_n 100

# 2. Collect PR data
python print_pulls.py python-attrs/attrs data/python-attrs/attrs/prs.jsonl

# 3. Build task instances
python build_dataset.py data/python-attrs/attrs/prs.jsonl data/python-attrs/attrs/instances.jsonl --language python

# 4. Add version info
python get_version.py --instance_path data/python-attrs/attrs/instances.jsonl --testbed github --max-workers 20
```

### SWE-Builder (Stage 2: Environment Setup)

Set LLM credentials:
```bash
export OPENAI_API_BASE_URL=<your_base_url>
export OPENAI_KEY=<your_key>
```

Run SWE-Builder:
```bash
python app/main.py swe-bench \
    --model gpt-4.1-mini \
    --tasks-map "python-mypy-instances.jsonl" \
    --num-processes 10 \
    --model-temperature 0.2 \
    --conv-round-limit 10 \
    --output-dir "output/gpt-4.1-mini/mypy" \
    --setup-dir "testbed" \
    --results-path "output/gpt-4.1-mini/mypy/results"
```

Batch processing example:
```bash
bash run/run.sh
```

### Fail2Pass Validation (Stage 3)

Generate test logs:
```bash
python evaluation/run_evaluation.py \
  --dataset_name "output/gpt-4.1-mini/mypy/results/results.json" \
  --predictions_path "gold" \
  --max_workers 5 \
  --run_id "mypy_fail2pass_check" \
  --output_path "run_instances" \
  --timeout 3600 \
  --is_judge_fail2pass
```

Validate Fail2Pass:
```bash
python scripts/judge_fail2pass.py evaluation/run_instance/mypy_gpt-4.1-mini/gold fail2pass_status.json
```

### Inference (Agent Execution)

**Stage 1: Build SWE Environment Images**

Set credentials:
```bash
export OPENAI_API_KEY="YOUR_API_KEY"
export OPENAI_BASE_URL="YOUR_URL"  # optional
```

Build and transfer images:
```bash
python inference/build_image/main.py \
  --input /path/to/instances.json \
  --output /path/to/run_dir \
  --max-iterations 5 \
  --eval-timeout 300 \
  --max-workers 2 \
  --model_name <model_name>
```

**Stage 2: Run Coding Agent**

Set LiteLLM credentials:
```bash
export LLM_BASE_URL="YOUR_URL"
export OPENAI_API_KEY="YOUR_API_KEY"
```

Example with r2egym (DeepSWE) scaffold:
```bash
python -m inference.agenthub.run.edit runagent_multiple \
  --dataset /path/to/TRANSFERRED_DATASET.json \
  --split dev \
  --k 1 \
  --start_idx 0 \
  --max_workers 5 \
  --traj_dir ./run_logs/deepswe_run \
  --exp_name deepswe_run \
  --llm_name openai/gpt-4o-mini \
  --use_fn_calling True \
  --backend docker \
  --scaffold r2egym
```

Other scaffolds: `mini_swe_agent`, `live_swe_agent`, `openhands`

### Evaluation with Patches

```bash
python evaluation/run_evaluation.py \
  --dataset_name "mypy_valid.json" \
  --predictions_path "predictions.json" \
  --max_workers 5 \
  --run_id "mypy_evaluation" \
  --output_path "run_instances" \
  --timeout 3600
```

### Utility Scripts

Compute costs:
```bash
python scripts/compute_cost.py <trajectory_dir>
```

## Key Implementation Details

### SWE-Builder Memory Pool

`app/agents/agents_manager.py` implements a memory pool system that:
- Stores successful environment setups in `<results_path>/memory_pool.json`
- Reuses setups for similar repositories/versions using `get_closest_version_info()`
- Reduces redundant LLM calls for similar environments
- Can be disabled with `--disable-memory-pool` flag

### Data Collection Pipeline

The `data_collection/pipeline.sh` script:
- Uses proxy rotation for GitHub API calls (avoiding rate limits without tokens)
- Supports resuming from specific indices (`--start-from`, `--end-at`)
- Processes repositories in parallel with configurable workers
- Handles timeouts gracefully (default 1800s per step)

### Inference Runtime

The `inference/agenthub/runtime/docker.py`:
- Mounts repository at `/testbed` for consistency
- Normalizes environment to reduce noise (e.g., removing `.venv`)
- Enforces eval timeout (default 300s)
- Supports both function-calling and non-function-calling modes

### Task Instance Format

Task instances (JSONL) contain:
- `instance_id`: Unique identifier (e.g., `python-attrs__attrs-173`)
- `repo`: Repository name (e.g., `python-attrs/attrs`)
- `base_commit`: Commit before the fix
- `patch`: Ground truth patch
- `test_patch`: Test changes
- `problem_statement`: Issue description
- `hints_text`: Optional hints
- `version`: Version tag
- `docker_image`, `dockerfile`, `eval_script`: Added by SWE-Builder

## Environment Variables

Core:
- `GITHUB_TOKEN`: Required for GitHub API access (data collection)
- `OPENAI_API_BASE_URL`: Base URL for OpenAI-compatible API (Stage 1 & 2)
- `OPENAI_KEY` or `OPENAI_API_KEY`: API key for LLM calls

Inference Stage 2 (LiteLLM):
- `LLM_BASE_URL`: Base URL for LiteLLM
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`: Provider-specific keys

Optional (data collection pipeline):
- `VORTEX_PROXY_HOST`, `VORTEX_PROXY_PASSWORD`: Proxy configuration
- `VORTEX_PROXY_HTTP_PORT`, `VORTEX_PROXY_HTTPS_PORT`: Proxy ports

## Testing

Tests are not extensively implemented. The main validation is through:
1. SWE-Builder's iterative refinement with `test_analysis_agent`
2. Fail2Pass validation comparing test outputs before/after patch
3. Manual inspection of generated environments

## Output Directories

- `output/`: SWE-Builder results (Dockerfiles, eval scripts, memory pool)
- `testbed/`: Temporary directories for environment setup (format: `repo__issue_timestamp`)
- `run_instances/`: Evaluation run outputs (test logs, patches)
- `run_logs/`: Inference trajectory logs (agent histories, patches, rewards)
- `data_collection/collect/data/`: Raw collected PR/instance data
- `data_collection/collect/github/_cache/`: Repository cache for data collection

## Model Support

SWE-Builder supports multiple LLM providers via `app/model/`:
- `gpt.py`: OpenAI models
- `claude.py`: Anthropic Claude
- `gemini.py`: Google Gemini
- `ollama.py`: Local Ollama
- `gptlitellm.py`: LiteLLM (unified interface)
- `groq.py`, `azure.py`, `bedrock.py`: Other providers

Inference (Stage 2) exclusively uses LiteLLM for model calls.

## Docker Usage

Docker is heavily used throughout:
- SWE-Builder creates and tests Docker images iteratively
- Evaluation runs tests in isolated containers
- Inference executes agents inside Docker environments
- Images follow naming convention: `swefactory/<repo_name>:<instance_id>`

Clean up Docker resources:
```bash
docker ps -a -q | xargs docker rm -f
docker image prune -af
```

## Important File Patterns

- Task instances: `*instances.jsonl`, `*instances_versions.jsonl`
- Transferred datasets: `*_transferred.json` (after Stage 1 inference prep)
- Memory pool: `memory_pool.json` (in results directory)
- Test outputs: `test_output_prev_apply.txt`, `test_output_after_apply.txt`
- Patches: `output_patch.diff`, `pred_minimal_try_<n>.patch`
- Trajectories: `trajectories.jsonl`, `trajectories_rejection_sampling.jsonl`

## Language-Specific Notes

- **Python**: Best supported, use `r2egym` or `openhands` scaffold
- **Java**: Requires `install-jdk` dependency, use bash-only scaffolds
- **JavaScript/TypeScript**: Use `--language js` for both, use bash-only scaffolds
- Multi-language repos: Prefer `mini_swe_agent` or `live_swe_agent` scaffolds
