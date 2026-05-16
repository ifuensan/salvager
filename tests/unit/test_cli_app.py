"""Tests for the typer CLI skeleton — Story 1.8 (FR39 + FR48)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from hardware_hunter.cli.app import app


@pytest.fixture
def runner() -> CliRunner:
    """typer/click runner — stdout and stderr are returned separately."""
    return CliRunner()


# ─────────────────────────────────────────────────────────────────────────
# `--help` surface — FR39 placeholder mounts
# ─────────────────────────────────────────────────────────────────────────


def test_help_lists_all_placeholder_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    for name in (
        "init",
        "login",
        "validate-wishlist",
        "validate-config",
        "test-search",
        "explain",
        "phase2",
        "audit",
        "health",
        "logs",
        "wishlist",
        "version",
    ):
        assert name in out, f"missing subcommand {name!r} in --help output"


def test_short_help_flag_works(runner: CliRunner) -> None:
    result = runner.invoke(app, ["-h"])
    assert result.exit_code == 0
    assert "hardware-hunter" in result.stdout.lower()


# ─────────────────────────────────────────────────────────────────────────
# `version` subcommand
# ─────────────────────────────────────────────────────────────────────────


def test_version_human_format(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    # Human format: "hardware-hunter <semver> (<commit>)"
    assert "hardware-hunter" in result.stdout
    # The version is read from pyproject.toml at runtime; assert on the
    # shape (semver MAJOR.MINOR.PATCH) rather than a hard-coded value
    # so version bumps don't break this test.
    import re

    assert re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)


def test_version_json_format_is_parseable(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert set(payload.keys()) == {"version", "commit"}
    # Match a semver MAJOR.MINOR.PATCH shape — see test_version_human_format
    # for why this is a regex match rather than a hard-coded constant.
    import re

    assert re.fullmatch(r"\d+\.\d+\.\d+", payload["version"]) is not None
    assert isinstance(payload["commit"], str)
    assert payload["commit"]  # non-empty


def test_version_commit_from_env_var(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Dockerfile bakes the commit at build time via this env var."""
    monkeypatch.setenv("HARDWARE_HUNTER_COMMIT", "deadbeef")
    result = runner.invoke(app, ["version", "--format", "json"])
    payload = json.loads(result.stdout.strip())
    assert payload["commit"] == "deadbeef"


def test_version_unknown_format_exits_with_usage_code(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version", "--format", "yaml"])
    assert result.exit_code == 2  # FR48 usage error
    assert "error" in result.stderr.lower()


# ─────────────────────────────────────────────────────────────────────────
# Bare invocation → daemon (FR39)
# ─────────────────────────────────────────────────────────────────────────


def test_bare_invocation_without_env_exits_missing_creds() -> None:
    """The bare daemon requires .env credentials; missing → exit 4.

    Runs via subprocess for the same reason as below (the structured
    logger writes to ``sys.stdout`` directly). With no ``.env`` file
    in the test cwd, :func:`load_env_or_exit` renders the locked
    error template and exits with the missing-credentials code per
    Story 2.6 / FR48.
    """
    # Scrub real-environment credentials so the loader sees an empty .env.
    scrubbed_env = {
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
        }
    }
    scrubbed_env["HARDWARE_HUNTER_COMMIT"] = "test-sha"

    result = subprocess.run(
        [sys.executable, "-m", "hardware_hunter"],
        capture_output=True,
        text=True,
        check=False,
        env=scrubbed_env,
    )
    assert result.returncode == 4, result.stderr
    assert "error" in result.stderr.lower()


# ─────────────────────────────────────────────────────────────────────────
# Placeholders — exit 1 with hint pointing at ROADMAP
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["logs"],
        ["wishlist", "list"],
    ],
)
def test_placeholder_commands_exit_with_code_1(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 1, (
        f"{argv!r} expected exit code 1 (placeholder); got {result.exit_code}"
    )
    assert "not yet implemented" in result.stderr
    assert "ROADMAP" in result.stderr


def test_login_wallapop_in_non_tty_exits_1(runner: CliRunner) -> None:
    """The login command is wired and refuses a non-interactive context.

    CliRunner provides a non-TTY stdin, so this also confirms the
    Story 2.9 non-TTY guard fires through the typer boundary.
    """
    result = runner.invoke(app, ["login", "wallapop", "--data-dir", "/tmp"])
    assert result.exit_code == 1
    assert "interactive terminal" in result.stderr


def test_login_ebay_missing_ru_name_exits_usage(runner: CliRunner) -> None:
    """``login ebay`` requires ``--ru-name``; omitting it is a usage error.

    Confirms the Story 2.10 command is mounted with its required option
    without needing a populated ``.env`` (the env load happens after
    typer's argument parsing). We assert only the exit code — the
    rendered error text is rich-width-dependent and truncates in CI.
    """
    result = runner.invoke(app, ["login", "ebay"])
    assert result.exit_code == 2  # FR48 usage error
    assert "usage:" in result.stderr.lower()


# ─────────────────────────────────────────────────────────────────────────
# FR48: unknown subcommand → exit 2 (usage error)
# ─────────────────────────────────────────────────────────────────────────


def test_unknown_subcommand_exits_with_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["nonexistent-command"])
    assert result.exit_code == 2


def test_unknown_flag_exits_with_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version", "--bogus-flag"])
    assert result.exit_code == 2
