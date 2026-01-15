"""Prompt utilities."""

from __future__ import annotations

import json
import os
from pathlib import Path

HEREDOC_DELIMITER = "EOF_114329324912"
SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_DIR = SCRIPT_DIR / "prompt_assets"


def _read_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return fh.read()


def get_system_prompt() -> str:
    return _read_prompt("system_prompt_root.txt")


def get_unroot_prompt(dockerfile: str, eval_script: str) -> str:
    prompt = _read_prompt("user_prompt_root.txt")
    prompt = prompt.replace("{{ORIGINAL_DOCKERFILE}}", dockerfile)
    return prompt


def get_dockerfile_selfcheck_prompt(
    original: str,
    candidate: str,
    diff: str,
    checklist: str,
) -> str:
    prompt = _read_prompt("user_prompt_selfcheck_dockerfile.txt")
    prompt = prompt.replace("{{ORIGINAL_DOCKERFILE}}", original)
    prompt = prompt.replace("{{CANDIDATE_DOCKERFILE}}", candidate)
    prompt = prompt.replace("{{DOCKER_DIFF}}", diff)
    prompt = prompt.replace("{{CHECKLIST}}", checklist)
    return prompt


def get_eval_review_prompt(
    final_dockerfile: str,
    docker_diff: str,
    original_eval_script: str,
) -> str:
    prompt = _read_prompt("user_prompt_eval_review.txt")
    prompt = prompt.replace("{{FINAL_DOCKERFILE}}", final_dockerfile)
    prompt = prompt.replace("{{DOCKER_DIFF}}", docker_diff)
    prompt = prompt.replace("{{ORIGINAL_EVAL_SCRIPT}}", original_eval_script)
    return prompt
