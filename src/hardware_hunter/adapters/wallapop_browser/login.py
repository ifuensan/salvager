"""Playwright-driven Wallapop login — Story 2.9 (FR41).

Owns the only ``playwright`` import in the codebase. Drives a headed
Chromium browser to Wallapop's login page, lets the operator complete
login + 2FA by hand, and polls the browser context until a logged-in
session cookie appears.

Why headed
----------
Wallapop's anti-bot stack rejects headless Chromium; the operator's
hands-on login is what produces a usable session. This adapter is
interactive by design — it has no headless mode.

Why poll for a cookie name
--------------------------
Listening on every navigation is unreliable across SSO redirects and
soft-route changes. Polling ``context.cookies()`` for a known session
cookie name is robust and finishes the moment Wallapop sets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, Final

#: How often the driver polls the browser context for the session cookie.
_POLL_INTERVAL_S: Final[float] = 1.0

#: Cookie names that, once set with a non-empty value, prove the browser
#: holds an authenticated Wallapop session. Polling stops as soon as ANY
#: of these is observed. Ordered most → least likely so the membership
#: check short-circuits on the common case.
_SESSION_COOKIE_NAMES: Final[tuple[str, ...]] = ("accessToken", "MPID", "device_id")

#: The shape Playwright reports per cookie:
#: ``{"name", "value", "domain", "path", "secure", "expires", ...}``.
#: Kept as a loose dict alias — the CLI's serializer reads only the
#: fields it needs and tolerates the rest.
CookieDict = dict[str, Any]


class BrowserLoginTimeout(RuntimeError):
    """The operator did not produce a session cookie within the budget."""


class BrowserNotInstalled(RuntimeError):
    """Playwright is importable but the Chromium binary is missing.

    Raised both when ``import playwright`` fails (package absent) and
    when ``chromium.launch`` reports the executable hasn't been
    downloaded (``playwright install chromium`` never ran).
    """


async def capture_wallapop_cookies(login_url: str, timeout_s: float) -> list[CookieDict]:
    """Open headed Chromium at ``login_url`` and return its cookie jar.

    Polls :meth:`BrowserContext.cookies` once per :data:`_POLL_INTERVAL_S`
    until one of :data:`_SESSION_COOKIE_NAMES` is observed with a
    non-empty value, or ``timeout_s`` elapses.

    Raises:
        BrowserNotInstalled: Playwright or its Chromium binary is absent.
        BrowserLoginTimeout: no session cookie appeared in time.
    """
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover — install gate
        raise BrowserNotInstalled(str(exc)) from exc

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=False)
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                raise BrowserNotInstalled(str(exc)) from exc
            raise

        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(login_url)
            return await _wait_for_session_cookie(context, timeout_s)
        finally:
            await browser.close()


async def _wait_for_session_cookie(context: Any, timeout_s: float) -> list[CookieDict]:
    """Poll ``context.cookies()`` until a session cookie appears."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        cookies = await context.cookies()
        if _has_session_cookie(cookies):
            return list(cookies)
        if asyncio.get_running_loop().time() >= deadline:
            raise BrowserLoginTimeout()
        await asyncio.sleep(_POLL_INTERVAL_S)


def _has_session_cookie(cookies: Iterable[CookieDict]) -> bool:
    return any(c.get("name") in _SESSION_COOKIE_NAMES and (c.get("value") or "") for c in cookies)


__all__ = [
    "BrowserLoginTimeout",
    "BrowserNotInstalled",
    "CookieDict",
    "capture_wallapop_cookies",
]
