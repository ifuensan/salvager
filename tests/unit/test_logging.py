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
        from hardware_hunter.observability.logging import get_logger
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
        from hardware_hunter.observability.logging import get_logger
        get_logger("test").warning("slow_response")
        """
    )
    assert result.returncode == 0, result.stderr
    records = _json_lines(result.stdout)
    assert records[0]["level"] == "warn"


def test_logger_includes_extras() -> None:
    result = _run(
        """
        from hardware_hunter.observability.logging import get_logger
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
        from hardware_hunter.observability.logging import get_logger
        log = get_logger("test")
        log.debug("hidden_event")
        log.warning("visible_event")
        """,
        env_extra={"HARDWARE_HUNTER_LOG_LEVEL": "warn"},
    )
    assert result.returncode == 0, result.stderr
    records = _json_lines(result.stdout)
    assert len(records) == 1
    assert records[0]["event"] == "visible_event"


def test_configure_log_level_overrides() -> None:
    result = _run(
        """
        from hardware_hunter.observability.logging import configure_log_level, get_logger
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
        from hardware_hunter.observability.logging import get_logger
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
        from hardware_hunter.observability.logging import get_logger
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
        from hardware_hunter.observability.logging import get_logger
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
