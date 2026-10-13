"""Structured JSON logging.

Every LabBench log record is emitted as a single JSON object to stdout and
appended to ``logs/labbench.log``. This keeps human-readable CLI output on
stdout/click and machine-readable diagnostics in the log stream.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import paths

_CONFIGURED = False

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a log record as a compact JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    """Configure the ``labbench`` logger once. Idempotent."""

    global _CONFIGURED
    logger = logging.getLogger("labbench")
    if _CONFIGURED:
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    formatter = JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if log_file is None:
        log_file = paths.logs_dir() / "labbench.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # Logging to file is best-effort; stdout always works.
        pass

    _CONFIGURED = True
    return logger


def get_logger(name: str = "labbench") -> logging.Logger:
    return logging.getLogger(name if name.startswith("labbench") else f"labbench.{name}")


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: object) -> None:
    """Log ``msg`` with structured ``fields`` attached to the JSON record."""

    logger.log(level, msg, extra=fields)
