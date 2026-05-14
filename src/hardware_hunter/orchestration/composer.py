"""Daemon composition root — wires every adapter into a :class:`Daemon`.

This module is the only place in the codebase that knows the concrete
adapter classes, their constructor arguments, and how operator state
(``data_dir``, ``config_dir``) is laid out on disk. Everything else
composes against the interface ABCs.

Graceful degradation
--------------------
Telegram + Gemini are mandatory: without delivery and evaluation,
the daemon has no work to do. A missing credential there fails fast
through :func:`load_env_or_exit` (exit ``4``).

Marketplace adapters degrade independently:

- Wallapop is enabled iff ``{data_dir}/auth/wallapop_cookies.json`` exists.
  Missing file → log ``wallapop_disabled_no_cookies`` and skip; the
  daemon polls only eBay.
- eBay is enabled iff ``{data_dir}/auth/oauth_tokens.json`` exists.
  Missing file → log ``ebay_disabled_no_tokens`` and skip; the daemon
  polls only Wallapop.

If BOTH marketplaces are skipped, :class:`NoMarketplacesEnabledError`
is raised — the daemon CLI converts that into exit ``5`` (fatal
infra / no work to do).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from hardware_hunter.adapters.asyncio_scheduler.scheduler import AsyncioScheduler
from hardware_hunter.adapters.ebay_api.fetcher import EbayApiFetcher
from hardware_hunter.adapters.ebay_api.quota import DailyQuotaTracker
from hardware_hunter.adapters.ebay_api.tokens import OAuthTokenStore
from hardware_hunter.adapters.llm_cache_sqlite.cache import (
    DEFAULT_CACHE_FILENAME,
    CachingListingEvaluator,
    SqliteLlmEvalCache,
)
from hardware_hunter.adapters.llm_gemini.evaluator import GeminiFlashEvaluator
from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.adapters.sqlite_store.migrations import (
    MigrationRunner,
    db_path_under,
)
from hardware_hunter.adapters.sqlite_store.store import SqliteStore
from hardware_hunter.adapters.telegram_bot.surface import TelegramBotSurface
from hardware_hunter.adapters.wallapop_api.fetcher import WallapopApiFetcher
from hardware_hunter.adapters.wallapop_tinyfish.fetcher import (
    WallapopTinyfishFetcher,
)
from hardware_hunter.config.config_yaml import ConfigModel, load_config
from hardware_hunter.config.env import EnvSettings
from hardware_hunter.config.wishlist_yaml import load_wishlist
from hardware_hunter.domain.prompts import PROMPT_VERSION
from hardware_hunter.domain.wishlist import Wishlist
from hardware_hunter.interfaces.page_fetcher import PageFetcher
from hardware_hunter.interfaces.scheduler import Scheduler
from hardware_hunter.observability.logging import get_logger
from hardware_hunter.orchestration.daemon import Daemon
from hardware_hunter.orchestration.degradation_reporter import DegradationReporter
from hardware_hunter.orchestration.health_state import HealthState
from hardware_hunter.orchestration.poll_loop import run_poll_cycle
from hardware_hunter.orchestration.wallapop_fallback import WallapopFallbackFetcher

#: Path under ``data_dir`` where Story 2.9 writes the Wallapop cookie jar
#: in Netscape ``cookies.txt`` format. The unofficial-API adapter reads
#: this file via :func:`hardware_hunter.adapters.wallapop_api.cookies.load_cookies`.
WALLAPOP_COOKIES_RELPATH: Final[Path] = Path("auth") / "wallapop_cookies.txt"

#: Path under ``data_dir`` where Story 2.10 writes the eBay OAuth tokens.
EBAY_OAUTH_TOKENS_RELPATH: Final[Path] = Path("auth") / "oauth_tokens.json"


class NoMarketplacesEnabledError(RuntimeError):
    """Neither Wallapop cookies nor eBay tokens are present.

    The daemon has nothing to do. The CLI maps this to exit ``5``.
    """


@dataclass
class ComposedDaemon:
    """The output of :func:`compose_daemon` — the daemon plus the
    handles the CLI needs to close on shutdown (so the SQLite files
    are flushed before the process exits)."""

    daemon: Daemon
    store: SqliteStore
    cache: SqliteLlmEvalCache

    async def aclose(self) -> None:
        """Close every adapter that owns OS resources. Idempotent —
        the store already guards double-close internally."""
        await self.cache.close()
        await self.store.close()


def compose_daemon(
    env: EnvSettings,
    *,
    config_path: str | Path,
    wishlist_path: str | Path,
) -> ComposedDaemon:
    """Build a fully-wired :class:`Daemon` for the given environment.

    The function:

    1. Loads ``config.yaml`` + ``wishlist.yaml``.
    2. Runs SQLite migrations against ``{data_dir}/hardware_hunter.db``.
    3. Builds the LLM evaluator wrapped in the SQLite eval cache.
    4. Builds the Telegram surface.
    5. Conditionally builds the Wallapop + eBay jobs (skips a
       marketplace when its credential file is absent).
    6. Wraps it all in a :class:`Daemon` with the operator's cadences.

    Raises :class:`NoMarketplacesEnabledError` when neither marketplace
    has its credential file in place.
    """
    log = get_logger("orchestration.composer")
    config: ConfigModel = load_config(config_path)
    wishlist: Wishlist = load_wishlist(wishlist_path)

    data_dir: Path = Path(config.paths.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "auth").mkdir(parents=True, exist_ok=True)

    store = _build_store(data_dir)
    cache, evaluator = _build_evaluator(env, config, data_dir)
    telegram = TelegramBotSurface(
        bot_token=env.TELEGRAM_BOT_TOKEN,
        recipient_chat_id=env.TELEGRAM_CHAT_ID,
    )

    # The health-state cache + degradation reporter are shared across
    # every marketplace job — one NFR-R3 fan-out for the whole daemon.
    health_state = HealthState()
    reporter = DegradationReporter(
        telegram_surface=telegram,
        health_state=health_state,
        dedup_window_seconds=config.observability.degradation_dedup_window_seconds,
    )

    scheduler: Scheduler = AsyncioScheduler()

    wallapop_job = _build_wallapop_job(
        env=env,
        data_dir=data_dir,
        wishlist=wishlist,
        evaluator=evaluator,
        store=store,
        telegram=telegram,
        reporter=reporter,
        log=log,
    )
    ebay_job = _build_ebay_job(
        env=env,
        config=config,
        data_dir=data_dir,
        wishlist=wishlist,
        evaluator=evaluator,
        store=store,
        telegram=telegram,
        log=log,
    )

    if wallapop_job is None and ebay_job is None:
        raise NoMarketplacesEnabledError(
            "no marketplace credentials found: run "
            "`hardware-hunter login wallapop` and/or "
            "`hardware-hunter login ebay` first"
        )

    daemon = Daemon(
        scheduler=scheduler,
        wallapop_job=wallapop_job,
        wallapop_cadence_minutes=config.schedule.wallapop_minutes,
        ebay_job=ebay_job,
        ebay_cadence_minutes=config.schedule.ebay_minutes,
    )
    return ComposedDaemon(daemon=daemon, store=store, cache=cache)


# ─────────────────────────────────────────────────────────────────────────
# Per-adapter builders
# ─────────────────────────────────────────────────────────────────────────


def _build_store(data_dir: Path) -> SqliteStore:
    """Open + migrate the canonical SQLite store under ``data_dir``."""
    db_path = db_path_under(data_dir)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    return SqliteStore(db_path)


def _build_evaluator(
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
) -> tuple[SqliteLlmEvalCache, CachingListingEvaluator]:
    """Build the Gemini evaluator and wrap it in the SQLite cache."""
    inner = GeminiFlashEvaluator(api_key=env.GEMINI_API_KEY)
    cache = SqliteLlmEvalCache(
        data_dir / DEFAULT_CACHE_FILENAME,
        ttl_normal=_hours(config.llm.cache_ttl_hours),
        ttl_low_confidence=_hours(config.llm.cache_ttl_hours_low_confidence),
    )
    evaluator = CachingListingEvaluator(inner, cache, PROMPT_VERSION)
    return cache, evaluator


def _build_wallapop_job(
    *,
    env: EnvSettings,
    data_dir: Path,
    wishlist: Wishlist,
    evaluator: CachingListingEvaluator,
    store: SqliteStore,
    telegram: TelegramBotSurface,
    reporter: DegradationReporter,
    log: object,
) -> Callable[[], Awaitable[None]] | None:
    """Build the Wallapop poll closure, or return None when cookies are missing."""
    cookies_path = data_dir / WALLAPOP_COOKIES_RELPATH
    if not cookies_path.exists():
        log.info(  # type: ignore[attr-defined]
            "wallapop_disabled_no_cookies",
            extra={"cookies_path": str(cookies_path)},
        )
        return None

    api_fetcher: PageFetcher = WallapopApiFetcher(cookies_path=cookies_path)
    tinyfish_fetcher: PageFetcher = WallapopTinyfishFetcher(api_key=env.TINYFISH_API_KEY)
    fallback = WallapopFallbackFetcher(
        api_fetcher=api_fetcher,
        tinyfish_fetcher=tinyfish_fetcher,
        reporter=reporter,
        cookies_path=cookies_path,
    )

    async def _wallapop_cycle() -> None:
        await run_poll_cycle(
            "wallapop",
            wishlist=wishlist,
            fetcher=fallback,
            evaluator=evaluator,
            store=store,
            telegram=telegram,
        )
        # Persist the API-path health to `_meta` so `health` can report
        # "wallapop_api degraded / wallapop_tinyfish healthy" without
        # the daemon's in-memory state (Story 4.4 / AR14).
        await store.set_meta(
            "wallapop_api_status",
            "healthy" if fallback.health.api_attempt_enabled() else "degraded",
        )

    return _wallapop_cycle


def _build_ebay_job(
    *,
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
    wishlist: Wishlist,
    evaluator: CachingListingEvaluator,
    store: SqliteStore,
    telegram: TelegramBotSurface,
    log: object,
) -> Callable[[], Awaitable[None]] | None:
    """Build the eBay poll closure, or return None when OAuth tokens are missing."""
    tokens_path = data_dir / EBAY_OAUTH_TOKENS_RELPATH
    if not tokens_path.exists():
        log.info(  # type: ignore[attr-defined]
            "ebay_disabled_no_tokens",
            extra={"tokens_path": str(tokens_path)},
        )
        return None

    token_store = OAuthTokenStore(tokens_path)
    quota = DailyQuotaTracker(config.ebay.daily_request_quota)
    fetcher: PageFetcher = EbayApiFetcher(
        token_store=token_store,
        app_id=env.EBAY_APP_ID,
        cert_id=env.EBAY_CERT_ID,
        quota=quota,
    )

    async def _ebay_cycle() -> None:
        await run_poll_cycle(
            "ebay",
            wishlist=wishlist,
            fetcher=fetcher,
            evaluator=evaluator,
            store=store,
            telegram=telegram,
        )

    return _ebay_cycle


def _hours(n: int) -> timedelta:
    return timedelta(hours=n)


__all__ = [
    "EBAY_OAUTH_TOKENS_RELPATH",
    "WALLAPOP_COOKIES_RELPATH",
    "ComposedDaemon",
    "NoMarketplacesEnabledError",
    "compose_daemon",
]
