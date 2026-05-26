"""Tests for ``salvager validate-wishlist`` — Story 2.4."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from salvager.cli.app import app

# Shared fixture
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_WISHLIST = REPO_ROOT / "wishlist.example.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def example_path(tmp_path: Path) -> Path:
    dest = tmp_path / "wishlist.yaml"
    dest.write_text(EXAMPLE_WISHLIST.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


# ─────────────────────────────────────────────────────────────────────────
# Success — human + JSON
# ─────────────────────────────────────────────────────────────────────────


def test_valid_wishlist_returns_success(runner: CliRunner, example_path: Path) -> None:
    result = runner.invoke(app, ["validate-wishlist", "--path", str(example_path)])
    assert result.exit_code == 0
    assert "wishlist.yaml is valid" in result.stdout
    assert "4 entries" in result.stdout
    assert "0 with Phase 2 enabled" in result.stdout


def test_valid_wishlist_json_output(runner: CliRunner, example_path: Path) -> None:
    result = runner.invoke(
        app,
        ["validate-wishlist", "--path", str(example_path), "--format", "json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload == {
        "valid": True,
        "entry_count": 4,
        "phase2_enabled_count": 0,
    }


def test_phase2_enabled_count_reflects_yaml(runner: CliRunner, tmp_path: Path) -> None:
    """One entry with phase2.enabled=true is counted."""
    wishlist = tmp_path / "wishlist.yaml"
    wishlist.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red
    ref: WD40
    type: hdd
    max_price_solo: 60.00
    keywords: []
    confidence_threshold: high
    phase2:
      enabled: true
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["validate-wishlist", "--path", str(wishlist), "--format", "json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload["phase2_enabled_count"] == 1


# ─────────────────────────────────────────────────────────────────────────
# Scope violation — locked (c3) error template
# ─────────────────────────────────────────────────────────────────────────


def test_forbidden_field_locked_error_template(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red Plus 4TB
    ref: WD40EFPX
    type: hdd
    max_price_solo: 60.00
    keywords: []
    confidence_threshold: high
    expected_resale_value: 80.00
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate-wishlist", "--path", str(bad)])

    assert result.exit_code == 3
    err = result.stderr
    # The locked template, all three required pieces.
    assert "forbidden field 'expected_resale_value'" in err
    assert "Red Plus 4TB" in err  # entry name resolved
    assert "(c3) scope contract" in err
    assert "github.com/ifuensan/salvager-research" in err
    # Line number from ruamel
    assert ":9:" in err or ":9 " in err or "line 9" in err


def test_scope_error_json_mode_emits_envelope_on_stderr(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red
    ref: WD40
    type: hdd
    arbitrage_score: 0.85
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate-wishlist", "--path", str(bad), "--format", "json"])
    assert result.exit_code == 3
    assert result.stdout == ""  # stderr only in JSON error mode
    payload = json.loads(result.stderr.strip())
    assert payload["exit_code"] == 3
    assert "arbitrage_score" in payload["message"]


# ─────────────────────────────────────────────────────────────────────────
# Duplicates — name both entries with line numbers
# ─────────────────────────────────────────────────────────────────────────


def test_duplicate_entry_keys_names_both_with_line_numbers(
    runner: CliRunner, tmp_path: Path
) -> None:
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red
    ref: WD40EFPX
    type: hdd
    max_price_solo: 60.00
    keywords: []
    confidence_threshold: high
  - manufacturer: WD
    model: Red
    ref: WD40EFPX
    type: hdd
    max_price_solo: 65.00
    keywords: []
    confidence_threshold: high
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate-wishlist", "--path", str(bad)])

    assert result.exit_code == 3
    err = result.stderr
    assert "duplicate entry key" in err
    # Both entry line numbers surface — the two `- manufacturer:` rows
    # at lines 2 and 9 of the YAML above (1-based).
    assert ":2" in err
    assert ":9" in err


# ─────────────────────────────────────────────────────────────────────────
# File not found
# ─────────────────────────────────────────────────────────────────────────


def test_missing_file_exits_with_code_1(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    result = runner.invoke(app, ["validate-wishlist", "--path", str(missing)])

    assert result.exit_code == 1
    err = result.stderr
    assert "not found" in err
    assert "salvager init" in err


def test_missing_file_json_envelope(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    result = runner.invoke(app, ["validate-wishlist", "--path", str(missing), "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr.strip())
    assert payload["exit_code"] == 1
    assert "not found" in payload["message"]


# ─────────────────────────────────────────────────────────────────────────
# Parse errors
# ─────────────────────────────────────────────────────────────────────────


def test_malformed_yaml_exits_with_code_3(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
   bad_indent: oops
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate-wishlist", "--path", str(bad)])
    assert result.exit_code == 3
    assert "malformed YAML" in result.stderr


# ─────────────────────────────────────────────────────────────────────────
# --format validation
# ─────────────────────────────────────────────────────────────────────────


def test_unknown_format_exits_with_code_2(runner: CliRunner, example_path: Path) -> None:
    result = runner.invoke(
        app, ["validate-wishlist", "--path", str(example_path), "--format", "yaml"]
    )
    assert result.exit_code == 2
    assert "unknown --format" in result.stderr.lower()
