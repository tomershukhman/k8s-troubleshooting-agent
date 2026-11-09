"""Structured JSON logs (DESIGN 3.9): one JSON object per line. A run logs through a stdlib
`LoggerAdapter` that adds its run_id, thread_id and user_id to every line."""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = "k8s_agent"
# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            **{k: v for k, v in vars(record).items() if k not in _STANDARD},
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level: str, file: Path | None = None) -> None:
    """Send this package's logs as JSON lines to `file`, else to stderr unless it's an
    interactive terminal (JSON is for machines; the CLI's progress display shows the same events
    there). Called by the CLI; the core only logs."""
    logger = logging.getLogger(LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler: logging.Handler = logging.NullHandler()
    if file is not None or not sys.stderr.isatty():
        handler = logging.FileHandler(file) if file else logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def run_log(**ids: str) -> logging.LoggerAdapter:
    """A logger for one run: `log.info("event", extra={...})` lines carry `ids` too."""
    return logging.LoggerAdapter(logging.getLogger(f"{LOGGER}.agent"), ids, merge_extra=True)
