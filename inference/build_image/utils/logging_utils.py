"""Logging helpers for the modular transfer agent."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_file_logger(logger: logging.Logger, log_file: str | Path) -> logging.Logger:
    """Attach a UTF-8 file handler to ``logger`` if not already present."""
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == path:
            break
    else:
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(file_handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def utc_now() -> str:
    """Return current UTC time ISO string with trailing ``Z``."""
    return __import__("datetime").datetime.utcnow().isoformat() + "Z"
