"""JSON output round-trip — Story 3.15 (NFR-O2 / FR48).

Every CLI subcommand that advertises ``--format json`` must produce
output where:

  1. Every emitted line on stdout (when --format=json is selected)
     parses cleanly through ``json.loads``.
  2. Every string field whose name suggests a timestamp (``*_at``,
     ``timestamp``, ``ts``) parses through ``datetime.fromisoformat``.
  3. The error envelope on a non-zero exit code also parses — the
     "machine-readable output" promise applies to failure paths too.

The test parameterises over real fixture inputs (a valid wishlist,
a missing wishlist, a valid config + .env) so the JSON shape is
exercised end-to-end through the actual command handlers, not a
mock layer.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hardware_hunter.cli.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_WISHLIST = REPO_ROOT / "wishlist.example.yaml"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"
EXAMPLE_ENV = REPO_ROOT / ".env.example"

# Heuristic: any string field with one of these substrings in its key
# must parse as an ISO 8601 timestamp via ``datetime.fromisoformat``.
_TIMESTAMP_KEY_HINTS: tuple[str, ...] = (
    "_at",
    "_ts",
    "timestamp",
    "rendered",
    "occurred",
    "fetched",
)


def _walk_timestamp_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Walk arbitrary JSON, yield ``(json_path, value)`` for every string
    whose key matches one of the timestamp hints."""
    hits: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            sub_path = f"{path}.{k}" if path else k
            if isinstance(v, str) and any(hint in k for hint in _TIMESTAMP_KEY_HINTS):
                hits.append((sub_path, v))
            else:
                hits.extend(_walk_timestamp_strings(v, sub_path))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            hits.extend(_walk_timestamp_strings(item, f"{path}[{i}]"))
    return hits


def _parse_lines(stdout: str) -> list[dict[str, Any]]:
    """Parse every non-empty stdout line as a JSON object."""
    parsed: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        loaded = json.loads(raw)
        assert isinstance(loaded, dict), (
            f"expected JSON object per line, got {type(loaded).__name__}: {raw!r}"
        )
        parsed.append(loaded)
    return parsed


def _assert_timestamps_parse(records: list[dict[str, Any]]) -> None:
    for record in records:
        for json_path, raw in _walk_timestamp_strings(record):
            try:
                datetime.fromisoformat(raw)
            except ValueError as exc:
                pytest.fail(f"field {json_path}={raw!r} is not a valid ISO 8601 timestamp: {exc}")


# ─────────────────────────────────────────────────────────────────────────
# Per-command fixtures
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def config_dir_with_valid_examples(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    target.mkdir()
    shutil.copy(EXAMPLE_WISHLIST, target / "wishlist.yaml")
    shutil.copy(EXAMPLE_CONFIG, target / "config.yaml")
    # The example .env scaffolds with placeholder values; copy as-is —
    # validate-config will report the placeholder error (still as JSON).
    shutil.copy(EXAMPLE_ENV, target / ".env")
    return target


# ─────────────────────────────────────────────────────────────────────────
# version --format json
# ─────────────────────────────────────────────────────────────────────────


def test_version_json_output_is_parseable() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    records = _parse_lines(result.stdout)
    assert len(records) == 1
    assert "version" in records[0]
    assert "commit" in records[0]
    _assert_timestamps_parse(records)


# ─────────────────────────────────────────────────────────────────────────
# validate-wishlist --format json
# ─────────────────────────────────────────────────────────────────────────


def test_validate_wishlist_json_success_is_parseable(
    config_dir_with_valid_examples: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "validate-wishlist",
            "--path",
            str(config_dir_with_valid_examples / "wishlist.yaml"),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    records = _parse_lines(result.stdout)
    assert len(records) == 1
    assert records[0]["valid"] is True
    assert isinstance(records[0]["entry_count"], int)
    _assert_timestamps_parse(records)


def test_validate_wishlist_json_failure_envelope_is_parseable(tmp_path: Path) -> None:
    """Even on exit ≠ 0 the JSON envelope round-trips — the
    "machine-readable output" promise applies to error paths too.

    Per UX-DR21 the success envelope goes to stdout, the error
    envelope to stderr; both must parse cleanly through ``json.loads``.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "validate-wishlist",
            "--path",
            str(tmp_path / "missing.yaml"),
            "--format",
            "json",
        ],
    )
    assert result.exit_code != 0
    records = _parse_lines(result.stderr)
    assert records, "error path must emit a JSON envelope on stderr (UX-DR21)"
    assert "error" in records[0] or "message" in records[0]
    assert "exit_code" in records[0]
    assert records[0]["exit_code"] in {1, 2, 3, 4, 5}
    _assert_timestamps_parse(records)


# ─────────────────────────────────────────────────────────────────────────
# validate-config --format json
# ─────────────────────────────────────────────────────────────────────────


def test_validate_config_json_output_is_parseable(
    config_dir_with_valid_examples: Path,
) -> None:
    """The example .env carries placeholder credentials, so the command
    typically exits 4 (auth) with a JSON error envelope. Either outcome
    is valid for this story — both must round-trip through json.loads."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "validate-config",
            "--config-path",
            str(config_dir_with_valid_examples / "config.yaml"),
            "--env-path",
            str(config_dir_with_valid_examples / ".env"),
            "--format",
            "json",
        ],
    )
    # Success → stdout; failure (e.g. .env still carrying placeholders) → stderr.
    stdout_records = _parse_lines(result.stdout)
    stderr_records = _parse_lines(result.stderr)
    records = stdout_records + stderr_records
    assert records, "validate-config --format json must produce at least one line"
    _assert_timestamps_parse(records)
    # If the command failed, the envelope carries an exit_code in the FR48 set.
    if result.exit_code != 0:
        for record in records:
            if "exit_code" in record:
                assert record["exit_code"] in {1, 2, 3, 4, 5}
