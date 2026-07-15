"""Crash/restart audit consistency — Story 4.9 (NFR-R5).

Mechanically asserts that the audit log and the seen-listings dedup
table stay consistent across an abrupt daemon death. The flow:

    cycle 1: 20 high-confidence listings → kill -9 after 8 alerts land
    restart: a fresh SqliteStore opens the same on-disk database
    cycle 2: 20 listings = 5 overlap with cycle 1 + 15 fresh low-conf

The invariants under test (Story 4.9 AC):

    - the 5 overlap listings produce ZERO new alerts — dedup state
      written by cycle 1 survived the crash;
    - the audit log holds exactly 8 alert rows: none lost to the
      partial first run, none duplicated by the restart;
    - every alert_snapshots row re-hydrates cleanly — SQLite's WAL +
      per-write autocommit means a crash leaves whole rows or nothing,
      never a torn row;
    - the database is in WAL journal mode (regression guard).

Opt-in: marked ``slow``, runs under ``pytest --runslow``.

The kill -9 is simulated by a Telegram surface that raises a
``BaseException`` on the 9th send. ``run_poll_cycle`` only catches
``Exception``, so the ``BaseException`` tears the cycle down exactly
the way an abrupt process death would — after 8 sends have each been
followed by their ``record_alert_snapshot`` + ``record_seen`` writes.

Telegram-before-audit race (documented per AC)
----------------------------------------------
``_dispatch_alert`` sends to Telegram first, then writes the audit row.
If a crash lands in that window — after the send returns but before the
audit INSERT commits — the listing is left un-seen, so on restart it is
re-evaluated and a SECOND Telegram alert goes out. That is the accepted
trade-off: over-alert rather than under-audit. This test deliberately
crashes on the *send* (not between send and audit), so the 8 completed
alerts are each fully persisted; the race window itself is a known,
accepted gap and is not exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
from salvager.interfaces.telegram_surface import CallbackHandler, TelegramSurface
from salvager.orchestration.poll_loop import run_poll_cycle

pytestmark = pytest.mark.slow

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_ENTRY_KEY = ("Western Digital", "WD Red Plus 4TB", "WD40EFPX")


def _entry() -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Western Digital",
            "model": "WD Red Plus 4TB",
            "ref": "WD40EFPX",
            "type": "hdd",
            "keywords": ["wd red plus 4tb"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
        }
    )


def _listing(listing_id: str) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=f"https://es.wallapop.com/item/{listing_id}",
        title="WD Red Plus 4TB NAS disk",
        description="ok",
        price_eur=Decimal("55.00"),
        location="Madrid",
        photo_urls=["https://cdn/p.jpg"],
        fetched_at=_T0,
    )


def _evaluation(listing_id: str, *, confidence: str) -> ListingEvaluation:
    return ListingEvaluation(
        listing_id=listing_id,
        entry_key=_ENTRY_KEY,
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take="Match." if confidence == "high" else "Title hint only.",
        is_container=False,
        evaluated_at=_T0,
    )


class _FixtureFetcher(PageFetcher):
    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def search(self, query: SearchQuery) -> list[Listing]:
        return list(self._listings)

    async def fetch(self, listing_url: str) -> Listing:  # pragma: no cover
        raise AssertionError("e2e: fetch() not exercised")


class _ScriptedEvaluator(ListingEvaluator):
    def __init__(self, by_listing: dict[str, ListingEvaluation]) -> None:
        self._by = by_listing

    async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
        if listing.listing_id not in self._by:
            raise AssertionError(f"e2e: no scripted eval for {listing.listing_id}")
        return self._by[listing.listing_id]


class _SimulatedCrash(BaseException):
    """A kill -9: a ``BaseException`` so ``run_poll_cycle``'s ``except
    Exception`` blocks don't swallow it — the cycle tears down abruptly."""


class _CrashingTelegram(TelegramSurface):
    """Sends normally until ``crash_after`` succeeds, then raises kill -9."""

    def __init__(self, *, crash_after: int) -> None:
        self._crash_after = crash_after
        self.sends = 0
        self._next_message_id = 1000

    async def send(self, rendered: RenderedAlert, *, reply_to_message_id: int | None = None) -> int:
        if self.sends >= self._crash_after:
            raise _SimulatedCrash("kill -9 mid-cycle")
        self.sends += 1
        self._next_message_id += 1
        return self._next_message_id

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
    ) -> None:  # pragma: no cover
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:  # pragma: no cover
        _ = handler


class _RecordingTelegram(TelegramSurface):
    def __init__(self) -> None:
        self.sends: list[RenderedAlert] = []
        self._next_message_id = 2000

    async def send(self, rendered: RenderedAlert, *, reply_to_message_id: int | None = None) -> int:
        self.sends.append(rendered)
        self._next_message_id += 1
        return self._next_message_id

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
    ) -> None:  # pragma: no cover
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:  # pragma: no cover
        _ = handler


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    MigrationRunner().run(connection)
    connection.close()
    return db_path


async def test_audit_log_consistent_across_crash_and_restart(migrated_db: Path) -> None:
    wishlist = Wishlist(entries=[_entry()])

    # ── Cycle 1: 20 high-confidence listings, crash after 8 alerts ───
    batch_one = [_listing(f"c1-{n:02d}") for n in range(20)]
    evals_one = {
        lst.listing_id: _evaluation(lst.listing_id, confidence="high") for lst in batch_one
    }
    crashing_telegram = _CrashingTelegram(crash_after=8)
    store_before = SqliteStore(migrated_db)

    with pytest.raises(_SimulatedCrash):
        await run_poll_cycle(
            "wallapop",
            wishlist=wishlist,
            fetcher=_FixtureFetcher(batch_one),
            evaluator=_ScriptedEvaluator(evals_one),
            store=store_before,
            telegram=crashing_telegram,
        )
    assert crashing_telegram.sends == 8
    # Abrupt death — the daemon never gets to close the store. Committed
    # rows are already on disk (WAL + autocommit); closing here is only
    # test hygiene and changes nothing about the persisted state.
    await store_before.close()

    # ── Restart: a fresh store opens the same on-disk database ───────
    store_after = SqliteStore(migrated_db)
    try:
        # Cycle 2: 5 overlap (already alerted + persisted in cycle 1) +
        # 15 fresh low-confidence listings (recorded as seen, no alerts).
        overlap = [_listing(f"c1-{n:02d}") for n in range(5)]
        fresh_low = [_listing(f"c2-{n:02d}") for n in range(15)]
        batch_two = overlap + fresh_low
        evals_two = {
            lst.listing_id: _evaluation(lst.listing_id, confidence="low") for lst in fresh_low
        }
        recording_telegram = _RecordingTelegram()

        summary = await run_poll_cycle(
            "wallapop",
            wishlist=wishlist,
            fetcher=_FixtureFetcher(batch_two),
            evaluator=_ScriptedEvaluator(evals_two),
            store=store_after,
            telegram=recording_telegram,
        )

        # Dedup survived the crash: the 5 overlap listings were skipped
        # before evaluation, so they produced zero new alerts.
        assert summary.alerts_sent == 0
        assert recording_telegram.sends == []
        assert summary.new_count == 15  # only the fresh listings were unseen
        assert summary.dropped_count == 15
    finally:
        await store_after.close()

    # ── Consistency assertions against the on-disk database ──────────
    connection = open_connection(migrated_db)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        alert_rows = connection.execute(
            "SELECT audit_id, alert_id, listing_json, evaluation_json FROM alert_snapshots"
        ).fetchall()
        distinct_alert_ids = connection.execute(
            "SELECT COUNT(DISTINCT alert_id) FROM alert_snapshots"
        ).fetchone()[0]
        seen_count = connection.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
    finally:
        connection.close()

    # WAL mode is on — regression guard against an accidental disable.
    assert journal_mode == "wal"

    # Exactly the 8 alerts from the partial first run: none lost to the
    # crash, none duplicated by the restart.
    assert len(alert_rows) == 8
    assert distinct_alert_ids == 8

    # Every audit row is whole — a torn row would fail JSON re-hydration.
    for row in alert_rows:
        Listing.model_validate_json(row["listing_json"])
        ListingEvaluation.model_validate_json(row["evaluation_json"])

    # seen_listings: 8 from cycle 1 (alerted) + 15 from cycle 2 (dropped).
    assert seen_count == 23
