"""Tests for the Wallapop browser-login adapter — Story 2.9.

The Playwright SDK itself is never exercised here — that needs a real
Chromium binary and a human. We test the polling loop
(:func:`_wait_for_session_cookie`) and the session-cookie predicate
against a fake ``BrowserContext``.
"""

from __future__ import annotations

from typing import Any

import pytest

from hardware_hunter.adapters.wallapop_browser import login as login_mod
from hardware_hunter.adapters.wallapop_browser.login import (
    BrowserLoginTimeout,
    _has_session_cookie,
    _wait_for_session_cookie,
)


def _ok_cookies() -> list[dict[str, Any]]:
    return [
        {"name": "accessToken", "value": "ey.fake.jwt", "domain": ".wallapop.com"},
        {"name": "device_id", "value": "device-1234", "domain": ".wallapop.com"},
    ]


class _FakeContext:
    """Returns ``cookies_per_poll[i]`` on the i-th ``cookies()`` call."""

    def __init__(self, cookies_per_poll: list[list[dict[str, Any]]]) -> None:
        self._sequence = cookies_per_poll
        self.poll_count = 0

    async def cookies(self) -> list[dict[str, Any]]:
        result = self._sequence[min(self.poll_count, len(self._sequence) - 1)]
        self.poll_count += 1
        return result


# ─────────────────────────────────────────────────────────────────────────
# _has_session_cookie predicate
# ─────────────────────────────────────────────────────────────────────────


def test_has_session_cookie_true_only_for_known_names_with_values() -> None:
    assert _has_session_cookie([{"name": "accessToken", "value": "x"}])
    assert _has_session_cookie([{"name": "MPID", "value": "x"}])
    # Known name but empty value → not logged in yet.
    assert not _has_session_cookie([{"name": "accessToken", "value": ""}])
    # Unknown cookie name → ignored.
    assert not _has_session_cookie([{"name": "csrftoken", "value": "x"}])
    assert not _has_session_cookie([])


# ─────────────────────────────────────────────────────────────────────────
# _wait_for_session_cookie polling loop
# ─────────────────────────────────────────────────────────────────────────


async def test_wait_for_session_cookie_returns_once_cookie_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Make polling instant so the test doesn't actually sleep.
    monkeypatch.setattr(login_mod, "_POLL_INTERVAL_S", 0.0)
    context = _FakeContext(
        [
            [],  # poll 1: not logged in yet
            [{"name": "other", "value": "x"}],  # poll 2: still no session cookie
            _ok_cookies(),  # poll 3: logged in
        ]
    )
    cookies = await _wait_for_session_cookie(context, timeout_s=5.0)
    assert any(c["name"] == "accessToken" for c in cookies)
    assert context.poll_count == 3


async def test_wait_for_session_cookie_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login_mod, "_POLL_INTERVAL_S", 0.0)
    context = _FakeContext([[]])  # never logs in
    with pytest.raises(BrowserLoginTimeout):
        await _wait_for_session_cookie(context, timeout_s=0.0)
