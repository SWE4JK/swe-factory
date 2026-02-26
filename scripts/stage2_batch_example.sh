#!/bin/bash
#
# stage2_batch_example.sh — Example usage of stage2_batch.sh
#
# This file demonstrates how to use stage2_batch.sh with different configurations.
# Copy and modify this file to suit your needs.
#

# ============================================================
# Setup API credentials
# ============================================================
export OPENAI_API_BASE=https://siflow-longmen.siflow.cn/siflow/longmen/skyinfer/skliu/minimax-litellm/v1/4000
export OPENAI_API_BASE_URL=$OPENAI_API_BASE  # Some tools read this variable
export OPENAI_API_KEY=EMPTY
export OPENAI_KEY=$OPENAI_API_KEY  # Some tools read this variable

# ============================================================
# Example 1: Process all Python repos with default settings
# ============================================================
# bash scripts/stage2_batch.sh \
#     --language Python \
#     --model litellm-generic-openai/minimax-m2.1

# ============================================================
# Example 2: Process specific repos only
# ============================================================
# bash scripts/stage2_batch.sh \
#     --language Python \
#     --model litellm-generic-openai/minimax-m2.1 \
#     --repo-filter "OpenHands,autogen,gpt_academic"

# ============================================================
# Example 3: Process repos 10-20 with higher concurrency
# ============================================================
# bash scripts/stage2_batch.sh \
#     --language Python \
#     --model litellm-generic-openai/minimax-m2.1 \
#     --start-from 10 \
#     --end-at 20 \
#     --num-processes 20

# ============================================================
# Example 4: Skip already processed repos
# ============================================================
# bash scripts/stage2_batch.sh \
#     --language Python \
#     --model litellm-generic-openai/minimax-m2.1 \
#     --skip-existing

# ============================================================
# Example 5: Use different model (GPT-4)
# ============================================================
# export OPENAI_API_BASE=https://api.openai.com/v1
# export OPENAI_API_KEY=sk-xxx
# bash scripts/stage2_batch.sh \
#     --language Python \
#     --model gpt-4-turbo \
#     --num-processes 5 \
#     --conv-round-limit 15

# ============================================================
# Example 6: Process Java repos
# ============================================================
# bash scripts/stage2_batch.sh \
#     --language Java \
#     --model litellm-generic-openai/minimax-m2.1

# ============================================================
# Example 7: Resume from a specific index (e.g., if interrupted)
# ============================================================
# bash scripts/stage2_batch.sh \
#     --language Python \
#     --model litellm-generic-openai/minimax-m2.1 \
#     --start-from 25 \
#     --skip-existing

# ============================================================
# Recommended: Use this for production
# ============================================================
bash scripts/stage2_batch.sh \
    --language Python \
    --model litellm-generic-openai/minimax-m2.1 \
    --num-processes 10 \
    --conv-round-limit 10 \
    --skip-existing
