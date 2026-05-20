"""Tests for :class:`WallapopTinyfishFetcher` — Story 3.5.

The TinyFish SDK is mocked at the constructor seam: a fake async
client records every ``agent.run`` call and returns a preloaded
:class:`AgentRunResponse` (or raises a preloaded exception). No
network calls, no real API key. Tests verify the schema-drift mapping,
error translation, rate-limit semantics, and goal/url construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tinyfish import (
    AgentRunResponse,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    RunStatus,
)

from salvager.adapters.wallapop_tinyfish import WallapopTinyfishFetcher
from salvager.adapters.wallapop_tinyfish.fetcher import SEARCH_GOAL_TEMPLATE
from salvager.adapters.wallapop_tinyfish.rate_limit import (
    SlidingWindowRateLimiter,
)
from salvager.domain.errors import (
    TinyFishAuthFailed,
    TinyFishRateLimited,
    TinyFishUnavailable,
    WallapopSchemaDrift,
)
from salvager.domain.listing import SearchQuery


def _fake_response(status_code: int, json_body: dict[str, Any] | None = None) -> httpx.Response:
    """Build the bare ``httpx.Response`` the TinyFish exception
    constructors expect — they only really read its status code +
    body, so a minimal handcrafted one suffices for tests."""
    import json as _json

    content = _json.dumps(json_body or {}).encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("POST", "https://agent.tinyfish.ai/v1/automation/run"),
    )


# A clearly fake key — the test doesn't go near a real TinyFish server.
_FAKE_KEY = SecretStr("sk-tinyfish-fake-deadbeefcafebabe0123456789abcdef")


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────


class _FakeAgent:
    """Records ``run`` invocations and returns / raises a preload."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_response: AgentRunResponse | None = None
        self.next_exception: BaseException | None = None

    async def run(self, *, goal: str, url: str, **kwargs: Any) -> AgentRunResponse:
        self.calls.append({"goal": goal, "url": url, **kwargs})
        if self.next_exception is not None:
            raise self.next_exception
        assert self.next_response is not None, "test forgot to preload a response"
        return self.next_response


class _FakeClient:
    """Stand-in for :class:`AsyncTinyFish`."""

    def __init__(self) -> None:
        self.agent = _FakeAgent()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _make_response(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    result: dict[str, Any] | None = None,
    run_id: str = "run-abc",
    error: Any = None,
) -> AgentRunResponse:
    return AgentRunResponse(
        status=status,
        run_id=run_id,
        result=result,
        error=error,
        num_of_steps=3,
        started_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 13, 12, 0, 30, tzinfo=UTC),
    )


def _query() -> SearchQuery:
    return SearchQuery(
        keyword="wd red plus 4tb",
        marketplace="wallapop",
        max_price_eur=Decimal("70"),
    )


def _good_payload(listing_id: str = "abc123") -> dict[str, Any]:
    return {
        "listings": [
            {
                "listing_id": listing_id,
                "url": f"https://es.wallapop.com/item/{listing_id}",
                "title": "WD Red Plus 4TB",
                "price_eur": "55.00",
                "location": "Madrid",
                "description": "Used, in box.",
                "photo_urls": ["https://cdn/photo.jpg"],
            }
        ]
    }


# ─────────────────────────────────────────────────────────────────────────
# Fake-client construction always uses the fake (no real SDK init)
# ─────────────────────────────────────────────────────────────────────────


class _RaisingRealClient:
    """Sentinel — if the fetcher ever falls back to a real AsyncTinyFish
    in a test it instantiates this and immediately raises, surfacing
    the leak as a hard test failure."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("production AsyncTinyFish was constructed during a unit test")


@pytest.fixture(autouse=True)
def _block_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "salvager.adapters.wallapop_tinyfish.fetcher.AsyncTinyFish",
        _RaisingRealClient,
    )


def _make_fetcher(
    *,
    rate_limit_per_minute: int = 5,
    rate_limiter: SlidingWindowRateLimiter | None = None,
) -> tuple[WallapopTinyfishFetcher, _FakeClient]:
    client = _FakeClient()
    fetcher = WallapopTinyfishFetcher(
        _FAKE_KEY,
        client=client,  # type: ignore[arg-type]
        rate_limit_per_minute=rate_limit_per_minute,
        rate_limiter=rate_limiter,
    )
    return fetcher, client


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


async def test_search_returns_parsed_listings() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(result=_good_payload())

    listings = await fetcher.search(_query())

    assert len(listings) == 1
    listing = listings[0]
    assert listing.listing_id == "abc123"
    assert listing.marketplace == "wallapop"
    assert listing.price_eur == Decimal("55.00")
    assert listing.location == "Madrid"
    assert listing.photo_urls == ["https://cdn/photo.jpg"]


async def test_search_builds_wallapop_search_url() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(result=_good_payload())

    await fetcher.search(_query())

    call = client.agent.calls[0]
    assert call["url"].startswith("https://es.wallapop.com/app/search?")
    assert "keywords=wd+red+plus+4tb" in call["url"]
    assert "max_sale_price=70" in call["url"]


async def test_search_passes_goal_template_with_results_limit() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(result=_good_payload())

    await fetcher.search(_query())

    goal = client.agent.calls[0]["goal"]
    assert goal == SEARCH_GOAL_TEMPLATE.format(limit=30)
    assert "Wallapop search results" in goal
    # No naked {limit} placeholder leaks through.
    assert "{limit}" not in goal


async def test_search_empty_listings_array_returns_empty_list() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(result={"listings": []})

    listings = await fetcher.search(_query())
    assert listings == []


# ─────────────────────────────────────────────────────────────────────────
# Error translation
# ─────────────────────────────────────────────────────────────────────────


async def test_auth_error_raises_tinyfish_auth_failed() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_exception = AuthenticationError("invalid key", response=_fake_response(401))

    with pytest.raises(TinyFishAuthFailed):
        await fetcher.search(_query())


async def test_permission_denied_also_raises_auth_failed() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_exception = PermissionDeniedError(
        "out of credits", response=_fake_response(403)
    )

    with pytest.raises(TinyFishAuthFailed):
        await fetcher.search(_query())


async def test_remote_rate_limit_translates_to_local_exception() -> None:
    fetcher, client = _make_fetcher()
    # Inject the retry-after via the exception's body attribute (the
    # adapter reads body["retry_after"]); the SDK attaches body itself
    # from the response, but for the unit test we patch it directly.
    exc = RateLimitError("rate-limited", response=_fake_response(429))
    exc.body = {"retry_after": 12}  # type: ignore[attr-defined]
    client.agent.next_exception = exc

    with pytest.raises(TinyFishRateLimited) as exc_info:
        await fetcher.search(_query())
    assert exc_info.value.retry_after_s == 12


async def test_run_finished_with_failed_status_raises_unavailable() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(status=RunStatus.FAILED, result=None)

    with pytest.raises(TinyFishUnavailable):
        await fetcher.search(_query())


async def test_malformed_result_raises_schema_drift_with_field_path() -> None:
    fetcher, client = _make_fetcher()
    bad = {
        "listings": [
            {"listing_id": "x"}  # missing url + title + price_eur
        ]
    }
    client.agent.next_response = _make_response(result=bad)

    with pytest.raises(WallapopSchemaDrift) as exc_info:
        await fetcher.search(_query())
    # Field path identifies which selector / prompt clause to patch.
    assert "listings" in exc_info.value.field_path


async def test_empty_result_envelope_raises_schema_drift() -> None:
    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(result=None, status=RunStatus.COMPLETED)

    with pytest.raises(WallapopSchemaDrift):
        await fetcher.search(_query())


# ─────────────────────────────────────────────────────────────────────────
# Client-side rate limiting
# ─────────────────────────────────────────────────────────────────────────


async def test_local_rate_limit_blocks_sixth_call_in_a_minute() -> None:
    fetcher, client = _make_fetcher(rate_limit_per_minute=5)
    client.agent.next_response = _make_response(result={"listings": []})

    for _ in range(5):
        await fetcher.search(_query())

    # The sixth call inside the same minute MUST fail without hitting the agent.
    calls_before = len(client.agent.calls)
    with pytest.raises(TinyFishRateLimited):
        await fetcher.search(_query())
    assert len(client.agent.calls) == calls_before, (
        "rate limit must short-circuit before the TinyFish call"
    )


async def test_local_rate_limit_window_slides_forward() -> None:
    """A controllable clock proves the window slides — after one full
    minute elapses the budget refills."""

    class _Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.now

    clock = _Clock()
    limiter = SlidingWindowRateLimiter(limit=2, window=timedelta(minutes=1), clock=clock)
    fetcher, client = _make_fetcher(rate_limiter=limiter)
    client.agent.next_response = _make_response(result={"listings": []})

    await fetcher.search(_query())
    await fetcher.search(_query())
    # Third would hit the cap at t=0…
    with pytest.raises(TinyFishRateLimited):
        await fetcher.search(_query())

    # …but after 61 seconds the window slides, both prior events age out,
    # the limiter refills.
    clock.now = clock.now + timedelta(seconds=61)
    await fetcher.search(_query())
    assert len(client.agent.calls) == 3


# ─────────────────────────────────────────────────────────────────────────
# Side-effects on failure
# ─────────────────────────────────────────────────────────────────────────


async def test_failed_call_still_records_against_rate_budget() -> None:
    """A 500/auth/timeout still counts toward the remote's rate budget,
    so we record locally too — no retry storm after a transient failure."""
    fetcher, client = _make_fetcher(rate_limit_per_minute=2)
    client.agent.next_exception = AuthenticationError("bad key", response=_fake_response(401))

    for _ in range(2):
        with pytest.raises(TinyFishAuthFailed):
            await fetcher.search(_query())

    # Budget used up by failures → third call short-circuits.
    with pytest.raises(TinyFishRateLimited):
        await fetcher.search(_query())


# ─────────────────────────────────────────────────────────────────────────
# Structured logging — wallapop_tinyfish_search_succeeded carries latency
# ─────────────────────────────────────────────────────────────────────────


async def test_success_emits_wallapop_tinyfish_search_succeeded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    fetcher, client = _make_fetcher()
    client.agent.next_response = _make_response(result=_good_payload())

    await fetcher.search(_query())
    out = capsys.readouterr().out

    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    success = [r for r in records if r["event"] == "wallapop_tinyfish_search_succeeded"]
    assert success, f"missing success log in {records!r}"
    assert success[0]["result_count"] == 1
    assert success[0]["marketplace"] == "wallapop"
    assert "latency_ms" in success[0]


# ─────────────────────────────────────────────────────────────────────────
# fetch() raises NotImplementedError at v0.x
# ─────────────────────────────────────────────────────────────────────────


async def test_fetch_raises_not_implemented_at_v0() -> None:
    fetcher, _ = _make_fetcher()
    with pytest.raises(NotImplementedError, match="Phase 2"):
        await fetcher.fetch("https://es.wallapop.com/item/abc123")


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline: tinyfish is allowed only inside this adapter
# ─────────────────────────────────────────────────────────────────────────


def test_only_tinyfish_adapters_import_tinyfish() -> None:
    """The adapter-discipline lint script enforces this at the package
    level; this test re-asserts it at the unit-test layer too so the
    invariant breaks here even before CI runs the lint.

    Two adapter packages legitimately depend on the TinyFish SDK:

      - ``adapters/wallapop_tinyfish/`` — Wallapop search fallback (3.5)
      - ``adapters/tinyfish_browser/`` — Phase 2 buy flows (5.3)

    Every other source file must be tinyfish-free.
    """
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "salvager"
    allowed_pkgs = (
        src_root / "adapters" / "wallapop_tinyfish",
        src_root / "adapters" / "tinyfish_browser",
    )
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if any(pkg in path.parents for pkg in allowed_pkgs):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tinyfish" or alias.name.startswith("tinyfish."):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "tinyfish" or module.startswith("tinyfish."):
                    offenders.append(f"{path}: from {module} import ...")
    assert not offenders, "tinyfish imports leaked outside the adapter:\n  " + "\n  ".join(
        offenders
    )
