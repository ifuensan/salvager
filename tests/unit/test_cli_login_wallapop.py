"""Tests for ``hardware-hunter login wallapop`` — Story 2.9.

The browser-login adapter is mocked at the module boundary (the
``capture`` parameter of :func:`run`). The Netscape serializer +
permission enforcement run for real against ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hardware_hunter.adapters.wallapop_api.cookies import load_cookies
from hardware_hunter.adapters.wallapop_browser import (
    BrowserLoginTimeout,
    BrowserNotInstalled,
)
from hardware_hunter.cli.commands.login_wallapop import _serialize_to_netscape, run


def _ok_cookies() -> list[dict[str, object]]:
    """A logged-in Wallapop cookie set as Playwright would surface it."""
    return [
        {
            "name": "accessToken",
            "value": "ey.fake.jwt",
            "domain": ".wallapop.com",
            "path": "/",
            "secure": True,
            "expires": 1_900_000_000,
        },
        {
            "name": "device_id",
            "value": "device-1234",
            "domain": ".wallapop.com",
            "path": "/",
            "secure": True,
            "expires": -1,  # session cookie — Netscape gets 0
        },
    ]


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


def test_run_writes_netscape_file_with_mode_0600(tmp_path: Path) -> None:
    cookies = _ok_cookies()

    async def fake_capture(url: str, timeout_s: float) -> list[dict[str, object]]:
        assert url.startswith("https://es.wallapop.com")
        return cookies

    code = run(tmp_path, capture=fake_capture, isatty=lambda: True)

    cookies_path = tmp_path / "auth" / "wallapop_cookies.txt"
    assert code == 0
    assert cookies_path.exists()
    assert (cookies_path.stat().st_mode & 0o777) == 0o600
    # Netscape format round-trips through the adapter's loader.
    jar = load_cookies(cookies_path)
    assert jar.get("accessToken", domain=".wallapop.com") == "ey.fake.jwt"


def test_run_serializes_session_cookies_with_zero_expiry(tmp_path: Path) -> None:
    async def fake_capture(url: str, timeout_s: float) -> list[dict[str, object]]:
        return _ok_cookies()

    code = run(tmp_path, capture=fake_capture, isatty=lambda: True)
    assert code == 0

    body = (tmp_path / "auth" / "wallapop_cookies.txt").read_text(encoding="utf-8")
    # The -1 session cookie must be normalized to "0" in column 5 (expiry).
    session_row = next(line for line in body.splitlines() if "device_id" in line)
    fields = session_row.split("\t")
    assert fields[4] == "0"


# ─────────────────────────────────────────────────────────────────────────
# Failure paths — non-TTY, timeout, browser missing
# ─────────────────────────────────────────────────────────────────────────


def test_run_refuses_in_non_tty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def never_called(url: str, timeout_s: float) -> list[dict[str, object]]:
        raise AssertionError("capture must not run in non-TTY context")

    code = run(tmp_path, capture=never_called, isatty=lambda: False)
    assert code == 1
    captured = capsys.readouterr()
    assert "interactive terminal" in captured.err
    # No file written on non-TTY refusal.
    assert not (tmp_path / "auth" / "wallapop_cookies.txt").exists()


def test_run_on_timeout_returns_exit_4_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def timeout_capture(url: str, timeout_s: float) -> list[dict[str, object]]:
        raise BrowserLoginTimeout()

    code = run(tmp_path, capture=timeout_capture, isatty=lambda: True)
    assert code == 4
    out = capsys.readouterr()
    assert "login timeout" in out.err
    assert "re-run" in out.err  # hint
    assert not (tmp_path / "auth" / "wallapop_cookies.txt").exists()


def test_run_on_browser_missing_returns_exit_4_with_install_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def missing_browser(url: str, timeout_s: float) -> list[dict[str, object]]:
        raise BrowserNotInstalled("Executable doesn't exist at /home/me/.cache/...")

    code = run(tmp_path, capture=missing_browser, isatty=lambda: True)
    assert code == 4
    out = capsys.readouterr()
    assert "Chromium" in out.err
    assert "playwright install chromium" in out.err
    assert not (tmp_path / "auth" / "wallapop_cookies.txt").exists()


# ─────────────────────────────────────────────────────────────────────────
# Netscape serializer details
# ─────────────────────────────────────────────────────────────────────────


def test_serializer_skips_rows_without_name_or_value() -> None:
    rendered = _serialize_to_netscape(
        [
            {"name": "", "value": "x", "domain": ".wallapop.com"},
            {"name": "y", "value": None, "domain": ".wallapop.com"},
            {
                "name": "good",
                "value": "v",
                "domain": ".wallapop.com",
                "path": "/",
                "secure": True,
                "expires": 0,
            },
        ]
    )
    rows = [line for line in rendered.splitlines() if not line.startswith("#")]
    assert len(rows) == 1
    assert rows[0].split("\t")[5:] == ["good", "v"]


def test_serializer_marks_dot_prefix_domain_as_include_subs() -> None:
    rendered = _serialize_to_netscape(
        [
            {
                "name": "a",
                "value": "1",
                "domain": ".wallapop.com",
                "path": "/",
                "secure": False,
                "expires": 0,
            },
            {
                "name": "b",
                "value": "2",
                "domain": "es.wallapop.com",
                "path": "/",
                "secure": False,
                "expires": 0,
            },
        ]
    )
    rows = [line for line in rendered.splitlines() if not line.startswith("#")]
    assert rows[0].split("\t")[1] == "TRUE"
    assert rows[1].split("\t")[1] == "FALSE"
