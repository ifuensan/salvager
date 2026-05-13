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
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from hardware_hunter.config.env import EnvSettings, reset_env_cache
from hardware_hunter.orchestration import composer as composer_module
from hardware_hunter.orchestration.composer import (
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
