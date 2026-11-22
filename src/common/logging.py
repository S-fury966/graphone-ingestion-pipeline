"""Structured logging: JSON lines to console + a rotating file in data/logs/."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path("data/logs")
_configured = False


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Attach any structured extras passed via logger.info(..., extra={"extra": {...}})
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(
    level: int = logging.INFO,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    log_file: str = "pipeline.log",
) -> None:
    """Configure root logging once: JSON to stdout and a rotating file."""
    global _configured
    if _configured:
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = JsonFormatter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_dir / log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, configuring logging on first use."""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
