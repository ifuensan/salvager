"""Tests for the structured JSON Lines logger — NFR-O1 / NFR-R5.

Subprocess isolation keeps the module-level ``_CONFIGURED`` flag and the
process-wide ``sys.excepthook`` patch from leaking across tests. Each test
spawns a fresh interpreter, runs an inline snippet, and asserts on stdout.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap


def _run(snippet: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a Python snippet in a fresh interpreter and return the completed process."""
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _json_lines(stdout: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_logger_emits_json_with_standard_fields() -> None:
    result = _run(
        """
        from salvager.observability.logging import get_logger
        get_logger("test").info("poll_started")
        """
    )
    assert result.returncode == 0, result.stderr
    records = _json_lines(result.stdout)
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "info"
    assert record["event"] == "poll_started"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", str(record["ts"]))


def test_logger_warn_alias_not_warning() -> None:
    result = _run(
        """
        from salvager.observability.logging import get_logger
        get_logger("test").warning("slow_response")
        """
    )
    assert result.returncode == 0, result.stderr
    records = _json_lines(result.stdout)
    assert records[0]["level"] == "warn"


def test_logger_includes_extras() -> None:
    result = _run(
        """
        from salvager.observability.logging import get_logger
        get_logger("test").info(
            "listing_evaluated",
            extra={
                "entry": "wd_red_plus_4tb",
                "marketplace": "wallapop",
                "listing_id": "abc123",
                "latency_ms": 842,
            },
        )
        """
    )
    assert result.returncode == 0, result.stderr
    record = _json_lines(result.stdout)[0]
    assert record["entry"] == "wd_red_plus_4tb"
    assert record["marketplace"] == "wallapop"
    assert record["listing_id"] == "abc123"
    assert record["latency_ms"] == 842


def test_level_filtering_via_env() -> None:
    result = _run(
        """
        from salvager.observability.logging import get_logger
        log = get_logger("test")
        log.debug("hidden_event")
        log.warning("visible_event")
        """,
        env_extra={"SALVAGER_LOG_LEVEL": "warn"},
    )
    assert result.returncode == 0, result.stderr
    records = _json_lines(result.stdout)
    assert len(records) == 1
    assert records[0]["event"] == "visible_event"


def test_configure_log_level_overrides() -> None:
    result = _run(
        """
        from salvager.observability.logging import configure_log_level, get_logger
        log = get_logger("test")
        log.debug("hidden_before")
        configure_log_level("debug")
        log.debug("visible_after")
        """
    )
    assert result.returncode == 0, result.stderr
    records = _json_lines(result.stdout)
    events = [r["event"] for r in records]
    assert events == ["visible_after"]


def test_unhandled_exception_emits_structured_error_and_exits_nonzero() -> None:
    result = _run(
        """
        from salvager.observability.logging import get_logger
        get_logger("test")  # configure root + install excepthook
        raise RuntimeError("boom")
        """
    )
    assert result.returncode != 0
    records = _json_lines(result.stdout)
    assert len(records) == 1
    record = records[0]
    assert record["level"] == "error"
    assert record["event"] == "unhandled_exception"
    assert record["error_class"] == "RuntimeError"
    assert record["error_message"] == "boom"
    assert "RuntimeError: boom" in str(record["stack"])


def test_keyboard_interrupt_uses_default_excepthook() -> None:
    result = _run(
        """
        from salvager.observability.logging import get_logger
        get_logger("test")
        raise KeyboardInterrupt()
        """
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "KeyboardInterrupt" in result.stderr


def test_output_is_single_line_per_record() -> None:
    """Pipe-to-jq compatibility: every emitted record is exactly one line of valid JSON."""
    result = _run(
        """
        from salvager.observability.logging import get_logger
        log = get_logger("test")
        log.info("first")
        log.info("second", extra={"entry": "wd_red_plus_4tb"})
        log.error("third", extra={"error_class": "ValueError"})
        """
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)
        assert "\n" not in line


# ─────────────────────────────────────────────────────────────────────────
# Pretty format — interactive-only single-line renderer + ANSI gating
# ─────────────────────────────────────────────────────────────────────────


def _pretty_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.strip()]


def test_pretty_formatter_renders_single_line() -> None:
    """One info record produces one line shaped HH:MM:SS  LEVEL  event  k=v …."""
    result = _run(
        """
        from salvager.observability.logging import configure_log_format, get_logger
        configure_log_format("pretty")
        get_logger("test").info(
            "listing_evaluated",
            extra={"entry": "wd_red_plus_4tb", "latency_ms": 842},
        )
        """,
    )
    assert result.returncode == 0, result.stderr
    lines = _pretty_lines(result.stdout)
    assert len(lines) == 1
    line = lines[0]
    assert re.match(r"\d{2}:\d{2}:\d{2}  INFO ", line)
    assert "listing_evaluated" in line
    assert "entry=wd_red_plus_4tb" in line
    assert "latency_ms=842" in line


def test_pretty_drops_none_extras() -> None:
    result = _run(
        """
        from salvager.observability.logging import configure_log_format, get_logger
        configure_log_format("pretty")
        get_logger("test").info(
            "evt",
            extra={"present": "yes", "absent": None},
        )
        """,
    )
    assert result.returncode == 0, result.stderr
    line = _pretty_lines(result.stdout)[0]
    assert "present=yes" in line
    assert "absent" not in line


def test_pretty_quotes_values_with_whitespace() -> None:
    result = _run(
        """
        from salvager.observability.logging import configure_log_format, get_logger
        configure_log_format("pretty")
        get_logger("test").info(
            "evt",
            extra={"detail": "has spaces here", "simple": "noquote"},
        )
        """,
    )
    assert result.returncode == 0, result.stderr
    line = _pretty_lines(result.stdout)[0]
    assert 'detail="has spaces here"' in line
    assert "simple=noquote" in line


def test_pretty_no_ansi_when_stdout_is_not_a_tty() -> None:
    """Pipe target (subprocess capture is a pipe, not a TTY) → no ANSI codes."""
    result = _run(
        """
        from salvager.observability.logging import configure_log_format, get_logger
        configure_log_format("pretty")
        get_logger("test").info("evt", extra={"k": "v"})
        """,
    )
    assert result.returncode == 0, result.stderr
    assert "\x1b[" not in result.stdout


def test_pretty_no_color_env_var_suppresses_ansi() -> None:
    """NO_COLOR forces plain output even when stdout would otherwise be a TTY."""
    result = _run(
        """
        import sys
        # Pretend stdout is a TTY so the only suppression path is NO_COLOR.
        sys.stdout.isatty = lambda: True
        from salvager.observability.logging import configure_log_format, get_logger
        configure_log_format("pretty")
        get_logger("test").info("evt", extra={"k": "v"})
        """,
        env_extra={"NO_COLOR": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "\x1b[" not in result.stdout


def test_pretty_appends_indented_traceback_on_exception() -> None:
    result = _run(
        """
        from salvager.observability.logging import configure_log_format, get_logger
        configure_log_format("pretty")
        log = get_logger("test")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log.exception("boom_event", extra={"detail": "simulated"})
        """,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "boom_event" in out
    assert "detail=simulated" in out
    # Traceback frames are indented under the primary line.
    assert "    Traceback (most recent call last):" in out
    assert "    RuntimeError: boom" in out


def test_configure_log_format_rejects_unknown_value() -> None:
    result = _run(
        """
        import sys
        from salvager.observability.logging import configure_log_format
        try:
            configure_log_format("verbose")
        except ValueError as exc:
            print(f"ERR:{exc}", file=sys.stderr)
        """,
    )
    assert result.returncode == 0
    assert "ERR:unknown log format 'verbose'" in result.stderr


def test_env_var_selects_pretty_format() -> None:
    """SALVAGER_LOG_FORMAT=pretty makes the first record render as pretty."""
    result = _run(
        """
        from salvager.observability.logging import get_logger
        get_logger("test").info("evt", extra={"k": "v"})
        """,
        env_extra={"SALVAGER_LOG_FORMAT": "pretty"},
    )
    assert result.returncode == 0, result.stderr
    line = _pretty_lines(result.stdout)[0]
    assert "evt" in line
    assert line[0:8].count(":") == 2  # HH:MM:SS prefix
    # Definitely not JSON.
    try:
        json.loads(line)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("pretty output unexpectedly parsed as JSON")


# ─────────────────────────────────────────────────────────────────────────
# Deterministic in-process formatter test — pins the exact rendered shape
# so any drift in the pretty UX becomes a visible diff in a PR.
# ─────────────────────────────────────────────────────────────────────────


def test_pretty_formatter_pinned_shape() -> None:
    """In-process render of a representative record — exact-string assert.

    Bypasses subprocess isolation because we construct the LogRecord by
    hand with a known epoch and the formatter is a pure function.
    """
    import logging
    from datetime import datetime

    from salvager.observability.logging import PrettyConsoleFormatter

    # 2026-05-25 14:30:00 local time. ``strftime`` reads the local zone,
    # so we feed the formatter the corresponding epoch.
    fixed_local = datetime(2026, 5, 25, 14, 30, 0)
    fixed_epoch = fixed_local.timestamp()

    record = logging.LogRecord(
        name="salvager.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="poll_cycle_complete",
        args=(),
        exc_info=None,
    )
    record.created = fixed_epoch
    record.marketplace = "wallapop"
    record.result_count = 3
    record.new_count = 0
    record.detail = "two reserved comps"  # whitespace -> quoted
    record.missing_key = None  # dropped

    rendered = PrettyConsoleFormatter().format(record)

    assert rendered == (
        "14:30:00  INFO   poll_cycle_complete  "
        "marketplace=wallapop result_count=3 new_count=0 "
        'detail="two reserved comps"'
    )
