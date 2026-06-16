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

from salvager.adapters.asyncio_scheduler.scheduler import AsyncioScheduler
from salvager.adapters.ebay_api.fetcher import EbayApiFetcher
from salvager.adapters.ebay_api.quota import DailyQuotaTracker
from salvager.adapters.ebay_api.tokens import OAuthTokenStore
from salvager.adapters.llm_cache_sqlite.cache import (
    DEFAULT_CACHE_FILENAME,
    CachingListingEvaluator,
    SqliteLlmEvalCache,
)
from salvager.adapters.llm_claude.evaluator import ClaudeHaikuEvaluator
from salvager.adapters.llm_gemini.evaluator import GeminiFlashEvaluator
from salvager.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from salvager.adapters.sqlite_store.connection import open_connection
from salvager.adapters.sqlite_store.migrations import (
    MigrationRunner,
    db_path_under,
)
from salvager.adapters.sqlite_store.phase2_state_reader import (
    SqlitePhase2StateReader,
)
from salvager.adapters.sqlite_store.store import SqliteStore
from salvager.adapters.telegram_bot.surface import TelegramBotSurface
from salvager.adapters.tinyfish_browser.ebay_checkout import EbayCheckoutFlow
from salvager.adapters.tinyfish_browser.marketplace_dispatch import (
    MarketplaceDispatchingBrowser,
    MarketplaceDispatchingPageFetcher,
)
from salvager.adapters.tinyfish_browser.wallapop_pay import WallapopPayFlow
from salvager.adapters.wallapop_api.fetcher import WallapopApiFetcher
from salvager.adapters.wallapop_tinyfish.fetcher import (
    WallapopTinyfishFetcher,
)
from salvager.config.config_yaml import ConfigModel, load_config
from salvager.config.env import EnvSettings
from salvager.config.wishlist_yaml import load_wishlist
from salvager.domain.listing import Listing, SearchQuery
from salvager.domain.prompts import PROMPT_VERSION
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.scheduler import Scheduler
from salvager.interfaces.store import EntryKey
from salvager.observability.logging import get_logger
from salvager.orchestration.buy_orchestrator import BuyOrchestrator, WishlistLoader
from salvager.orchestration.callback_handler import (
    DEFAULT_SNOOZE_HOURS,
    CallbackDispatcher,
)
from salvager.orchestration.circuit_breaker import CircuitBreaker
from salvager.orchestration.daemon import Daemon
from salvager.orchestration.degradation_reporter import DegradationReporter
from salvager.orchestration.health_state import HealthState
from salvager.orchestration.phase2_parsers import default_price_parser_registry
from salvager.orchestration.phase2_preflight import Phase2Preflight
from salvager.orchestration.poll_loop import run_poll_cycle
from salvager.orchestration.reconciler import Reconciler
from salvager.orchestration.smoke_job import (
    build_scheduled_smoke_task,
    build_smoke_runner,
)
from salvager.orchestration.smoke_test import DEFAULT_SMOKE_FIXTURES_DIR
from salvager.orchestration.wallapop_fallback import WallapopFallbackFetcher

#: Path under ``data_dir`` where Story 2.9 writes the Wallapop cookie jar
#: in Netscape ``cookies.txt`` format. The unofficial-API adapter reads
#: this file via :func:`salvager.adapters.wallapop_api.cookies.load_cookies`.
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
    are flushed before the process exits).

    ``telegram`` + ``dispatcher`` are exposed so the daemon entry-point
    can run :meth:`TelegramSurface.listen_callbacks` concurrently with
    the scheduler — view/skip/snooze taps drive Phase 1 audit + state
    + keyboard edits through the dispatcher. The dispatcher holds a
    fully-wired :class:`BuyOrchestrator`, so Phase 2 Comprar taps
    execute a real checkout (gated by ``phase2.enabled`` per wishlist
    entry).

    The ``_phase2_*`` fields are the OS-resource handles owned by the
    buy orchestrator's collaborators (two SQLite connections, two
    :class:`AsyncTinyFish` HTTP clients, and the reconciler's
    dispatching fetcher which owns the eBay refetch HTTP client). They
    live on the dataclass so :meth:`aclose` can close them on shutdown.
    """

    daemon: Daemon
    store: SqliteStore
    cache: SqliteLlmEvalCache
    telegram: TelegramBotSurface
    dispatcher: CallbackDispatcher
    _phase2_audit_writer: Phase2AuditWriter
    _phase2_state_reader: SqlitePhase2StateReader
    _phase2_wallapop_pay: WallapopPayFlow
    _phase2_ebay_checkout: EbayCheckoutFlow
    _phase2_recon_fetcher: MarketplaceDispatchingPageFetcher

    async def aclose(self) -> None:
        """Close every adapter that owns OS resources. Idempotent —
        the store already guards double-close internally.

        Each close is isolated: a failure in one still lets the rest
        run, so a flaky adapter on shutdown can't leak the others'
        connections. The first error (if any) is re-raised after every
        handle has been given its chance to close, preserving the
        shutdown-surfaces-errors contract.
        """
        closers = (
            self._phase2_wallapop_pay.close,
            self._phase2_ebay_checkout.close,
            self._phase2_recon_fetcher.aclose,
            self._phase2_audit_writer.close,
            self._phase2_state_reader.close,
            self.cache.close,
            self.store.close,
        )
        log = get_logger("orchestration.composer")
        errors: list[Exception] = []
        for close in closers:
            try:
                await close()
            except Exception as exc:  # keep closing the rest before re-raising
                log.exception("composed_daemon_aclose_failed", extra={"closer": close.__qualname__})
                errors.append(exc)
        if errors:
            raise errors[0]


def compose_daemon(
    env: EnvSettings,
    *,
    config_path: str | Path,
    wishlist_path: str | Path,
) -> ComposedDaemon:
    """Build a fully-wired :class:`Daemon` for the given environment.

    The function:

    1. Loads ``config.yaml`` + ``wishlist.yaml``.
    2. Runs SQLite migrations against ``{data_dir}/salvager.db``.
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

    # Credential-gated marketplace enablement. Both the polling job and
    # the Phase 2 reconciler share these paths, so we resolve them once.
    wallapop_cookies_path = data_dir / WALLAPOP_COOKIES_RELPATH
    ebay_tokens_path = data_dir / EBAY_OAUTH_TOKENS_RELPATH
    wallapop_enabled = wallapop_cookies_path.exists()
    ebay_enabled = ebay_tokens_path.exists()

    # One shared :class:`DailyQuotaTracker` for eBay — the polling job
    # and the Phase 2 reconciler both hit the same daily quota, so the
    # in-memory counter must be a single instance across them.
    ebay_quota = DailyQuotaTracker(config.ebay.daily_request_quota) if ebay_enabled else None

    wallapop_job = _build_wallapop_job(
        env=env,
        config=config,
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
        data_dir=data_dir,
        wishlist=wishlist,
        evaluator=evaluator,
        store=store,
        telegram=telegram,
        quota=ebay_quota,
        log=log,
    )

    if wallapop_job is None and ebay_job is None:
        raise NoMarketplacesEnabledError(
            "no marketplace credentials found: run "
            "`salvager login wallapop` and/or "
            "`salvager login ebay` first"
        )

    # Phase 2 buy orchestrator — operator-confirmed checkout. The
    # orchestrator fires only when an operator taps the Comprar button
    # on a Phase 2 alert; it never auto-buys. Marketplace dispatch lives
    # in the per-marketplace wrappers (see marketplace_dispatch.py) so
    # the orchestrator can hold a single browser + single fetcher.
    # Built before the Daemon because its audit-writer + state-reader feed
    # the scheduled smoke-test job below.
    phase2 = _build_buy_orchestrator(
        env=env,
        config=config,
        data_dir=data_dir,
        wishlist_path=Path(wishlist_path),
        store=store,
        telegram=telegram,
        reporter=reporter,
        wallapop_cookies_path=wallapop_cookies_path if wallapop_enabled else None,
        ebay_tokens_path=ebay_tokens_path if ebay_enabled else None,
        ebay_quota=ebay_quota,
    )

    # Phase 2 price-parser smoke-test. Keeps the preflight freshness signal
    # green so opted-in entries stay armable: one run on startup + an
    # hour-gated daily run at config.phase2.smoke_test_hour_utc. Reuses the
    # buy bundle's audit-writer / state-reader so the result lands on the
    # same phase2_state the preflight reads.
    smoke_runner = build_smoke_runner(
        fixtures_dir=DEFAULT_SMOKE_FIXTURES_DIR,
        parsers=default_price_parser_registry(),
        audit_writer=phase2.audit_writer,
        state_reader=phase2.state_reader,
        reporter=reporter,
        tolerance_eur=config.phase2.reconciliation_tolerance_eur,
        tolerance_pct=config.phase2.reconciliation_tolerance_pct,
    )
    smoke_task = build_scheduled_smoke_task(
        runner=smoke_runner,
        state_reader=phase2.state_reader,
        hour_utc=config.phase2.smoke_test_hour_utc,
    )

    daemon = Daemon(
        scheduler=scheduler,
        wallapop_job=wallapop_job,
        wallapop_cadence_minutes=config.schedule.wallapop_minutes,
        ebay_job=ebay_job,
        ebay_cadence_minutes=config.schedule.ebay_minutes,
        smoke_job=smoke_task,
        smoke_startup=smoke_runner,
    )

    dispatcher = CallbackDispatcher(
        store=store,
        surface=telegram,
        buy_orchestrator=phase2.orchestrator,
        snooze_hours=DEFAULT_SNOOZE_HOURS,
    )

    return ComposedDaemon(
        daemon=daemon,
        store=store,
        cache=cache,
        telegram=telegram,
        dispatcher=dispatcher,
        _phase2_audit_writer=phase2.audit_writer,
        _phase2_state_reader=phase2.state_reader,
        _phase2_wallapop_pay=phase2.wallapop_pay,
        _phase2_ebay_checkout=phase2.ebay_checkout,
        _phase2_recon_fetcher=phase2.recon_fetcher,
    )


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


def build_inner_evaluator(env: EnvSettings, config: ConfigModel) -> ListingEvaluator:
    """Construct the concrete :class:`ListingEvaluator` for ``config.llm.provider``.

    Shared by :func:`_build_evaluator` and the ``test-search`` /
    ``explain`` CLI commands so the provider dispatch lives in one
    place (NFR-I3).

    Raises ``ValueError`` if the selected provider's API key is missing
    from the environment, or the provider literal slipped past the
    ``LLMProvider`` schema check (defensive — should not happen at
    runtime).
    """
    provider = config.llm.provider
    if provider == "gemini-flash":
        if env.GEMINI_API_KEY is None:
            raise ValueError(
                "llm.provider=gemini-flash selected but GEMINI_API_KEY is not set in .env"
            )
        return GeminiFlashEvaluator(api_key=env.GEMINI_API_KEY)
    if provider == "claude-haiku":
        if env.ANTHROPIC_API_KEY is None:
            raise ValueError(
                "llm.provider=claude-haiku selected but ANTHROPIC_API_KEY is not set in .env"
            )
        return ClaudeHaikuEvaluator(api_key=env.ANTHROPIC_API_KEY)
    if provider == "gpt-4o-mini":
        raise NotImplementedError(
            "llm.provider=gpt-4o-mini selected but the OpenAI adapter has not been built yet "
            "(see interfaces/listing_evaluator.py). Pick 'gemini-flash' or 'claude-haiku'."
        )
    raise ValueError(f"unknown llm.provider={provider!r}")


def _build_evaluator(
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
) -> tuple[SqliteLlmEvalCache, CachingListingEvaluator]:
    """Build the configured evaluator and wrap it in the SQLite cache."""
    inner = build_inner_evaluator(env, config)
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
    config: ConfigModel,
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

    api_fetcher: PageFetcher = WallapopApiFetcher(
        cookies_path=cookies_path,
        latitude=config.wallapop.latitude,
        longitude=config.wallapop.longitude,
    )
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
    data_dir: Path,
    wishlist: Wishlist,
    evaluator: CachingListingEvaluator,
    store: SqliteStore,
    telegram: TelegramBotSurface,
    quota: DailyQuotaTracker | None,
    log: object,
) -> Callable[[], Awaitable[None]] | None:
    """Build the eBay poll closure, or return None when OAuth tokens are missing.

    ``quota`` is the daily-request tracker shared with the Phase 2
    reconciler (built once in :func:`compose_daemon`). ``None`` is the
    same gate as the file check — eBay is disabled.
    """
    tokens_path = data_dir / EBAY_OAUTH_TOKENS_RELPATH
    if quota is None or not tokens_path.exists():
        log.info(  # type: ignore[attr-defined]
            "ebay_disabled_no_tokens",
            extra={"tokens_path": str(tokens_path)},
        )
        return None

    token_store = OAuthTokenStore(tokens_path)
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


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 buy orchestrator wiring
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Phase2Bundle:
    """Output of :func:`_build_buy_orchestrator` — the orchestrator plus
    the OS-resource handles that :class:`ComposedDaemon` closes on
    shutdown (two SQLite connections, two AsyncTinyFish clients, and the
    reconciler's dispatching fetcher which owns the eBay refetch HTTP
    client)."""

    orchestrator: BuyOrchestrator
    audit_writer: Phase2AuditWriter
    state_reader: SqlitePhase2StateReader
    wallapop_pay: WallapopPayFlow
    ebay_checkout: EbayCheckoutFlow
    recon_fetcher: MarketplaceDispatchingPageFetcher


class _UnavailableMarketplaceFetcher(PageFetcher):
    """``PageFetcher`` stand-in for a marketplace whose credentials are
    absent at composition time. Construction is side-effect-free; every
    method raises so the code path is loud if it gets exercised. The
    dispatcher should never route here in practice — alerts only come
    from polled marketplaces, and the reconciler's pre-buy refetch keys
    off the listing URL's host."""

    def __init__(self, marketplace: str) -> None:
        self._marketplace = marketplace

    async def search(self, query: SearchQuery) -> list[Listing]:  # noqa: ARG002
        raise RuntimeError(
            f"reconciler tried to search() {self._marketplace} but no "
            f"{self._marketplace} credentials are configured for this daemon"
        )

    async def fetch(self, listing_url: str) -> Listing:
        raise RuntimeError(
            f"reconciler tried to fetch({listing_url!r}) but no "
            f"{self._marketplace} credentials are configured for this daemon"
        )


def _make_wishlist_loader(wishlist_path: Path) -> WishlistLoader:
    """Build the ``EntryKey → WishlistEntry | None`` closure for the
    BuyOrchestrator.

    The orchestrator's pre-flight resolves entries at tap time, not at
    daemon-startup time, so an operator who edits ``wishlist.yaml``
    between alert and tap sees the updated state respected. The closure
    re-reads the file but short-circuits to a parsed-and-cached
    ``Wishlist`` when the file's mtime hasn't moved.
    """
    cache: dict[float, Wishlist] = {}

    def _load_current() -> Wishlist:
        mtime = wishlist_path.stat().st_mtime
        if mtime not in cache:
            cache.clear()  # keep at most one entry
            cache[mtime] = load_wishlist(wishlist_path)
        return cache[mtime]

    def loader(entry_key: EntryKey) -> WishlistEntry | None:
        for entry in _load_current().entries:
            if entry.entry_key == entry_key:
                return entry
        return None

    return loader


def _build_buy_orchestrator(
    *,
    env: EnvSettings,
    config: ConfigModel,
    data_dir: Path,
    wishlist_path: Path,
    store: SqliteStore,
    telegram: TelegramBotSurface,
    reporter: DegradationReporter,
    wallapop_cookies_path: Path | None,
    ebay_tokens_path: Path | None,
    ebay_quota: DailyQuotaTracker | None,
) -> _Phase2Bundle:
    """Wire the Phase 2 buy orchestrator with its nine collaborators.

    The orchestrator drives one buy per operator Comprar tap — no
    autonomous trigger anywhere in the pipeline.

    ``wallapop_cookies_path`` and ``ebay_tokens_path`` reflect which
    marketplaces the daemon was composed with: a ``None`` value swaps
    that marketplace's reconciliation fetcher for a stub that raises
    if invoked, so the orchestrator builds cleanly on single-marketplace
    deployments. The dispatcher never routes to a missing side in
    practice because alerts only come from polled marketplaces.

    ``ebay_quota`` is the shared :class:`DailyQuotaTracker` from
    :func:`compose_daemon` so polling and reconciliation charge a
    single in-memory counter against the same daily limit.
    """
    db_path = db_path_under(data_dir)
    audit_writer = Phase2AuditWriter(db_path)
    state_reader = SqlitePhase2StateReader(db_path)

    preflight = Phase2Preflight(
        state_reader=state_reader,
        circuit_breaker_threshold=config.phase2.circuit_breaker_threshold,
    )
    circuit_breaker = CircuitBreaker(
        audit_writer=audit_writer,
        state_reader=state_reader,
        reporter=reporter,
        threshold=config.phase2.circuit_breaker_threshold,
    )

    # Reconciler's pre-buy refetch uses fresh per-marketplace fetchers —
    # cheap, stateless duplicates of what _build_*_job builds internally.
    # Reusing the poll-time instances would require returning them from
    # the per-marketplace builders, which is a bigger refactor for no
    # operational benefit.
    wallapop_recon_fetcher: PageFetcher = (
        WallapopApiFetcher(
            cookies_path=wallapop_cookies_path,
            latitude=config.wallapop.latitude,
            longitude=config.wallapop.longitude,
        )
        if wallapop_cookies_path is not None
        else _UnavailableMarketplaceFetcher("wallapop")
    )
    ebay_recon_fetcher: PageFetcher = (
        EbayApiFetcher(
            token_store=OAuthTokenStore(ebay_tokens_path),
            app_id=env.EBAY_APP_ID,
            cert_id=env.EBAY_CERT_ID,
            quota=ebay_quota,
        )
        if ebay_tokens_path is not None and ebay_quota is not None
        else _UnavailableMarketplaceFetcher("ebay")
    )
    recon_fetcher = MarketplaceDispatchingPageFetcher(
        wallapop=wallapop_recon_fetcher,
        ebay=ebay_recon_fetcher,
    )
    reconciler = Reconciler(
        cross_source_fetcher=recon_fetcher,
        tolerance_eur=config.phase2.reconciliation_tolerance_eur,
        tolerance_pct=config.phase2.reconciliation_tolerance_pct,
    )

    # Both browser flows share the TinyFish API key — marketplace login
    # happens inside the browser session, not at construction.
    wallapop_pay = WallapopPayFlow(api_key=env.TINYFISH_API_KEY)
    ebay_checkout = EbayCheckoutFlow(api_key=env.TINYFISH_API_KEY)
    browser = MarketplaceDispatchingBrowser(
        wallapop=wallapop_pay,
        ebay=ebay_checkout,
    )

    orchestrator = BuyOrchestrator(
        preflight=preflight,
        reconciler=reconciler,
        browser=browser,
        circuit_breaker=circuit_breaker,
        audit_writer=audit_writer,
        telegram_surface=telegram,
        store=store,
        reporter=reporter,
        wishlist_loader=_make_wishlist_loader(wishlist_path),
    )
    return _Phase2Bundle(
        orchestrator=orchestrator,
        audit_writer=audit_writer,
        state_reader=state_reader,
        wallapop_pay=wallapop_pay,
        ebay_checkout=ebay_checkout,
        recon_fetcher=recon_fetcher,
    )


__all__ = [
    "EBAY_OAUTH_TOKENS_RELPATH",
    "WALLAPOP_COOKIES_RELPATH",
    "ComposedDaemon",
    "NoMarketplacesEnabledError",
    "build_inner_evaluator",
    "compose_daemon",
]
