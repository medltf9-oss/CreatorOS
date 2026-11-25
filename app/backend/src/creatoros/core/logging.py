"""Structured logging with mandatory redaction.

Credentials must never reach a log file, a crash dump or a diagnostics bundle
(INV-3). Two layers protect that:

1. Key-based: any mapping key matching SENSITIVE_KEYS is replaced outright.
2. Value-based: strings matching known credential shapes (Meta/Instagram tokens,
   JWTs, bearer headers, `act.` ids) are replaced by a pattern label.

The redactor is exercised by tests with synthetic secrets so regressions fail CI
rather than leaking in the field.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

from .paths import ensure_dir

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "refresh_token",
        "token",
        "session_token",
        "client_secret",
        "app_secret",
        "password",
        "passwd",
        "secret",
        "code",
        "code_verifier",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api_key",
    }
)

REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"EAAG[a-zA-Z0-9._-]{10,}", re.IGNORECASE), "[REDACTED_META_TOKEN]"),
    (re.compile(r"IGQV[a-zA-Z0-9._-]{10,}", re.IGNORECASE), "[REDACTED_IG_TOKEN]"),
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bact\.[0-9]{6,}"), "[REDACTED_ACT_ID]"),
    (re.compile(r"\bcli_[a-z0-9]{16,}\b"), "[REDACTED_TIKTOK_CLIENT]"),
)

_MAX_DEPTH = 6


def get_logger(name: str) -> logging.Logger:
    """Module logger; only emits after configure_logging() has run."""
    return logging.getLogger(name)


def redact_value(value: Any, _depth: int = 0) -> Any:
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact_value(val, _depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item, _depth + 1) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in REDACTION_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted if len(redacted) <= 4000 else redacted[:4000] + "…[truncated]"
    return value


class RedactionFilter(logging.Filter):
    """Scrubs a record's message and structured extras before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_value(record.msg)
        if record.args:
            record.args = tuple(redact_value(arg) for arg in record.args)  # type: ignore[assignment]
        for key, value in list(record.__dict__.items()):
            if key in ("msg", "args", "exc_info", "exc_text", "stack_info"):
                continue
            if str(key).lower() in SENSITIVE_KEYS:
                record.__dict__[key] = "[REDACTED]"
            elif isinstance(value, (str, dict, list)):
                record.__dict__[key] = redact_value(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("correlation_id", "component", "task_id", "job_id", "agent_id", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact_value(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_dir: Path | None = None, to_stdout: bool = True) -> Path:
    """Configure daily-rotating JSON log files on D: plus optional stdout."""
    directory = log_dir or ensure_dir("logs", "backend")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "creatoros.log"

    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(RedactionFilter())
    root.addHandler(file_handler)

    if to_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(JsonFormatter())
        stream.addFilter(RedactionFilter())
        root.addHandler(stream)

    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")
    return target
