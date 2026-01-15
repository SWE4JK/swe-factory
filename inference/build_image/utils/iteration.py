"""Iteration metadata helpers."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging_utils import utc_now


@dataclass
class IterationContext:
    iteration: int
    directory: Path
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_time: datetime = field(default_factory=datetime.utcnow)
    success: bool = False
    error: Optional[Exception] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = copy.deepcopy(self.metadata)
        payload.setdefault("iteration", self.iteration)
        payload.setdefault("iteration_dir", self.metadata.get("iteration_dir"))
        payload.setdefault("start_time", self.metadata.get("start_time", utc_now()))
        payload.setdefault("status", "success" if self.success else "failed")
        return payload


class IterationRecorder:
    """Capture metadata for each iteration and write to disk on finalize."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.records: List[Dict[str, Any]] = []

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.output_dir))
        except ValueError:
            return str(path)

    def start(self, iteration: int) -> IterationContext:
        directory = self.output_dir / f"iteration_{iteration}"
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "iteration": iteration,
            "iteration_dir": self.relative(directory),
            "start_time": utc_now(),
            "status": "in_progress",
        }
        ctx = IterationContext(iteration=iteration, directory=directory, metadata=meta)
        return ctx

    def finalize(
        self,
        ctx: IterationContext,
        stats: Dict[str, Any],
        success: bool,
        error_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        end_time = datetime.utcnow()
        ctx.success = success
        ctx.metadata["status"] = "success" if success else "failed"
        ctx.metadata["end_time"] = end_time.isoformat() + "Z"
        ctx.metadata["duration_seconds"] = (end_time - ctx.start_time).total_seconds()

        ctx.metadata["llm_calls"] = stats.get("llm_calls", 0)
        ctx.metadata["cost"] = stats.get("cost", 0.0)
        ctx.metadata["input_tokens"] = stats.get("input_tokens", 0)
        ctx.metadata["output_tokens"] = stats.get("output_tokens", 0)
        if stats.get("response_log"):
            ctx.metadata["response_log"] = stats["response_log"]
        if error_payload:
            ctx.metadata["error"] = error_payload

        metadata_path = ctx.directory / "metadata.json"
        metadata_path.write_text(
            json.dumps(ctx.metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        ctx.metadata["metadata_path"] = self.relative(metadata_path)
        self.records.append(copy.deepcopy(ctx.metadata))
        return ctx.metadata

    def summarize(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self.records)
