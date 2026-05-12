"""Tests for ``hardware-hunter validate-config`` — Story 2.7."""

from __future__ import annotations

import json
import os
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from hardware_hunter.cli.app import app
from hardware_hunter.config.env import reset_env_cache

VALID_ENV = textwrap.dedent(
    """\
    TELEGRAM_BOT_TOKEN=t
    TELEGRAM_CHAT_ID=12345
    GEMINI_API_KEY=g
    EBAY_APP_ID=a
    EBAY_CERT_ID=c
    EBAY_DEV_ID=d
    TINYFISH_API_KEY=tf
    """
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scrub real-environment credentials and the env cache so tests are
    isolated from the developer's actual shell."""
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "GEMINI_API_KEY",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
        "EBAY_DEV_ID",
        "TINYFISH_API_KEY",
        "HERMES_URL",
        "HERMES_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_env_cache()
    yield
    reset_env_cache()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def valid_setup(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "config.yaml"
    config.write_text("logging:\n  level: info\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(VALID_ENV, encoding="utf-8")
    return config, env


def _invoke(
    runner: CliRunner,
    config: Path,
    env: Path,
    *extra: str,
) -> Result:
    """CliRunner.invoke with the standard --config-path/--env-path pair."""
    # CliRunner inherits the parent process's environ — reset_env_cache
    # is autouse, but the subprocess-loaded env may still carry stale
    # values. We explicitly scrub the harness env here too.
    env_for_subprocess = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "GEMINI_API_KEY",
            "EBAY_APP_ID",
            "EBAY_CERT_ID",
            "EBAY_DEV_ID",
            "TINYFISH_API_KEY",
            "HERMES_URL",
            "HERMES_API_KEY",
        }
    }
    return runner.invoke(
        app,
        ["validate-config", "--config-path", str(config), "--env-path", str(env), *extra],
        env=env_for_subprocess,
    )


# ─────────────────────────────────────────────────────────────────────────
# Success — human + JSON
# ─────────────────────────────────────────────────────────────────────────


def test_valid_config_and_env_returns_success(
    runner: CliRunner, valid_setup: tuple[Path, Path]
) -> None:
    config, env = valid_setup
    result = _invoke(runner, config, env)
    assert result.exit_code == 0, result.stderr
    assert "config.yaml + .env are valid" in result.stdout


def test_valid_config_and_env_json_output(
    runner: CliRunner, valid_setup: tuple[Path, Path]
) -> None:
    config, env = valid_setup
    result = _invoke(runner, config, env, "--format", "json")
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == {"valid": True}


# ─────────────────────────────────────────────────────────────────────────
# Config validation — exit 3, section + field surfaced
# ─────────────────────────────────────────────────────────────────────────


def test_invalid_config_field_exits_with_code_3(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("phase2:\n  reconciliation_tolerance_pct: 150\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(VALID_ENV, encoding="utf-8")

    result = _invoke(runner, config, env)
    assert result.exit_code == 3
    assert "[phase2.reconciliation_tolerance_pct]" in result.stderr


def test_invalid_config_json_envelope(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("schedule:\n  wallapop_minutes: -5\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(VALID_ENV, encoding="utf-8")

    result = _invoke(runner, config, env, "--format", "json")
    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr.strip())
    assert payload["exit_code"] == 3
    assert "schedule.wallapop_minutes" in payload["message"]


def test_unknown_section_caught_as_config_error(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("scheduel:\n  wallapop_minutes: 15\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(VALID_ENV, encoding="utf-8")

    result = _invoke(runner, config, env)
    assert result.exit_code == 3
    assert "scheduel" in result.stderr


def test_malformed_config_yaml_exits_3(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("schedule:\n  wallapop_minutes: [oops\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(VALID_ENV, encoding="utf-8")

    result = _invoke(runner, config, env)
    assert result.exit_code == 3
    assert "malformed YAML" in result.stderr


# ─────────────────────────────────────────────────────────────────────────
# .env validation — exit 4 (auth)
# ─────────────────────────────────────────────────────────────────────────


def test_missing_env_var_exits_with_code_4(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")

    result = _invoke(runner, config, env)
    assert result.exit_code == 4
    err = result.stderr
    assert "missing required env var:" in err
    assert "see .env.example" in err


def test_missing_env_var_json_envelope(runner: CliRunner, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")

    result = _invoke(runner, config, env, "--format", "json")
    assert result.exit_code == 4
    payload = json.loads(result.stderr.strip())
    assert payload["exit_code"] == 4
    assert "missing required env var:" in payload["message"]


# ─────────────────────────────────────────────────────────────────────────
# File-not-found — exit 1
# ─────────────────────────────────────────────────────────────────────────


def test_missing_config_file_exits_1(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    env = tmp_path / ".env"
    env.write_text(VALID_ENV, encoding="utf-8")

    result = _invoke(runner, missing, env)
    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert "hardware-hunter init" in result.stderr


# ─────────────────────────────────────────────────────────────────────────
# Ordering — config errors take priority over env errors
# ─────────────────────────────────────────────────────────────────────────


def test_config_error_takes_priority_over_missing_env(runner: CliRunner, tmp_path: Path) -> None:
    """When BOTH config and env are broken, the config error wins —
    operators fix one layer at a time, and config is the layer they
    can debug with `validate-config` alone."""
    config = tmp_path / "config.yaml"
    config.write_text("schedule:\n  wallapop_minutes: -5\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")  # missing fields

    result = _invoke(runner, config, env)
    assert result.exit_code == 3  # config validation, not env auth
    assert "schedule" in result.stderr


# ─────────────────────────────────────────────────────────────────────────
# --format validation
# ─────────────────────────────────────────────────────────────────────────


def test_unknown_format_exits_with_code_2(
    runner: CliRunner, valid_setup: tuple[Path, Path]
) -> None:
    config, env = valid_setup
    result = _invoke(runner, config, env, "--format", "yaml")
    assert result.exit_code == 2
    assert "unknown --format" in result.stderr.lower()
