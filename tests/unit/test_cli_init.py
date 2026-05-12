"""Tests for ``hardware-hunter init`` — Story 2.8 (FR40)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hardware_hunter.cli.app import app
from hardware_hunter.cli.commands.init_cmd import OVERWRITE_TOKEN, run

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ─────────────────────────────────────────────────────────────────────────
# Template-sync contract — bundled copies must match repo-root examples
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "repo_name, bundled_name",
    [
        (".env.example", "dot.env.example"),
        ("wishlist.example.yaml", "wishlist.example.yaml"),
        ("config.example.yaml", "config.example.yaml"),
    ],
)
def test_bundled_templates_match_repo_examples(repo_name: str, bundled_name: str) -> None:
    """The bundled copy under src/hardware_hunter/templates must match
    the operator-facing copy at the repo root, byte for byte. Drift here
    means a user's `init` produces different content than what they see
    on GitHub when forking. This test is the sync mechanism."""
    repo_copy = (REPO_ROOT / repo_name).read_bytes()
    bundled = (REPO_ROOT / "src" / "hardware_hunter" / "templates" / bundled_name).read_bytes()
    assert bundled == repo_copy


# ─────────────────────────────────────────────────────────────────────────
# Happy path — empty config_dir
# ─────────────────────────────────────────────────────────────────────────


def test_empty_config_dir_scaffolds_three_files(tmp_path: Path) -> None:
    target = tmp_path / "config"
    exit_code = run(target, force=False)
    assert exit_code == 0
    assert (target / ".env").exists()
    assert (target / "wishlist.yaml").exists()
    assert (target / "config.yaml").exists()


def test_init_creates_missing_config_dir(tmp_path: Path) -> None:
    target = tmp_path / "deeply" / "nested" / "config"
    exit_code = run(target, force=False)
    assert exit_code == 0
    assert target.is_dir()


def test_scaffolded_files_match_bundled_templates(tmp_path: Path) -> None:
    target = tmp_path / "config"
    run(target, force=False)
    # The scaffolded files are byte-for-byte identical to the examples.
    assert (target / "wishlist.yaml").read_bytes() == (
        REPO_ROOT / "wishlist.example.yaml"
    ).read_bytes()
    assert (target / "config.yaml").read_bytes() == (REPO_ROOT / "config.example.yaml").read_bytes()
    assert (target / ".env").read_bytes() == (REPO_ROOT / ".env.example").read_bytes()


def test_init_renders_panel_with_each_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UX-DR: a rich.panel.Panel(box=ROUNDED) lists each created file."""
    target = tmp_path / "config"
    run(target, force=False)
    out = capsys.readouterr().out
    # Path of each file appears.
    assert str(target / ".env") in out
    assert str(target / "wishlist.yaml") in out
    assert str(target / "config.yaml") in out
    # The panel title is in the output too.
    assert "hardware-hunter init" in out


# ─────────────────────────────────────────────────────────────────────────
# Refusal when files exist (no --force)
# ─────────────────────────────────────────────────────────────────────────


def test_existing_wishlist_refuses_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    target = tmp_path / "config"
    target.mkdir()
    (target / "wishlist.yaml").write_text("existing", encoding="utf-8")

    exit_code = run(target, force=False)
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "wishlist.yaml already exists" in err
    assert "--force" in err
    # Nothing was overwritten.
    assert (target / "wishlist.yaml").read_text(encoding="utf-8") == "existing"
    # And nothing was written for the other two files either.
    assert not (target / ".env").exists()
    assert not (target / "config.yaml").exists()


# ─────────────────────────────────────────────────────────────────────────
# --force in non-TTY — refuses per NFR-S6
# ─────────────────────────────────────────────────────────────────────────


def test_force_without_tty_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    target = tmp_path / "config"
    target.mkdir()
    (target / "wishlist.yaml").write_text("existing", encoding="utf-8")

    def _not_a_tty() -> bool:
        return False

    exit_code = run(target, force=True, isatty=_not_a_tty)
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "requires an interactive terminal" in err
    assert (target / "wishlist.yaml").read_text(encoding="utf-8") == "existing"


# ─────────────────────────────────────────────────────────────────────────
# --force + TTY — typing-OVERWRITE confirmation
# ─────────────────────────────────────────────────────────────────────────


def test_force_with_tty_and_correct_token_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    (target / "wishlist.yaml").write_text("existing", encoding="utf-8")

    prompts: list[str] = []

    def _record_prompt(message: str) -> str:
        prompts.append(message)
        return OVERWRITE_TOKEN

    exit_code = run(
        target,
        force=True,
        isatty=lambda: True,
        prompt=_record_prompt,
    )
    assert exit_code == 0
    assert prompts == [f"Type '{OVERWRITE_TOKEN}' to confirm: "]
    # File was overwritten with the bundled template content.
    assert (target / "wishlist.yaml").read_bytes() == (
        REPO_ROOT / "wishlist.example.yaml"
    ).read_bytes()


def test_force_with_tty_and_wrong_token_cancels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    target = tmp_path / "config"
    target.mkdir()
    (target / "wishlist.yaml").write_text("existing", encoding="utf-8")

    exit_code = run(
        target,
        force=True,
        isatty=lambda: True,
        prompt=lambda _msg: "y",  # operator typed "y" instead of OVERWRITE
    )
    assert exit_code == 1
    out_err = capsys.readouterr()
    assert "cancelled" in out_err.out + out_err.err
    # File was NOT overwritten.
    assert (target / "wishlist.yaml").read_text(encoding="utf-8") == "existing"


def test_force_with_correct_token_overwrites_all_three(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    for name in (".env", "wishlist.yaml", "config.yaml"):
        (target / name).write_text("stale", encoding="utf-8")

    exit_code = run(
        target,
        force=True,
        isatty=lambda: True,
        prompt=lambda _msg: OVERWRITE_TOKEN,
    )
    assert exit_code == 0
    # All three were refreshed from bundled templates.
    assert (target / ".env").read_bytes() == (REPO_ROOT / ".env.example").read_bytes()
    assert (target / "wishlist.yaml").read_bytes() == (
        REPO_ROOT / "wishlist.example.yaml"
    ).read_bytes()
    assert (target / "config.yaml").read_bytes() == (REPO_ROOT / "config.example.yaml").read_bytes()


# ─────────────────────────────────────────────────────────────────────────
# Typer integration
# ─────────────────────────────────────────────────────────────────────────


def test_init_via_typer_empty_dir(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "config"
    result = runner.invoke(app, ["init", "--config-dir", str(target)])
    assert result.exit_code == 0
    assert (target / "wishlist.yaml").exists()


def test_init_via_typer_existing_file_refused(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    (target / "config.yaml").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["init", "--config-dir", str(target)])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
