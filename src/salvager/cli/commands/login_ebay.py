"""``salvager login ebay`` — Story 2.10 (FR42, NFR-I5, NFR-S2, AR21).

Walks the operator through eBay's OAuth authorization-code flow:

1. Build + print the consent URL (and best-effort open it in a browser).
2. The operator logs in, grants consent, and eBay redirects to their
   RuName with ``?code=...`` appended.
3. The operator pastes that ``code`` back into the terminal.
4. The code is exchanged for refresh + access tokens (via the
   ``ebay_api`` adapter — that's where ``httpx`` lives).
5. Tokens are persisted to ``data_dir/auth/oauth_tokens.json`` with
   mode ``0600`` via :class:`OAuthTokenStore` (atomic write).

The daemon's eBay adapter (Story 3.7) auto-refreshes the access token
from here on; the operator only re-runs this command if the refresh
token itself is revoked.

Exit codes (FR48)
-----------------
``0`` success • ``1`` non-TTY • ``4`` OAuth exchange failed.
"""

from __future__ import annotations

import sys
import webbrowser
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from salvager.adapters.ebay_api.oauth import (
    DEFAULT_SCOPE,
    build_consent_url,
    exchange_code_for_tokens,
)
from salvager.adapters.ebay_api.tokens import OAuthTokens, OAuthTokenStore
from salvager.domain.errors import EbayOAuthExchangeFailed
from salvager.observability.logging import get_logger
from salvager.observability.styling import render_prose

#: Test seam: an async callable matching
#: :func:`exchange_code_for_tokens`'s keyword contract.
TokenExchange = Callable[..., Coroutine[Any, Any, OAuthTokens]]


def run(
    data_dir: Path,
    *,
    app_id: SecretStr,
    cert_id: SecretStr,
    ru_name: str,
    scope: str = DEFAULT_SCOPE,
    exchange: TokenExchange | None = None,
    prompt_for_code: Callable[[], str] = lambda: input("Paste the authorization code: ").strip(),
    open_browser: Callable[[str], bool] = webbrowser.open,
    isatty: Callable[[], bool] = sys.stdin.isatty,
) -> int:
    """Run the eBay OAuth login flow. Returns a CLI exit code.

    Never raises through the typer boundary — every failure is rendered
    via :func:`render_prose` first.
    """
    import asyncio

    log = get_logger("cli.login_ebay")

    if not isatty():
        render_prose("login ebay requires an interactive terminal", style="error")
        return 1

    consent_url = build_consent_url(
        app_id=app_id.get_secret_value(),
        ru_name=ru_name,
        scope=scope,
    )
    render_prose(f"Open this URL to authorize salvager:\n{consent_url}", style="info")
    # Best-effort — headless boxes have no browser; the operator can
    # still copy the URL above. A failure here is not fatal.
    try:
        open_browser(consent_url)
    except Exception:
        log.debug("login_ebay_browser_open_failed", extra={})

    code = prompt_for_code()
    if not code:
        render_prose(
            "no authorization code entered",
            style="error",
            hint="re-run salvager login ebay and paste the code from the redirect URL",
        )
        return 4

    exchange_fn = exchange if exchange is not None else exchange_code_for_tokens

    try:
        tokens = asyncio.run(
            exchange_fn(
                code=code,
                app_id=app_id,
                cert_id=cert_id,
                ru_name=ru_name,
            )
        )
    except EbayOAuthExchangeFailed as exc:
        log.exception(
            "login_ebay_exchange_failed",
            extra={"status_code": exc.status_code, "ebay_message": exc.ebay_message},
        )
        render_prose(
            f"OAuth exchange failed: {exc.ebay_message}",
            style="error",
            hint="re-run salvager login ebay and re-paste the code",
        )
        return 4

    tokens_path = data_dir / "auth" / "oauth_tokens.json"
    OAuthTokenStore(tokens_path).save(tokens)

    log.info(
        "login_ebay_success",
        extra={
            "tokens_path": str(tokens_path),
            "expires_at": tokens.expires_at.isoformat(),
        },
    )
    render_prose("OAuth tokens captured (mode 0600 verified)", style="success")
    return 0


__all__ = ["run"]
