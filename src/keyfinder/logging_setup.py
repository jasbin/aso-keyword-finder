"""Process-wide logging configuration (human-readable text or JSON lines on stderr)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Literal

__all__ = ["JsonFormatter", "TextFormatter", "configure_logging"]

LogFormat = Literal["text", "json"]

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Chatty third-party loggers. httpx logs every request URL (query string included) at INFO.
_QUIET_LOGGERS = ("httpx", "httpcore")

# Attributes every LogRecord carries; anything else on a record came from ``extra=``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",  # added in Python 3.12
    "color_message",  # uvicorn's ANSI-coloured duplicate of the message
}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Fields passed via ``logger.x(..., extra={...})`` (private ``_names`` are skipped)."""
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value, default=str, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _logfmt_value(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    if text and not any(ch.isspace() or ch in "\"'=" for ch in text):
        return text
    return json.dumps(text, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``msg`` plus any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in _extra_fields(record).items():
            # Never let an extra field clobber the core keys.
            payload[key if key not in payload else f"extra_{key}"] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc_info"] = record.exc_text
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        try:
            return json.dumps(payload, default=str, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            # e.g. dicts with non-string keys or NaN: fall back to repr() for the offending values.
            safe = {key: value if _json_safe(value) else repr(value) for key, value in payload.items()}
            return json.dumps(safe, default=str, ensure_ascii=False, allow_nan=False)


class TextFormatter(logging.Formatter):
    """``<time> <LEVEL> <logger>: <message> key=value ...`` (extras rendered logfmt-style)."""

    def __init__(self) -> None:
        super().__init__(_TEXT_FORMAT)

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        extras = _extra_fields(record)
        if not extras:
            return line
        rendered = " ".join(f"{key}={_logfmt_value(value)}" for key, value in extras.items())
        return f"{line} {rendered}"


class _StderrHandler(logging.Handler):
    """Writes to whatever ``sys.stderr`` is *at emit time*.

    Binding the stream at configuration time breaks when stderr is swapped later
    (test runners, ``typer.testing.CliRunner``, Rich consoles), so look it up per record.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            stream = sys.stderr
            stream.write(message + "\n")
            stream.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


def _resolve_level(level: str) -> int:
    resolved = logging.getLevelName(level.strip().upper())
    if not isinstance(resolved, int):
        raise ValueError(f"unknown log level {level!r}; use DEBUG, INFO, WARNING, ERROR or CRITICAL")
    return resolved


def configure_logging(level: str = "INFO", fmt: LogFormat = "text") -> None:
    """Configure the root logger to write ``fmt``-formatted records to stderr.

    Idempotent: calling it again replaces the handler installed by a previous call
    (so the level/format can be changed) and never duplicates output. Handlers
    installed by anything else (e.g. pytest's capture handler) are left alone.
    """
    numeric_level = _resolve_level(level)
    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    elif fmt == "text":
        formatter = TextFormatter()
    else:
        raise ValueError(f"unknown log format {fmt!r}; use 'text' or 'json'")

    handler = _StderrHandler()
    handler.setFormatter(formatter)
    handler.set_name("keyfinder")

    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _StderrHandler):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
