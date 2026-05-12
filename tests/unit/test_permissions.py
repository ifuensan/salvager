"""Tests for credential-file permission gate — Story 2.11 (NFR-S2 / AR22)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hardware_hunter.config.permissions import (
    EXPECTED_MODE,
    CredentialMissingError,
    CredentialPermissionsError,
    verify_credential_permissions,
    verify_or_exit,
)

# Windows isn't a v1 deployment target — the permission model differs
# enough that asserting POSIX modes there would only generate noise.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-mode credential gate is Linux/macOS only at v1",
)


def _write_file(path: Path, mode: int) -> Path:
    """Create a file with content and set its permission mode."""
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(mode)
    return path


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


def test_mode_0600_passes_silently(tmp_path: Path) -> None:
    secret = _write_file(tmp_path / ".env", 0o600)
    verify_credential_permissions([secret])  # returns None, no raise


def test_multiple_files_all_at_0600_pass(tmp_path: Path) -> None:
    files = [
        _write_file(tmp_path / ".env", 0o600),
        _write_file(tmp_path / "wallapop_cookies.txt", 0o600),
        _write_file(tmp_path / "oauth_tokens.json", 0o600),
    ]
    verify_credential_permissions(files)


def test_empty_list_passes(tmp_path: Path) -> None:
    """Empty input is valid — caller decides which files matter."""
    verify_credential_permissions([])


# ─────────────────────────────────────────────────────────────────────────
# Non-0600 modes — every "looser than 0600" path must fail
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o755, 0o660, 0o666, 0o777])
def test_non_0600_modes_raise(tmp_path: Path, mode: int) -> None:
    secret = _write_file(tmp_path / ".env", mode)
    with pytest.raises(CredentialPermissionsError) as excinfo:
        verify_credential_permissions([secret])
    err = excinfo.value
    assert err.path == secret
    assert err.observed_mode == mode
    assert f"{mode:04o}" in str(err)


def test_stricter_modes_also_rejected(tmp_path: Path) -> None:
    """Even a stricter mode (0o400, read-only) isn't exactly 0600 and
    must fail — the gate is exact-match, not max-permissive."""
    secret = _write_file(tmp_path / ".env", 0o400)
    with pytest.raises(CredentialPermissionsError):
        verify_credential_permissions([secret])


def test_first_violation_stops_the_walk(tmp_path: Path) -> None:
    """If the first file is broken, later files aren't inspected — the
    operator fixes one credential at a time."""
    bad = _write_file(tmp_path / "bad.env", 0o644)
    later = tmp_path / "later.env"  # deliberately not created

    with pytest.raises(CredentialPermissionsError) as excinfo:
        verify_credential_permissions([bad, later])
    assert excinfo.value.path == bad


# ─────────────────────────────────────────────────────────────────────────
# Missing files — different exception class
# ─────────────────────────────────────────────────────────────────────────


def test_missing_file_raises_separate_exception_class(tmp_path: Path) -> None:
    missing = tmp_path / "wallapop_cookies.txt"
    with pytest.raises(CredentialMissingError) as excinfo:
        verify_credential_permissions([missing])
    assert excinfo.value.path == missing


def test_missing_error_not_subclass_of_permissions_error(tmp_path: Path) -> None:
    """Callers may want to render different hints for the two cases —
    the exception hierarchy preserves the distinction."""
    missing = tmp_path / ".env"
    with pytest.raises(CredentialMissingError) as excinfo:
        verify_credential_permissions([missing])
    assert not isinstance(excinfo.value, CredentialPermissionsError)


# ─────────────────────────────────────────────────────────────────────────
# verify_or_exit() — daemon-entry helper with rendered error + exit 4
# ─────────────────────────────────────────────────────────────────────────


def test_verify_or_exit_succeeds_silently_at_0600(
    tmp_path: Path,
) -> None:
    secret = _write_file(tmp_path / ".env", 0o600)
    verify_or_exit([secret])  # returns None


def test_verify_or_exit_renders_permission_template(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    secret = _write_file(tmp_path / ".env", 0o644)

    with pytest.raises(SystemExit) as excinfo:
        verify_or_exit([secret])
    assert excinfo.value.code == 4

    err = capsys.readouterr().err
    assert "has mode 0644" in err
    assert "expected 0600" in err
    assert f"chmod 600 {secret}" in err


def test_verify_or_exit_renders_missing_template(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    missing = tmp_path / "wallapop_cookies.txt"

    with pytest.raises(SystemExit) as excinfo:
        verify_or_exit([missing])
    assert excinfo.value.code == 4

    err = capsys.readouterr().err
    assert "missing credential file:" in err
    assert "login wallapop" in err


def test_missing_env_hint_points_at_init(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    missing = tmp_path / ".env"

    with pytest.raises(SystemExit):
        verify_or_exit([missing])
    err = capsys.readouterr().err
    assert "hardware-hunter init" in err


def test_missing_oauth_hint_points_at_login_ebay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    missing = tmp_path / "oauth_tokens.json"

    with pytest.raises(SystemExit):
        verify_or_exit([missing])
    err = capsys.readouterr().err
    assert "login ebay" in err


# ─────────────────────────────────────────────────────────────────────────
# Module contract
# ─────────────────────────────────────────────────────────────────────────


def test_expected_mode_constant_is_0o600() -> None:
    assert EXPECTED_MODE == 0o600
