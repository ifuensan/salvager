"""Tests for the .env loader — Story 2.6 (FR49 + NFR-S1)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from salvager.config.env import (
    ENV_AUTH_EXIT_CODE,
    get_env_settings,
    load_env_or_exit,
    reset_env_cache,
)

VALID_ENV = textwrap.dedent(
    """\
    TELEGRAM_BOT_TOKEN=secret-bot-token
    TELEGRAM_CHAT_ID=12345
    GEMINI_API_KEY=secret-gemini
    EBAY_APP_ID=secret-ebay-app
    EBAY_CERT_ID=secret-ebay-cert
    EBAY_DEV_ID=secret-ebay-dev
    TINYFISH_API_KEY=secret-tinyfish
    """
)


@pytest.fixture(autouse=True)
def _isolate_env_cache_and_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Scrub real-environment credentials and the loader's cache between
    tests so each case loads only what its ``.env`` fixture provides."""
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "GEMINI_API_KEY",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
        "EBAY_DEV_ID",
        "TINYFISH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_env_cache()
    yield
    reset_env_cache()


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(VALID_ENV, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────
# Required fields + SecretStr masking
# ─────────────────────────────────────────────────────────────────────────


def test_valid_env_loads_all_required_fields(env_file: Path) -> None:
    settings = get_env_settings(env_file)
    assert settings.TELEGRAM_CHAT_ID == 12345
    assert isinstance(settings.TELEGRAM_BOT_TOKEN, SecretStr)
    assert settings.TELEGRAM_BOT_TOKEN.get_secret_value() == "secret-bot-token"
    assert settings.GEMINI_API_KEY is not None
    assert settings.GEMINI_API_KEY.get_secret_value() == "secret-gemini"


def test_repr_masks_secrets(env_file: Path) -> None:
    settings = get_env_settings(env_file)
    rendered = repr(settings)
    assert "secret-bot-token" not in rendered
    assert "secret-gemini" not in rendered
    # SecretStr renders as "**********"
    assert "**********" in rendered


def test_model_dump_json_masks_secrets(env_file: Path) -> None:
    """The default JSON dump must NOT leak cleartext — even when an
    operator accidentally pipes the settings object to logs."""
    settings = get_env_settings(env_file)
    payload = json.loads(settings.model_dump_json())
    for value in payload.values():
        if isinstance(value, str):
            assert "secret-" not in value


def test_str_masks_secrets(env_file: Path) -> None:
    settings = get_env_settings(env_file)
    rendered = str(settings)
    assert "secret-bot-token" not in rendered
    assert "secret-gemini" not in rendered


# ─────────────────────────────────────────────────────────────────────────
# Singleton — FR49 (no hot-reload)
# ─────────────────────────────────────────────────────────────────────────


def test_second_call_returns_cached_instance(env_file: Path) -> None:
    first = get_env_settings(env_file)
    second = get_env_settings(env_file)
    assert first is second


def test_reset_env_cache_forces_rehydration(env_file: Path) -> None:
    first = get_env_settings(env_file)
    reset_env_cache()
    second = get_env_settings(env_file)
    assert first is not second


def test_cache_keys_on_env_file_path(env_file: Path, tmp_path: Path) -> None:
    """A different path is loaded independently — useful for tests that
    parametrize across multiple env files, but the daemon never does this."""
    other = tmp_path / "other.env"
    other.write_text(VALID_ENV.replace("12345", "67890"), encoding="utf-8")
    first = get_env_settings(env_file)
    second = get_env_settings(other)
    assert first.TELEGRAM_CHAT_ID == 12345
    assert second.TELEGRAM_CHAT_ID == 67890


# ─────────────────────────────────────────────────────────────────────────
# Missing-field handling — exits with code 4 (auth)
# ─────────────────────────────────────────────────────────────────────────


def test_missing_required_var_raises_validation_error(tmp_path: Path) -> None:
    incomplete = tmp_path / ".env"
    incomplete.write_text(
        "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\nGEMINI_API_KEY=g\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        get_env_settings(incomplete)


def test_load_env_or_exit_renders_locked_template_on_missing_var(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    incomplete = tmp_path / ".env"
    incomplete.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        load_env_or_exit(incomplete)

    assert excinfo.value.code == ENV_AUTH_EXIT_CODE
    captured = capsys.readouterr()
    assert "missing required env var:" in captured.err
    assert "see .env.example" in captured.err
    # Names the first missing var, not all of them — operators fix one
    # at a time. GEMINI_API_KEY became optional when the Claude adapter
    # landed (NFR-I3: provider-specific keys are validated by the
    # composer at compose time, not by the env schema), so the first
    # missing-required field is now EBAY_APP_ID.
    assert "EBAY_APP_ID" in captured.err


def test_load_env_or_exit_returns_settings_on_success(env_file: Path) -> None:
    settings = load_env_or_exit(env_file)
    assert settings.TELEGRAM_CHAT_ID == 12345


# ─────────────────────────────────────────────────────────────────────────
# NFR-S1 — log lines must never carry secret values
# ─────────────────────────────────────────────────────────────────────────


def test_log_env_loaded_emits_names_only_via_subprocess(
    env_file: Path,
) -> None:
    """The structured-log line for env_loaded must contain the variable
    NAMES but NEVER their values. Runs via subprocess so the structured
    logger's stdout-writing handler can be captured cleanly."""
    snippet = f"""
        from salvager.config.env import (
            get_env_settings,
            log_env_loaded,
            reset_env_cache,
        )
        reset_env_cache()
        settings = get_env_settings({str(env_file)!r})
        log_env_loaded(settings)
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env.update({k: v for k, v in os.environ.items() if k not in env})  # keep PATH etc.
    # Force-strip any real credentials from the subprocess environment so
    # only the test fixture's .env values populate EnvSettings.
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "GEMINI_API_KEY",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
        "EBAY_DEV_ID",
        "TINYFISH_API_KEY",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    env_lines = [r for r in lines if r.get("event") == "env_loaded"]
    assert len(env_lines) == 1
    record = env_lines[0]
    # Names present.
    assert "TELEGRAM_BOT_TOKEN" in record["vars_loaded"]
    assert "GEMINI_API_KEY" in record["vars_loaded"]
    # Values absent — anywhere in the serialized record.
    serialized = json.dumps(record)
    assert "secret-bot-token" not in serialized
    assert "secret-gemini" not in serialized
    assert "secret-tinyfish" not in serialized


def test_unknown_env_var_is_ignored(tmp_path: Path) -> None:
    """extra='ignore' so unrelated env vars (PATH, HOME, etc.) don't crash."""
    env_path = tmp_path / ".env"
    env_path.write_text(VALID_ENV + "RANDOM_THIRDPARTY_KEY=xyz\n", encoding="utf-8")
    settings = get_env_settings(env_path)
    assert settings.TELEGRAM_CHAT_ID == 12345
