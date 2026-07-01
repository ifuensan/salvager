"""Wallapop unofficial-API :class:`PageFetcher` — Story 3.4.

Searches ``api.wallapop.com/api/v3/search/section`` with the
operator's captured session cookie. Maps results into domain
:class:`Listing` instances; surfaces schema drift, session expiry,
and other 4xx/5xx failures as typed exceptions in
:mod:`salvager.domain.errors`.

Migration note (2026-05-18 / 2026-05-19)
----------------------------------------
- The legacy ``/api/v3/general/search`` endpoint was deprecated by
  Wallapop before 2026-05-18; it returns HTTP 403 (CDN-level) to
  every client. The current SPA hits ``/api/v3/search/section``
  with ``section_type=organic_search_results`` and required
  ``latitude``/``longitude`` query params (browser geolocation).
- Wallapop's CloudFront WAF rejects clients whose TLS handshake
  doesn't look like a real browser (JA3/JA4 fingerprinting).
  ``httpx`` is one of those. The adapter uses
  ``curl_cffi.requests.AsyncSession`` with ``impersonate='chrome131'``
  to reproduce the Chrome ClientHello bit-for-bit and pass.
- Beyond TLS, the SPA injects six application-level headers the WAF
  cross-checks against cookies: ``Authorization: Bearer
  <accessToken>``, ``mpid`` / ``trackinguserid`` (both from the
  ``trackingUserId`` cookie), ``x-deviceid`` (from the ``device_id``
  cookie, also encoded in the JWT payload), ``x-appversion``,
  ``deviceos`` / ``x-deviceos`` (web = "0"), and a custom
  ``Accept: application/json; sequence=v2``. All eight are derived
  per-request from the cookie jar; the only hardcoded value is
  ``x-appversion``, which moves with the SPA bundle (bump as
  needed).

TLS: ``curl_cffi.requests.AsyncSession`` enables ``verify=True`` by
default and we do not override it (NFR-S3).

Logging events
--------------
``wallapop_search_succeeded`` — latency_ms + result_count + marketplace
``wallapop_search_failed``    — error_class + status_code (when known)
``wallapop_schema_drift``     — error_class + field_path
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from salvager.adapters.wallapop_api.cookies import load_cookies, write_cookies
from salvager.adapters.wallapop_api.schema import (
    WallapopApiItem,
    WallapopApiSearchResponse,
)
from salvager.domain.errors import (
    WallapopApiError,
    WallapopSchemaDrift,
    WallapopSessionExpired,
)
from salvager.domain.listing import Listing, SearchQuery
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.observability.logging import get_logger

_DEFAULT_BASE_URL = "https://api.wallapop.com"
_SEARCH_PATH = "/api/v3/search/section"
_ITEM_PATH_TEMPLATE = "/api/v3/items/{listing_id}"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_ITEM_URL_TEMPLATE = "https://es.wallapop.com/item/{web_slug}"
#: NextAuth refresh endpoint. Takes existing cookies as input, emits
#: a fresh ``accessToken`` + ``__Secure-next-auth.session-token`` via
#: ``Set-Cookie`` response headers (response body is empty). The
#: web SPA calls this transparently on every 401 from api.wallapop.com.
_FEDERATED_SESSION_URL = "https://es.wallapop.com/api/auth/federated-session"
#: Cookies the federated-session refresh response rotates. Persisted
#: back to disk so the next process invocation starts with fresh
#: tokens instead of always 401-ing on the first request.
_ROTATING_COOKIE_NAMES: frozenset[str] = frozenset(
    {"accessToken", "__Secure-next-auth.session-token"}
)
#: Madrid centre — kept as a module-level fallback so callers that
#: don't have a :class:`ConfigModel` (legacy tests, ad-hoc scripts)
#: still get a working fetcher.
_DEFAULT_LATITUDE = 40.4168
_DEFAULT_LONGITUDE = -3.7038
#: SPA app version. Moves with the SPA bundle Wallapop ships — verified
#: 2026-05-19 = "821220". Bumping is safe; the API checks presence /
#: parseability rather than a specific value (verified empirically).
_X_APPVERSION = "821220"
#: Web client identifier the API expects in both ``deviceos`` and
#: ``x-deviceos``. iOS=2, Android=1 per the SPA bundle.
_DEVICEOS_WEB = "0"


@dataclass(slots=True)
class WallapopResponse:
    """Provider-agnostic HTTP response surface the fetcher consumes.

    The default callable wraps :mod:`curl_cffi` responses into this
    shape; tests construct it directly. Decoupling means tests don't
    need to mock TLS, headers, cookies, or the curl FFI layer.
    """

    status_code: int
    text: str
    json_data: Any  # Parsed body, or None when status >= 400.


#: Signature of the low-level HTTP call. ``path`` is relative to the
#: ``base_url``; ``params`` is the query dict. The callable owns
#: cookie jar / impersonation / auth-header derivation — the
#: production default does this from the cookies-file path; tests
#: skip all of it and return canned responses.
WallapopRequestCallable = Callable[[str, dict[str, str]], Awaitable[WallapopResponse]]


class WallapopApiFetcher(PageFetcher):
    """``PageFetcher`` backed by Wallapop's unofficial JSON API.

    Production calls leave ``request`` as None; the constructor wires
    a ``curl_cffi`` session with browser TLS impersonation and the
    eight required SPA headers derived from the cookie jar.
    Tests pass a callable returning :class:`WallapopResponse`
    directly — same pattern as the Gemini / Claude evaluator
    adapters (NFR-M1).
    """

    def __init__(
        self,
        cookies_path: str | Path,
        *,
        latitude: float = _DEFAULT_LATITUDE,
        longitude: float = _DEFAULT_LONGITUDE,
        base_url: str = _DEFAULT_BASE_URL,
        request: WallapopRequestCallable | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._cookies_path = Path(cookies_path)
        self._latitude = latitude
        self._longitude = longitude
        self._request: WallapopRequestCallable = (
            request
            if request is not None
            else _build_default_request(
                cookies_path=self._cookies_path,
                base_url=self._base_url,
                timeout_s=timeout,
            )
        )
        self._log = get_logger("adapter.wallapop_api")

    async def aclose(self) -> None:
        """No-op for the callable seam — production session is
        opened/closed per call to keep the surface stateless.
        Kept for API compatibility with the previous httpx-based
        client (some call sites still ``await fetcher.aclose()``).
        """
        return None

    # ─────────────────────────────────────────────────────────────────
    # PageFetcher
    # ─────────────────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Listing]:
        # The v3 endpoint expects a leading space in `keywords` when
        # source is "search_box" (mirrors the SPA, verified
        # empirically against live traffic 2026-05-18).
        params: dict[str, str] = {
            "keywords": " " + query.keyword,
            "source": "search_box",
            "latitude": str(self._latitude),
            "longitude": str(self._longitude),
            "order_by": "most_relevance",
            "search_id": str(uuid.uuid4()),
            "section_type": "organic_search_results",
        }

        started = time.perf_counter()
        try:
            response = await self._request(_SEARCH_PATH, params)
        except Exception as exc:
            self._log.error(
                "wallapop_search_failed",
                extra={"error_class": exc.__class__.__name__, "marketplace": "wallapop"},
            )
            raise WallapopApiError(0, str(exc)) from exc

        self._raise_for_status(response)
        try:
            payload = WallapopApiSearchResponse.model_validate(response.json_data)
        except ValidationError as exc:
            drift = _from_validation_error(exc)
            self._log.error(
                "wallapop_schema_drift",
                extra={
                    "error_class": "WallapopSchemaDrift",
                    "marketplace": "wallapop",
                    "field_path": drift.field_path,
                },
            )
            raise drift from exc

        listings = [_item_to_listing(item) for item in payload.items]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._log.info(
            "wallapop_search_succeeded",
            extra={
                "marketplace": "wallapop",
                "latency_ms": elapsed_ms,
                "result_count": len(listings),
            },
        )
        return listings

    async def fetch(self, listing_url: str) -> Listing:
        """Fetch one listing by URL.

        Used by ``salvager explain <url>`` (Epic 4) and the Phase 2
        pre-buy reconciliation. The per-item endpoint accepts the
        listing_id (the path's tail segment, which Wallapop's slugs
        encode at the end after the last ``-``); we pass the full
        slug, which the API tolerates.
        """
        # Pasted Wallapop URLs often carry ``?source=...`` / ``#anchor`` —
        # drop both before extracting the trailing slug, or the upstream
        # endpoint 404s on an otherwise valid URL.
        listing_path = urlsplit(listing_url).path.rstrip("/")
        listing_id = listing_path.rsplit("/", 1)[-1]
        path = _ITEM_PATH_TEMPLATE.format(listing_id=listing_id)
        started = time.perf_counter()
        try:
            response = await self._request(path, {})
        except Exception as exc:
            raise WallapopApiError(0, str(exc)) from exc

        self._raise_for_status(response)
        try:
            item = WallapopApiItem.model_validate(response.json_data)
        except ValidationError as exc:
            raise _from_validation_error(exc) from exc

        listing = _item_to_listing(item)
        self._log.info(
            "wallapop_fetch_succeeded",
            extra={
                "marketplace": "wallapop",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "listing_id": listing_id,
            },
        )
        return listing

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _raise_for_status(self, response: WallapopResponse) -> None:
        if response.status_code == 401:
            self._log.warning(
                "wallapop_session_expired",
                extra={"marketplace": "wallapop", "status_code": 401},
            )
            raise WallapopSessionExpired("Wallapop returned HTTP 401 — session expired")
        if response.status_code >= 400:
            body = response.text[:200] if response.text else None
            self._log.error(
                "wallapop_api_error",
                extra={
                    "marketplace": "wallapop",
                    "status_code": response.status_code,
                    "error_class": "WallapopApiError",
                },
            )
            raise WallapopApiError(response.status_code, body)


def _item_to_listing(item: WallapopApiItem) -> Listing:
    """Project an upstream ``WallapopApiItem`` onto the domain shape.

    ``seller_history_count`` was sourced from the legacy nested
    ``user.items_count`` field; the v3 search endpoint omits it. A
    follow-up enrichment via ``/api/v3/users/{user_id}`` will
    repopulate it for callers that need it; today it stays ``None``.
    """
    slug = item.web_slug or item.id
    return Listing(
        listing_id=item.id,
        marketplace="wallapop",
        url=_ITEM_URL_TEMPLATE.format(web_slug=slug),
        title=item.title,
        description=item.description,
        price_eur=Decimal(str(item.price.amount)),
        # Wallapop's search/section API does not expose a fixed shipping cost
        # (it's computed at checkout from the buyer's address + package weight),
        # so shipping_eur stays None (unknown) → the buyer-total uses the
        # configurable buffer + the deterministic Protección Wallapop fee. See
        # salvager.domain.pricing.
        shipping_eur=None,
        location=item.location.city if item.location else None,
        photo_urls=[url for url in (item.preferred_photo_url(),) if url is not None],
        seller_id=item.user_id,
        seller_history_count=None,
        published_at=_unix_millis_to_dt(item.created_at),
        fetched_at=datetime.now(UTC),
        is_reserved=bool(item.reserved and item.reserved.flag),
    )


def _unix_millis_to_dt(value: int | None) -> datetime | None:
    """Convert Wallapop's unix-millisecond timestamps to ``datetime``."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _from_validation_error(exc: ValidationError) -> WallapopSchemaDrift:
    """Map pydantic's ValidationError to a single :class:`WallapopSchemaDrift`.

    Pydantic reports ``loc`` rooted at the model being validated, so the
    path already includes the envelope segments (``data.section.items.0.id``)
    for search responses and just the field name (``id``) for the per-item
    endpoint. Both are useful as-is — adding an extra prefix would either
    duplicate or mis-attribute the path.
    """
    first = exc.errors()[0]
    path = ".".join(str(p) for p in first["loc"])
    return WallapopSchemaDrift(
        field_path=path or "<root>",
        detail=first["msg"],
    )


# ─────────────────────────────────────────────────────────────────────────
# Default request callable — wraps curl_cffi
# ─────────────────────────────────────────────────────────────────────────


def _build_default_request(
    *,
    cookies_path: Path,
    base_url: str,
    timeout_s: float,
) -> WallapopRequestCallable:
    """Construct the production :data:`WallapopRequestCallable` backed
    by ``curl_cffi.requests.AsyncSession`` with browser-TLS
    impersonation, and a transparent session-refresh dance on 401.

    The callable closes over a ``_RefreshingSession`` that holds the
    cookie state in memory. On 401 from the API, it calls
    ``federated-session``, parses ``Set-Cookie`` headers from the
    response, updates the in-memory cookies, persists them back to
    ``cookies_path``, and retries the original request once. This
    mirrors what the SPA does in the browser and is what keeps a
    long-running daemon from failing every ~5 minutes when the
    Keycloak JWT expires.

    The ``curl_cffi`` import is lazy so the C-extension stays out of
    the test import graph and outside the NFR-M1 adapter-discipline
    lint blast radius for non-adapter modules.
    """
    session = _RefreshingSession(
        cookies_path=cookies_path,
        base_url=base_url,
        timeout_s=timeout_s,
    )
    return session.call


class _RefreshingSession:
    """Stateful wallapop_api transport: cookies in memory, automatic
    federated-session refresh on 401.

    Production-only — tests bypass this by injecting their own
    callable at the :data:`WallapopRequestCallable` seam.
    """

    def __init__(
        self,
        *,
        cookies_path: Path,
        base_url: str,
        timeout_s: float,
    ) -> None:
        self._cookies_path = cookies_path
        self._base_url = base_url
        self._timeout = timeout_s
        # Lazy: loaded on first call so a fetcher built before
        # `salvager login wallapop` runs doesn't crash at import.
        self._jar: Any = None  # httpx.Cookies; typed loose to avoid module-level import
        self._cookies: dict[str, str] | None = None
        #: mtime captured the last time we loaded (or wrote) cookies. Used
        #: to spot operator re-logins: ``WallapopFallbackFetcher`` watches
        #: the same mtime to flip the API path back on after expiry, and
        #: without this re-read the in-memory jar would still hold the
        #: stale (refresh-token-already-revoked) cookies — the daemon
        #: would 401-loop until a process restart.
        self._cookie_file_mtime: float | None = None
        # Serializes the 401 refresh dance so concurrent search()/fetch()
        # calls don't fire two federated-session refreshes in parallel
        # and clobber each other's rotated cookies.
        self._refresh_lock = asyncio.Lock()
        self._log = get_logger("adapter.wallapop_api")

    def _ensure_cookies_loaded(self) -> dict[str, str]:
        if self._cookies is None or self._disk_is_newer():
            self._jar = load_cookies(self._cookies_path)
            self._cookies = {c.name: c.value for c in self._jar.jar}
            self._cookie_file_mtime = self._current_mtime()
        return self._cookies

    def _current_mtime(self) -> float | None:
        try:
            return self._cookies_path.stat().st_mtime
        except OSError:
            # File transiently missing (e.g., mid-rename). Skip the
            # snapshot rather than crash; next call will retry.
            return None

    def _disk_is_newer(self) -> bool:
        """Has ``cookies.txt`` been rewritten since our last load?

        Catches operator re-logins (``salvager login wallapop`` writes a
        fresh file with a newer mtime); the in-process refresh-dance
        write also bumps the snapshot inline so it doesn't trip this.
        """
        if self._cookie_file_mtime is None:
            return False
        current = self._current_mtime()
        if current is None:
            return False
        return current > self._cookie_file_mtime

    async def call(self, path: str, params: dict[str, str]) -> WallapopResponse:
        cookies = self._ensure_cookies_loaded()
        response = await self._do_request(path, params, cookies)
        if response.status_code != 401:
            return response

        # 401 → refresh dance → retry once. Serialize through the lock
        # so concurrent callers don't double-refresh; once inside the
        # lock, re-check with the current cookies first — another caller
        # may have already rotated tokens while we waited. If refresh
        # itself fails, let the original 401 propagate (caller raises
        # WallapopSessionExpired and operator re-runs `salvager login
        # wallapop`).
        async with self._refresh_lock:
            retry_response = await self._do_request(path, params, self._cookies or {})
            if retry_response.status_code != 401:
                return retry_response
            refreshed = await self._refresh_session()
            if not refreshed:
                return response
            return await self._do_request(path, params, self._cookies or {})

    async def _do_request(
        self,
        path: str,
        params: dict[str, str],
        cookies: dict[str, str],
    ) -> WallapopResponse:
        from curl_cffi import requests as curl_requests

        headers = _build_request_headers(cookies)
        async with curl_requests.AsyncSession(impersonate="chrome131") as session:
            response = await session.get(
                f"{self._base_url}{path}",
                params=params,
                cookies=cookies,
                headers=headers,
                timeout=self._timeout,
            )

        try:
            data: Any = response.json() if response.status_code < 400 else None
        except Exception:
            data = None

        return WallapopResponse(
            status_code=int(response.status_code),
            text=response.text or "",
            json_data=data,
        )

    async def _refresh_session(self) -> bool:
        """Hit ``federated-session`` with current cookies; on 200, lift
        ``accessToken`` + ``__Secure-next-auth.session-token`` out of
        the Set-Cookie headers, update memory + disk, return True.

        Returns False when refresh fails for any reason (network,
        non-200, missing cookies in response). The caller decides
        whether to surface 401 to the operator.
        """
        from curl_cffi import requests as curl_requests

        assert self._cookies is not None  # _ensure_cookies_loaded called first
        try:
            async with curl_requests.AsyncSession(impersonate="chrome131") as session:
                response = await session.get(
                    _FEDERATED_SESSION_URL,
                    cookies=self._cookies,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "es-ES,es;q=0.9",
                        "Referer": "https://es.wallapop.com/",
                    },
                    timeout=self._timeout,
                )
        except Exception as exc:
            self._log.warning(
                "wallapop_session_refresh_failed",
                extra={"error_class": exc.__class__.__name__, "marketplace": "wallapop"},
            )
            return False

        if response.status_code != 200:
            self._log.warning(
                "wallapop_session_refresh_failed",
                extra={"status_code": response.status_code, "marketplace": "wallapop"},
            )
            return False

        # ``curl_cffi`` exposes Set-Cookie cookies on the response's
        # cookies jar (matches the requests API). Lift the two
        # rotating tokens.
        rotated: dict[str, str] = {}
        for cookie in response.cookies.jar:
            if cookie.name in _ROTATING_COOKIE_NAMES and cookie.value:
                rotated[cookie.name] = cookie.value

        if not rotated:
            self._log.warning(
                "wallapop_session_refresh_missing_cookies",
                extra={"marketplace": "wallapop"},
            )
            return False

        # In-memory update.
        self._cookies = {**self._cookies, **rotated}
        # Disk update — best-effort. If this fails we still benefit
        # in-memory for the rest of this process. Snapshot the new
        # mtime so the next ``_ensure_cookies_loaded`` doesn't re-read
        # what we just wrote ourselves.
        try:
            write_cookies(
                self._cookies_path,
                name_value_pairs=self._cookies,
                template_jar=self._jar,
            )
            self._cookie_file_mtime = self._current_mtime()
        except Exception as exc:
            self._log.warning(
                "wallapop_session_refresh_persist_failed",
                extra={"error_class": exc.__class__.__name__, "marketplace": "wallapop"},
            )

        self._log.info(
            "wallapop_session_refreshed",
            extra={"marketplace": "wallapop", "rotated_cookies": sorted(rotated.keys())},
        )
        return True


def _build_request_headers(cookies: dict[str, str]) -> dict[str, str]:
    """Mint the six application-level headers the SPA sends alongside
    cookies. ``Authorization: Bearer`` is derived from the ``accessToken``
    cookie value (a JWT); ``mpid`` / ``trackinguserid`` from
    ``trackingUserId``; ``x-deviceid`` from ``device_id``. ``Accept``,
    ``x-appversion``, and the ``deviceos`` pair are constants.

    Wallapop's WAF cross-checks these against the cookie jar; sending
    them is the difference between HTTP 200 and HTTP 403 in production
    (verified empirically 2026-05-19).
    """
    access_token = cookies.get("accessToken", "")
    tracking_user_id = cookies.get("trackingUserId", "")
    device_id = cookies.get("device_id", "")

    return {
        "Accept": "application/json; sequence=v2",
        "Authorization": f"Bearer {access_token}",
        "mpid": tracking_user_id,
        "trackinguserid": tracking_user_id,
        "x-deviceid": device_id,
        "x-appversion": _X_APPVERSION,
        "deviceos": _DEVICEOS_WEB,
        "x-deviceos": _DEVICEOS_WEB,
    }
