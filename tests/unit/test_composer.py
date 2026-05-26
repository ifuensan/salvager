"""Tests for the daemon composition root — graceful-degrade matrix.

The composer is the only module that knows the concrete adapter
classes; here we verify it builds the right set of jobs given the
on-disk credential files.

We do not instantiate the network-touching adapters directly — building
their constructors requires real credentials we don't have in CI.
Instead we monkeypatch the adapter classes at the composer-module
attribute level. The composer's logic (presence check → build or skip)
is what we exercise.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from salvager.config.env import EnvSettings, reset_env_cache
from salvager.orchestration import composer as composer_module
from salvager.orchestration.composer import (
    EBAY_OAUTH_TOKENS_RELPATH,
    WALLAPOP_COOKIES_RELPATH,
    NoMarketplacesEnabledError,
    compose_daemon,
)

_VALID_CONFIG = textwrap.dedent(
    """\
    schedule:
      wallapop_minutes: 15
      ebay_minutes: 30
    paths:
      data_dir: {data_dir}
      config_dir: {config_dir}
    """
)

_VALID_WISHLIST = textwrap.dedent(
    """\
    entries:
      - manufacturer: Western Digital
        model: WD Red Plus 4TB
        ref: WD40EFPX
        type: hdd
        keywords: [wd red plus 4tb]
        max_price_solo: 70.00
        max_price_in_device: 200.00
        confidence_threshold: medium
    """
)


@pytest.fixture(autouse=True)
def _reset_env_cache() -> Iterator[None]:
    reset_env_cache()
    yield
    reset_env_cache()


@pytest.fixture
def env() -> EnvSettings:
    """An EnvSettings hydrated from in-memory values (no .env file needed)."""
    return EnvSettings(
        TELEGRAM_BOT_TOKEN=SecretStr("bot-token"),
        TELEGRAM_CHAT_ID=12345,
        GEMINI_API_KEY=SecretStr("gemini-key"),
        EBAY_APP_ID=SecretStr("ebay-app"),
        EBAY_CERT_ID=SecretStr("ebay-cert"),
        EBAY_DEV_ID=SecretStr("ebay-dev"),
        TINYFISH_API_KEY=SecretStr("tinyfish-key"),
    )


@pytest.fixture
def fake_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (config_dir, data_dir) with config.yaml + wishlist.yaml."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        _VALID_CONFIG.format(data_dir=str(data_dir), config_dir=str(config_dir)),
        encoding="utf-8",
    )
    (config_dir / "wishlist.yaml").write_text(_VALID_WISHLIST, encoding="utf-8")
    return config_dir, data_dir


@pytest.fixture
def stub_adapters(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the heavy adapter classes with no-op stubs.

    We swap each adapter constructor for a class whose __init__ just
    records its args. This isolates the composer's wiring logic from
    the adapters' real construction requirements (network clients,
    schema-version checks, etc.).
    """
    recorded: dict[str, Any] = {}

    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            recorded.setdefault(type(self).__name__, []).append({"args": args, "kwargs": kwargs})

        async def search(self, query: Any) -> list[Any]:
            return []

        async def fetch(self, listing_url: str) -> Any:
            return None

        async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
            return None

        async def send(self, *args: Any, **kwargs: Any) -> int:
            return 0

        async def close(self) -> None:
            return None

    for name in (
        "GeminiFlashEvaluator",
        "TelegramBotSurface",
        "WallapopApiFetcher",
        "WallapopTinyfishFetcher",
        "EbayApiFetcher",
        "OAuthTokenStore",
        "DailyQuotaTracker",
    ):
        stub_class = type(name, (_Stub,), {})
        monkeypatch.setattr(composer_module, name, stub_class)

    return recorded


# ─────────────────────────────────────────────────────────────────────────
# Graceful-degrade matrix
# ─────────────────────────────────────────────────────────────────────────


def test_both_credentials_missing_raises(
    env: EnvSettings,
    fake_dirs: tuple[Path, Path],
    stub_adapters: dict[str, Any],
) -> None:
    config_dir, _ = fake_dirs
    with pytest.raises(NoMarketplacesEnabledError, match="no marketplace credentials"):
        compose_daemon(
            env,
            config_path=config_dir / "config.yaml",
            wishlist_path=config_dir / "wishlist.yaml",
        )


def test_only_wallapop_cookies_present_builds_wallapop_job(
    env: EnvSettings,
    fake_dirs: tuple[Path, Path],
    stub_adapters: dict[str, Any],
) -> None:
    config_dir, data_dir = fake_dirs
    (data_dir / WALLAPOP_COOKIES_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (data_dir / WALLAPOP_COOKIES_RELPATH).write_text("{}", encoding="utf-8")

    composed = compose_daemon(
        env,
        config_path=config_dir / "config.yaml",
        wishlist_path=config_dir / "wishlist.yaml",
    )
    try:
        # Wallapop is wired; eBay is not.
        assert composed.daemon._wallapop_job is not None
        assert composed.daemon._ebay_job is None
    finally:
        # Composed wraps a real SqliteStore + SqliteLlmEvalCache; close them.
        import asyncio

        asyncio.run(composed.aclose())


def test_only_ebay_tokens_present_builds_ebay_job(
    env: EnvSettings,
    fake_dirs: tuple[Path, Path],
    stub_adapters: dict[str, Any],
) -> None:
    config_dir, data_dir = fake_dirs
    (data_dir / EBAY_OAUTH_TOKENS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (data_dir / EBAY_OAUTH_TOKENS_RELPATH).write_text("{}", encoding="utf-8")

    composed = compose_daemon(
        env,
        config_path=config_dir / "config.yaml",
        wishlist_path=config_dir / "wishlist.yaml",
    )
    try:
        assert composed.daemon._wallapop_job is None
        assert composed.daemon._ebay_job is not None
    finally:
        import asyncio

        asyncio.run(composed.aclose())


def test_both_credentials_present_builds_both_jobs(
    env: EnvSettings,
    fake_dirs: tuple[Path, Path],
    stub_adapters: dict[str, Any],
) -> None:
    config_dir, data_dir = fake_dirs
    (data_dir / WALLAPOP_COOKIES_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (data_dir / WALLAPOP_COOKIES_RELPATH).write_text("{}", encoding="utf-8")
    (data_dir / EBAY_OAUTH_TOKENS_RELPATH).write_text("{}", encoding="utf-8")

    composed = compose_daemon(
        env,
        config_path=config_dir / "config.yaml",
        wishlist_path=config_dir / "wishlist.yaml",
    )
    try:
        assert composed.daemon._wallapop_job is not None
        assert composed.daemon._ebay_job is not None
        # Cadences come from config.yaml, not the daemon defaults.
        assert composed.daemon._wallapop_cadence_minutes == 15
        assert composed.daemon._ebay_cadence_minutes == 30
    finally:
        import asyncio

        asyncio.run(composed.aclose())


def test_composer_wires_telegram_listener_dispatcher_with_buy_orchestrator(
    env: EnvSettings,
    fake_dirs: tuple[Path, Path],
    stub_adapters: dict[str, Any],
) -> None:
    """``ComposedDaemon`` exposes the Telegram surface and the callback
    dispatcher with a fully-wired BuyOrchestrator. Phase 2 Comprar taps
    drive the orchestrator instead of falling back to the
    ``buy_orchestrator_not_wired`` defence-in-depth path.
    """
    from salvager.orchestration.buy_orchestrator import BuyOrchestrator

    config_dir, data_dir = fake_dirs
    (data_dir / WALLAPOP_COOKIES_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (data_dir / WALLAPOP_COOKIES_RELPATH).write_text("{}", encoding="utf-8")

    composed = compose_daemon(
        env,
        config_path=config_dir / "config.yaml",
        wishlist_path=config_dir / "wishlist.yaml",
    )
    try:
        assert composed.telegram is not None
        assert composed.dispatcher is not None
        # The dispatcher must hold the same surface — otherwise an ack
        # keyboard edit lands on a different bot than the operator sees.
        assert composed.dispatcher._surface is composed.telegram

        # BuyOrchestrator is wired with all nine collaborators populated.
        buy = composed.dispatcher._buy_orchestrator
        assert isinstance(buy, BuyOrchestrator)
        for field_name in (
            "preflight",
            "reconciler",
            "browser",
            "circuit_breaker",
            "audit_writer",
            "telegram_surface",
            "store",
            "reporter",
            "wishlist_loader",
        ):
            assert getattr(buy, field_name) is not None, (
                f"BuyOrchestrator.{field_name} was not populated by the composer"
            )
        # Telegram + store are the same instances reused from the Phase 1 build.
        assert buy.telegram_surface is composed.telegram
        assert buy.store is composed.store
    finally:
        import asyncio

        asyncio.run(composed.aclose())


# ─────────────────────────────────────────────────────────────────────────
# WishlistLoader closure — must see operator edits between alert and tap
# ─────────────────────────────────────────────────────────────────────────


def test_wishlist_loader_returns_current_entry_after_file_edit(tmp_path: Path) -> None:
    """A wishlist edit between alert and tap must be observed by the
    orchestrator's preflight via the loader. The closure re-reads on
    mtime change instead of snapshotting at startup."""
    import os
    import time

    from salvager.orchestration.composer import _make_wishlist_loader

    wishlist_path = tmp_path / "wishlist.yaml"
    initial = """
entries:
  - manufacturer: Western Digital
    model: WD Red Plus 4TB
    ref: WD40EFPX
    type: hdd
    max_price_solo: 60.00
    max_price_in_device: null
    keywords: ["wd red plus 4tb"]
    container_keywords: []
    confidence_threshold: high
    phase2:
      enabled: false
      max_price_eur: null
"""
    wishlist_path.write_text(initial, encoding="utf-8")
    loader = _make_wishlist_loader(wishlist_path)

    entry_key = ("Western Digital", "WD Red Plus 4TB", "WD40EFPX")
    first = loader(entry_key)
    assert first is not None
    assert first.max_price_solo == Decimal("60.00")

    # Bump mtime explicitly so a rewrite within the same second is still
    # visible to the cache invalidation logic.
    updated = initial.replace("60.00", "55.00")
    wishlist_path.write_text(updated, encoding="utf-8")
    os.utime(wishlist_path, (time.time() + 1, time.time() + 1))

    second = loader(entry_key)
    assert second is not None
    assert second.max_price_solo == Decimal("55.00"), (
        "loader should re-read the file after mtime bumps"
    )


def test_wishlist_loader_returns_none_for_unknown_entry_key(tmp_path: Path) -> None:
    """An EntryKey that doesn't match any current entry resolves to None
    (operator removed the entry between alert and tap)."""
    from salvager.orchestration.composer import _make_wishlist_loader

    wishlist_path = tmp_path / "wishlist.yaml"
    wishlist_path.write_text(
        """
entries:
  - manufacturer: Western Digital
    model: WD Red Plus 4TB
    ref: WD40EFPX
    type: hdd
    max_price_solo: 60.00
    max_price_in_device: null
    keywords: ["wd red plus 4tb"]
    container_keywords: []
    confidence_threshold: high
    phase2:
      enabled: false
      max_price_eur: null
""",
        encoding="utf-8",
    )
    loader = _make_wishlist_loader(wishlist_path)

    removed_key = ("Western Digital", "WD Red Plus 8TB", "WD80EFPX")
    assert loader(removed_key) is None
