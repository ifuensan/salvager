"""Structured JSON Lines logger for salvager — NFR-O1 / NFR-R5.

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
- Default output format is ``json`` (NFR-O1). The opt-in ``pretty`` format
  renders the same record content as a human-readable single line for
  interactive ``uv run salvager`` debugging.
- ``SALVAGER_LOG_LEVEL`` / ``SALVAGER_LOG_FORMAT`` env vars override at start.
- :func:`configure_log_level` / :func:`configure_log_format` are called by
  the ``config.yaml`` loader at daemon startup.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

_LOGGER_ROOT = "salvager"
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


# ANSI escape sequences for the pretty formatter. Applied only when the
# emit-time check confirms stdout is a TTY and NO_COLOR is unset.
_ANSI_RESET = "\x1b[0m"
_ANSI_BY_LEVEL: dict[str, str] = {
    "debug": "\x1b[38;5;245m",  # dim grey
    "info": "\x1b[36m",  # cyan
    "warn": "\x1b[33m",  # yellow
    "error": "\x1b[31m",  # red
}
_ANSI_EVENT = "\x1b[1;36m"  # bold cyan
# Values containing whitespace or quotes are wrapped in double quotes so
# the key=value boundary survives copy/paste into another shell.
_PRETTY_QUOTE_RE = re.compile(r'[\s"\\]')


def _format_pretty_value(value: Any) -> str:
    text = str(value)
    if _PRETTY_QUOTE_RE.search(text):
        # Backslash first so the escapes we insert below don't get re-escaped.
        # Newline / CR / tab MUST become visible sequences — otherwise a value
        # like "line1\nline2" would break the one-line-per-record invariant
        # the pretty format relies on for grep / tee consumers.
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return text


class PrettyConsoleFormatter(logging.Formatter):
    """Single-line human-readable formatter for interactive sessions.

    Renders as ``HH:MM:SS  LEVEL  event  key=value …`` on one line, with
    the formatted traceback appended on indented continuation lines when
    ``record.exc_info`` is set. ``None`` extras are dropped; values
    containing whitespace are quoted.

    ANSI level/event colouring is emitted only when, at emit time,
    ``sys.stdout.isatty()`` is true and the ``NO_COLOR`` env var is unset.
    The check runs per record because ``_DynamicStdoutHandler`` re-resolves
    stdout per emit (pytest swaps it between tests).
    """

    def format(self, record: logging.LogRecord) -> str:
        level_name = _LEVEL_NAMES.get(record.levelno, record.levelname.lower())
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        event = record.getMessage()

        use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        if use_color:
            level_label = (
                f"{_ANSI_BY_LEVEL.get(level_name, '')}{level_name.upper():<5}{_ANSI_RESET}"
            )
            event_label = f"{_ANSI_EVENT}{event}{_ANSI_RESET}"
        else:
            level_label = f"{level_name.upper():<5}"
            event_label = event

        extras: list[str] = []
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_FIELDS or key.startswith("_"):
                continue
            if value is None:
                continue
            extras.append(f"{key}={_format_pretty_value(value)}")

        line = f"{timestamp}  {level_label}  {event_label}"
        if extras:
            line += "  " + " ".join(extras)

        if record.exc_info:
            tb_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
            indented = "\n".join(f"    {tb_line}" for tb_line in tb_text.splitlines())
            line += f"\n{indented}"
        return line


# Lookup table maps the config-yaml format name to the formatter factory.
_FORMATTER_FACTORIES: dict[str, type[logging.Formatter]] = {
    "json": JsonLineFormatter,
    "pretty": PrettyConsoleFormatter,
}


def _build_formatter(format_name: str) -> logging.Formatter:
    try:
        factory = _FORMATTER_FACTORIES[format_name]
    except KeyError as exc:
        valid = ", ".join(sorted(_FORMATTER_FACTORIES))
        raise ValueError(f"unknown log format {format_name!r}; expected one of: {valid}") from exc
    return factory()


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


def _configure_root(
    level_name: str | None = None,
    format_name: str | None = None,
) -> None:
    """Idempotent root-logger setup. Safe to call repeatedly; the level
    and format arguments, if provided, override the current settings."""
    global _CONFIGURED
    root = logging.getLogger(_LOGGER_ROOT)

    if _CONFIGURED:
        if level_name is not None:
            root.setLevel(_resolve_level(level_name))
        if format_name is not None:
            new_formatter = _build_formatter(format_name)
            for handler in root.handlers:
                handler.setFormatter(new_formatter)
        return

    root.handlers.clear()
    root.propagate = False

    resolved_format = format_name or os.environ.get("SALVAGER_LOG_FORMAT", "json")
    handler = _DynamicStdoutHandler()
    handler.setFormatter(_build_formatter(resolved_format))
    root.addHandler(handler)

    resolved_level = level_name or os.environ.get("SALVAGER_LOG_LEVEL", "info")
    root.setLevel(_resolve_level(resolved_level))

    sys.excepthook = _excepthook
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured structured-JSON logger under ``salvager.<name>``.

    The root configuration is idempotent. Log level is read from the
    ``SALVAGER_LOG_LEVEL`` env var (default ``info``);
    :func:`configure_log_level` overrides it from ``config.yaml`` at
    daemon startup.
    """
    _configure_root()
    qualified = name if name.startswith(_LOGGER_ROOT) else f"{_LOGGER_ROOT}.{name}"
    return logging.getLogger(qualified)


def configure_log_level(level: str) -> None:
    """Reconfigure the root log level — called by the config loader."""
    _configure_root(level_name=level)


def configure_log_format(format_name: str) -> None:
    """Reconfigure the root log format — called by the config loader / CLI.

    Raises ``ValueError`` when ``format_name`` is not one of the supported
    names (``json`` / ``pretty``). Safe to call before or after
    :func:`_configure_root` has run for the first time.
    """
    _configure_root(format_name=format_name)
