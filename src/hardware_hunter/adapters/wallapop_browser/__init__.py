"""Wallapop browser-login adapter — Story 2.9.

Public surface:

  - :func:`capture_wallapop_cookies` — drive a headed Chromium session
    and return its cookie jar once the operator has logged in
  - :class:`BrowserLoginTimeout` — no session cookie within the budget
  - :class:`BrowserNotInstalled` — Playwright present, Chromium binary absent
  - :data:`CookieDict` — the shape Playwright reports per cookie

The Playwright SDK is imported ONLY inside this package — adapter
discipline (NFR-M1) keeps browser automation out of ``cli/`` and the
orchestration layer. The ``login wallapop`` CLI command composes
against :func:`capture_wallapop_cookies` and owns everything else
(TTY gate, Netscape serialization, atomic write, 0600 enforcement).
"""

from hardware_hunter.adapters.wallapop_browser.login import (
    BrowserLoginTimeout,
    BrowserNotInstalled,
    CookieDict,
    capture_wallapop_cookies,
)

__all__ = [
    "BrowserLoginTimeout",
    "BrowserNotInstalled",
    "CookieDict",
    "capture_wallapop_cookies",
]
