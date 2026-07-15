"""End-to-end poll-cycle test — Story 3.14 AR15/AR16.

Drives the full pipeline (snooze filter → dedup → eval fan-out →
threshold → render → send → record) against a representative listing
stream:

    10 listings = 3 strong matches + 7 dropped-below-threshold + 1
    container variant (extra row in the matches set)

Uses the real ``SqliteStore`` (temp file) + the real Phase 1 renderer
+ fake PageFetcher / ListingEvaluator / TelegramSurface. The
assertion matrix mirrors the Story 3.14 AC:

    - exactly 3 alerts dispatched
    - seen_listings contains all 10 rows
    - alert_snapshots contains exactly 3 rows
    - the container-shaped alert renders the Direction-E split
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from salvager.adapters.sqlite_store import (
    MigrationRunner,
    SqliteStore,
    open_connection,
)
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.domain.alert import InlineButton, RenderedAlert
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, SearchQuery
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.telegram_surface import (
    CallbackHandler,
    TelegramSurface,
)
from salvager.orchestration.poll_loop import run_poll_cycle

_T0 = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _entry() -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": "WD Red Plus 4TB",
            "ref": "WD40EFPX",
            "type": "hdd",
            "keywords": ["wd red plus 4tb"],
            "container_keywords": ["synology", "qnap"],
            "max_price_solo": Decimal("70.00"),
            "max_price_in_device": Decimal("200.00"),
            "confidence_threshold": "medium",
        }
    )


def _listing(listing_id: str, *, title: str = "WD Red 4TB") -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=f"https://es.wallapop.com/item/{listing_id}",
        title=title,
        description="ok",
        price_eur=Decimal("55.00"),
        location="Madrid",
        photo_urls=["https://cdn/photo.jpg"],
        fetched_at=_T0,
    )


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    MigrationRunner().run(connection)
    connection.close()
    return db_path


# ─────────────────────────────────────────────────────────────────────────
# Pipeline fakes
# ─────────────────────────────────────────────────────────────────────────


class _FixtureFetcher(PageFetcher):
    """Returns a fixed list of listings on every search."""

    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def search(self, query: SearchQuery) -> list[Listing]:
        return list(self._listings)

    async def fetch(self, listing_url: str) -> Listing:  # pragma: no cover — e2e doesn't call this
        raise AssertionError("e2e: fetch() not exercised")


class _ScriptedEvaluator(ListingEvaluator):
    """Returns the preloaded ListingEvaluation for each listing_id."""

    def __init__(self, by_listing: dict[str, ListingEvaluation]) -> None:
        self._by = by_listing
        self.calls: list[str] = []

    async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
        self.calls.append(listing.listing_id)
        if listing.listing_id not in self._by:
            raise AssertionError(f"e2e: no scripted evaluation for {listing.listing_id}")
        return self._by[listing.listing_id]


class _RecordingTelegram(TelegramSurface):
    def __init__(self) -> None:
        self.sends: list[RenderedAlert] = []
        self._next_message_id = 1000

    async def send(self, rendered: RenderedAlert) -> int:
        self.sends.append(rendered)
        message_id = self._next_message_id
        self._next_message_id += 1
        return message_id

    async def edit_alert(
        self,
        message_id: int,
        rendered: RenderedAlert,
        *,
        has_photo: bool,
    ) -> None:
        return None

    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:  # pragma: no cover — e2e doesn't edit
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:  # pragma: no cover
        _ = handler


# ─────────────────────────────────────────────────────────────────────────
# The fixture stream: 3 matches + 7 dropped + 1 container variant
# ─────────────────────────────────────────────────────────────────────────


def _evaluation(
    listing_id: str,
    *,
    confidence: str = "high",
    is_container: bool = False,
    take: str = "Strong match.",
    wrapper_text: str | None = None,
    extracted_text: str | None = None,
) -> ListingEvaluation:
    return ListingEvaluation(
        listing_id=listing_id,
        entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take=take,
        is_container=is_container,
        wrapper_text=wrapper_text,
        extracted_text=extracted_text,
        evaluated_at=_T0,
    )


def _make_fixture_stream() -> tuple[list[Listing], dict[str, ListingEvaluation]]:
    # 3 strong matches (high confidence).
    matches = [_listing(f"match{n}") for n in range(3)]
    matches_eval = {m.listing_id: _evaluation(m.listing_id, confidence="high") for m in matches}

    # 7 dropped-below-threshold (low confidence — threshold is medium).
    dropped = [_listing(f"drop{n}") for n in range(7)]
    dropped_eval = {
        d.listing_id: _evaluation(d.listing_id, confidence="low", take="Title hint only.")
        for d in dropped
    }

    # 1 container variant (high confidence, container shape — counts as a match
    # and renders the indented wrapper rows).
    container = _listing(
        "container1",
        title="Synology DS220+ NAS — incluye 2x WD Red Plus 4TB",
    )
    container_eval = _evaluation(
        "container1",
        confidence="high",
        is_container=True,
        take="NAS includes the wishlisted drives.",
        wrapper_text="Synology DS220+ NAS",
        extracted_text="WD Red Plus 4TB drives",
    )

    listings = matches + dropped + [container]
    evals: dict[str, ListingEvaluation] = {
        **matches_eval,
        **dropped_eval,
        container.listing_id: container_eval,
    }
    return listings, evals


# ─────────────────────────────────────────────────────────────────────────
# The test
# ─────────────────────────────────────────────────────────────────────────


_FIXED_ALERT_ID_COUNTER: list[int] = [0]


def _fixed_alert_id() -> UUID:
    """Predictable UUIDs so the e2e test can correlate without relying on
    actual UUID4 randomness."""
    _FIXED_ALERT_ID_COUNTER[0] += 1
    return UUID(f"00000000-0000-4000-8000-{_FIXED_ALERT_ID_COUNTER[0]:012d}")


async def test_full_cycle_dispatches_expected_alerts_and_records_audit(
    migrated_db: Path,
) -> None:
    listings, evals = _make_fixture_stream()
    fetcher = _FixtureFetcher(listings)
    evaluator = _ScriptedEvaluator(evals)
    store = SqliteStore(migrated_db)
    telegram = _RecordingTelegram()

    _FIXED_ALERT_ID_COUNTER[0] = 0  # reset for deterministic IDs

    try:
        summary = await run_poll_cycle(
            "wallapop",
            wishlist=Wishlist(entries=[_entry()]),
            fetcher=fetcher,
            evaluator=evaluator,
            store=store,
            telegram=telegram,
            new_alert_id=_fixed_alert_id,
        )

        # ── Cycle summary ────────────────────────────────────────────
        assert summary.result_count == 11  # 3 + 7 + 1
        assert summary.new_count == 11
        assert summary.alerts_sent == 4  # 3 direct matches + 1 container
        assert summary.dropped_count == 7
        assert summary.errors == 0

        # ── Telegram surface ─────────────────────────────────────────
        assert len(telegram.sends) == 4

        # The container alert carries the Direction-E indented rows.
        container_alerts = [
            r for r in telegram.sends if "Wrapper:" in r.text and "Extracted:" in r.text
        ]
        assert len(container_alerts) == 1
        # Direct-match alerts have NO wrapper rows.
        direct_alerts = [r for r in telegram.sends if "Wrapper:" not in r.text]
        assert len(direct_alerts) == 3

        # ── Database state ───────────────────────────────────────────
        connection = open_connection(migrated_db)
        try:
            seen_count = connection.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
            alert_count = connection.execute("SELECT COUNT(*) FROM alert_snapshots").fetchone()[0]
        finally:
            connection.close()

        assert seen_count == 11, "every listing must be recorded as seen exactly once"
        assert alert_count == 4
    finally:
        await store.close()
