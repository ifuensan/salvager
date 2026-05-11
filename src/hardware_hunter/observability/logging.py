"""Structured JSON Lines logger for hardware-hunter — NFR-O1 / NFR-R5.

Every record emitted via :func:`get_logger` is a single JSON object on stdout
carrying the standard fields:

- ``level`` — ``debug | info | warn | error`` (NOT Python's default ``warning``)
- ``ts``    — ISO 8601 with millisecond precision and ``Z`` suffix
- ``event`` — snake_case event name (the log call's first argument)

Optional fields the caller supplies via ``extra={...}`` are included verbatim:
``entry``, ``marketplace``, ``listing_id``, ``latency_ms``, ``error_class``,
plus any other key the caller passes.

Unhandled exceptions trigger a final structured ``unhandled_exception`` record
via ``sys.excepthook`` and the interpreter exits non-zero (NFR-R5).

Configuration:
- Default level is ``info``.
- ``HARDWARE_HUNTER_LOG_LEVEL`` environment variable overrides at process start.
- :func:`configure_log_level` is called by the ``config.yaml`` loader at
  daemon startup (lands in Story 2.5).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

# Fields built into logging.LogRecord that are framework-internal — exclude
# from the JSON output. Anything else found on the record is treated as
# caller-supplied extras and surfaced as a top-level field.
_RESERVED_LOGRECORD_FIELDS = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Python's logging uses "warning" but NFR-O1 names "warn". Map at the
# serialization boundary so callers may use either Python idiom.
_LEVEL_NAMES = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

_LOGGER_ROOT = "hardware_hunter"
_CONFIGURED = False


def _iso8601_z(epoch_seconds: float) -> str:
    """ISO 8601 UTC with millisecond precision and ``Z`` suffix."""
    dt = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{dt.microsecond // 1000:03d}Z"


def _resolve_level(name: str) -> int:
    """Resolve a level name (``info``, ``warn``, ``error``, etc.) to the
    numeric logging level. Accepts both ``warn`` and ``warning``."""
    lower = name.lower()
    if lower in {"warn", "warning"}:
        return logging.WARNING
    return logging.getLevelNamesMapping().get(lower.upper(), logging.INFO)


class JsonLineFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as one JSON object, one line."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "level": _LEVEL_NAMES.get(record.levelno, record.levelname.lower()),
            "ts": _iso8601_z(record.created),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_FIELDS or key.startswith("_"):
                continue
            out[key] = value
        if record.exc_info:
            out["stack"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(out, default=str, separators=(",", ":"))


class _DynamicStdoutHandler(logging.Handler):
    """Stream handler that re-resolves ``sys.stdout`` at every emit.

    Compatible with pytest's stdout-capturing fixtures (``capsys`` / ``capfd``)
    that swap ``sys.stdout`` between tests, and with downstream consumers that
    pipe stdout to ``jq``.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def _excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> None:
    """Final structured-log record on unhandled exception (NFR-R5).

    KeyboardInterrupt is passed through to the default excepthook so an
    operator pressing Ctrl+C does not show up as a crash event.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logging.getLogger(_LOGGER_ROOT).error(
        "unhandled_exception",
        exc_info=(exc_type, exc_value, exc_tb),
        extra={
            "error_class": exc_type.__name__,
            "error_message": str(exc_value),
        },
    )


def _configure_root(level_name: str | None = None) -> None:
    """Idempotent root-logger setup. Safe to call repeatedly; the level
    argument, if provided, always overrides the current threshold."""
    global _CONFIGURED
    root = logging.getLogger(_LOGGER_ROOT)

    if _CONFIGURED:
        if level_name is not None:
            root.setLevel(_resolve_level(level_name))
        return

    root.handlers.clear()
    root.propagate = False

    handler = _DynamicStdoutHandler()
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)

    resolved = level_name or os.environ.get("HARDWARE_HUNTER_LOG_LEVEL", "info")
    root.setLevel(_resolve_level(resolved))

    sys.excepthook = _excepthook
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured structured-JSON logger under ``hardware_hunter.<name>``.

    The root configuration is idempotent. Log level is read from the
    ``HARDWARE_HUNTER_LOG_LEVEL`` env var (default ``info``);
    :func:`configure_log_level` overrides it from ``config.yaml`` at
    daemon startup.
    """
    _configure_root()
    qualified = name if name.startswith(_LOGGER_ROOT) else f"{_LOGGER_ROOT}.{name}"
    return logging.getLogger(qualified)


def configure_log_level(level: str) -> None:
    """Reconfigure the root log level — called by the config loader."""
    _configure_root(level_name=level)
