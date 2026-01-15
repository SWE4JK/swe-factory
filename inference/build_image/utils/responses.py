"""LLM call tracking and logging."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Optional

from .logging_utils import utc_now


class ResponseTracker:
    """Manage per-iteration usage stats and JSONL logs of model calls."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.responses_dir = self.output_dir / "responses"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.iteration_stats: DefaultDict[Optional[int], Dict[str, Any]] = defaultdict(
            lambda: {
                "cost": 0.0,
                "llm_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "response_log": None,
            }
        )
        self.total_cost: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.current_iteration: Optional[int] = None

    def set_iteration(self, iteration: Optional[int]) -> None:
        self.current_iteration = iteration
        self.iteration_stats[iteration]  # ensure entry exists

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.output_dir))
        except ValueError:
            return str(path)

    def log_call(self, payload: Dict[str, Any], response: Dict[str, Any]) -> None:
        iteration = self.current_iteration
        stats = self.iteration_stats[iteration]

        usage = response.get("usage", {})
        cost = float(usage.get("cost", 0.0) or 0.0)
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0

        entry = {
            "timestamp": utc_now(),
            "iteration": iteration,
            "call_index": stats["llm_calls"],
            "request": payload,
            "response": response,
            "usage": usage,
            "cost": cost,
        }

        stats["llm_calls"] += 1
        stats["cost"] += cost
        stats["input_tokens"] += prompt_tokens
        stats["output_tokens"] += output_tokens

        self.total_cost += cost
        self.total_input_tokens += prompt_tokens
        self.total_output_tokens += output_tokens

        target = (
            self.responses_dir / ("global.jsonl" if iteration is None else f"iteration_{iteration}.jsonl")
        )
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False))
            fh.write("\n")
        stats["response_log"] = self._relative(target)

    def get_stats(self, iteration: Optional[int]) -> Dict[str, Any]:
        return self.iteration_stats[iteration]

    def aggregate_totals(self) -> Dict[str, Any]:
        return {
            "total_cost": round(self.total_cost, 6),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }

    def list_response_logs(self) -> list[str]:
        logs = []
        for child in sorted(self.responses_dir.glob("*.jsonl")):
            logs.append(self._relative(child))
        return logs
