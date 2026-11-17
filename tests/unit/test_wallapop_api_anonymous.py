"""Anonymous fallback of the Wallapop API transport — v0.5.2.

Search and item detail are public endpoints: once the session is dead
(401 + failed refresh) the transport serves calls without cookies and
without an ``Authorization`` header instead of failing over to the paid
TinyFish path. These tests drive :class:`_RefreshingSession` with its
network call (``_do_request``) and refresh dance replaced by fakes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from salvager.adapters.wallapop_api.fetcher import (
    WallapopResponse,
    _RefreshingSession,
)
from salvager.config.config_yaml import WallapopConfig

_OK = WallapopResponse(status_code=200, text="{}", json_data={"data": {}})
_EXPIRED = WallapopResponse(status_code=401, text="", json_data=None)


def _write_cookies(path: Path, mtime: float) -> None:
    path.write_text(
        "# Netscape HTTP Cookie File\n.wallapop.com\tTRUE\t/\tTRUE\t0\taccessToken\tjwt\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


class _FakeSession(_RefreshingSession):
    """Answers authenticated calls with ``authed`` and anonymous ones with
    ``anonymous``; the refresh dance answers ``refresh_ok``."""

    def __init__(self, cookies_path: Path) -> None:
        super().__init__(cookies_path=cookies_path, base_url="https://x", timeout_s=1)
        self.authed = _EXPIRED
        self.anonymous = _OK
        self.refresh_ok = False
        self.calls: list[str] = []

    async def _do_request(
        self,
        path: str,
        params: dict[str, str],
        cookies: dict[str, str],
        *,
        anonymous: bool = False,
    ) -> WallapopResponse:
        self.calls.append("anonymous" if anonymous else "authed")
        return self.anonymous if anonymous else self.authed

    async def _refresh_session(self) -> bool:
        self.calls.append("refresh")
        return self.refresh_ok


@pytest.fixture
def cookies_path(tmp_path: Path) -> Path:
    path = tmp_path / "wallapop_cookies.txt"
    _write_cookies(path, mtime=1_000_000.0)
    return path


async def test_expired_session_is_served_anonymously(cookies_path: Path) -> None:
    session = _FakeSession(cookies_path)

    response = await session.call("/api/v3/search/section", {})

    assert response.status_code == 200
    assert session.calls == ["authed", "authed", "refresh", "anonymous"]


async def test_anonymous_mode_skips_the_auth_round_trips(cookies_path: Path) -> None:
    session = _FakeSession(cookies_path)
    await session.call("/api/v3/search/section", {})
    session.calls.clear()

    response = await session.call("/api/v3/search/section", {})

    assert response.status_code == 200
    assert session.calls == ["anonymous"], "no 401 / refresh churn while the cookie is unchanged"


async def test_renewed_cookie_leaves_anonymous_mode(cookies_path: Path) -> None:
    session = _FakeSession(cookies_path)
    await session.call("/api/v3/search/section", {})

    _write_cookies(cookies_path, mtime=2_000_000.0)
    session.authed = _OK
    session.calls.clear()
    response = await session.call("/api/v3/search/section", {})

    assert response.status_code == 200
    assert session.calls == ["authed"]
    session.calls.clear()
    await session.call("/api/v3/search/section", {})
    assert session.calls == ["authed"], "back on the authenticated path"


async def test_rejected_anonymous_call_propagates_the_original_401(cookies_path: Path) -> None:
    session = _FakeSession(cookies_path)
    session.anonymous = _EXPIRED

    response = await session.call("/api/v3/search/section", {})

    assert response.status_code == 401


async def test_successful_refresh_never_goes_anonymous(cookies_path: Path) -> None:
    session = _FakeSession(cookies_path)
    session.refresh_ok = True

    async def _authed_after_refresh(
        path: str,
        params: dict[str, str],
        cookies: dict[str, str],
        *,
        anonymous: bool = False,
    ) -> WallapopResponse:
        session.calls.append("anonymous" if anonymous else "authed")
        return _OK if "refresh" in session.calls else _EXPIRED

    session._do_request = _authed_after_refresh  # type: ignore[method-assign]

    response = await session.call("/api/v3/search/section", {})

    assert response.status_code == 200
    assert "anonymous" not in session.calls


def test_tinyfish_search_is_off_by_default() -> None:
    assert WallapopConfig().tinyfish_search_daily_limit == 0
