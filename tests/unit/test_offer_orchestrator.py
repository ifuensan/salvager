"""Offer orchestrator + preflight tests (wallapop-offer-flow).

Real ``OfferAuditWriter`` over a migrated temp DB (lockout/dedupe/budget
are DB-backed contracts); fakes for the store, fetcher, offer session,
Telegram surface, and reporter. Covers the happy path, every abort path,
lockout engagement + independence rules, the daily budget, and the
keyboard-restore guarantee on every outcome.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.adapters.sqlite_store.migrations import MigrationRunner, db_path_under
from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter
from salvager.domain.alert import AlertSnapshot, CallbackEvent, EventName, RenderedAlert
from salvager.domain.errors import OfferFailureReason, WallapopApiError
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.offer_audit import OfferAttemptRecord
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.offer_session import (
    OfferResult,
    OfferSendFailure,
    OfferSession,
    OfferSuccess,
)
from salvager.orchestration.offer_orchestrator import (
    OfferOrchestrator,
    OfferOutcomeAborted,
    OfferOutcomeFailure,
    OfferOutcomeSuccess,
)
from salvager.orchestration.offer_preflight import OfferPreflight

_T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_ENTRY_KEY = ("Corsair", "Vengeance LPX 16GB", "CMK16GX4M2D3000C16")


def _entry(**offer_overrides: Any) -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": _ENTRY_KEY[0],
            "model": _ENTRY_KEY[1],
            "ref": _ENTRY_KEY[2],
            "type": "ram",
            "keywords": ["corsair"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
            "offer": {"enabled": True, **offer_overrides},
        }
    )


def _listing(**overrides: Any) -> Listing:
    base: dict[str, Any] = {
        "listing_id": "internal-123",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/corsair-abc",
        "title": "Corsair Vengeance LPX 16GB",
        "description": "d",
        "price_eur": Decimal("70.00"),
        "shipping_eur": Decimal("3.50"),
        "fetched_at": _T0,
    }
    base.update(overrides)
    return Listing(**base)


def _snapshot(listing: Listing | None = None) -> AlertSnapshot:
    lst = listing if listing is not None else _listing()
    return AlertSnapshot(
        alert_id=uuid4(),
        entry_key=_ENTRY_KEY,
        entry_display_name="Corsair Vengeance LPX 16GB (CMK16GX4M2D3000C16)",
        listing=lst,
        evaluation=ListingEvaluation(
            listing_id=lst.listing_id,
            entry_key=_ENTRY_KEY,
            confidence="high",
            one_line_take="Match.",
            is_container=False,
            evaluated_at=_T0,
        ),
        phase="negotiable",
        rendered_at=_T0,
    )


def _event(snapshot: AlertSnapshot) -> CallbackEvent:
    return CallbackEvent(
        callback_query_id="cq1",
        chat_id=1,
        message_id=99,
        callback_data=f"listing:offer:{snapshot.alert_id}",
        verb="offer",
    )


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, snapshot: AlertSnapshot | None) -> None:
        self._snapshot = snapshot

    async def get_alert_snapshot_by_alert_id(self, alert_id: Any) -> AlertSnapshot | None:
        if self._snapshot is not None and self._snapshot.alert_id == alert_id:
            return self._snapshot
        return None


class _FakeFetcher:
    def __init__(self, fresh: Listing | None = None, error: Exception | None = None) -> None:
        self._fresh = fresh
        self._error = error
        self.calls = 0

    async def fetch_listing(self, listing: Listing) -> Listing:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._fresh is not None
        return self._fresh


class _FakeOfferSession(OfferSession):
    def __init__(self, result: OfferResult | None = None) -> None:
        self._result = result
        self.calls: list[tuple[str, Decimal]] = []

    async def execute_offer(self, listing: Listing, amount_eur: Decimal) -> OfferResult:
        self.calls.append((listing.listing_id, amount_eur))
        assert self._result is not None, "test drove execution unexpectedly"
        return self._result


class _FakeTelegram:
    def __init__(self) -> None:
        self.sends: list[RenderedAlert] = []
        self.keyboard_edits: list[tuple[int, Any]] = []

    async def send(self, rendered: RenderedAlert, **_: Any) -> int:
        self.sends.append(rendered)
        return 1

    async def edit_keyboard(self, message_id: int, keyboard: Any) -> None:
        self.keyboard_edits.append((message_id, keyboard))


class _FakeReporter:
    def __init__(self) -> None:
        self.events: list[EventName] = []

    async def report(self, severity: str, event: EventName, *, ctx: Any = None) -> None:
        self.events.append(event)


@pytest.fixture
async def offer_writer(tmp_path: Path) -> AsyncIterator[OfferAuditWriter]:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    MigrationRunner().run(connection)
    connection.close()
    writer = OfferAuditWriter(db_path)
    yield writer
    await writer.close()


def _orchestrator(
    *,
    offer_writer: OfferAuditWriter,
    snapshot: AlertSnapshot | None,
    fetcher: _FakeFetcher,
    session: _FakeOfferSession,
    entry: WishlistEntry | None = None,
    kill_switch: bool = False,
    daily_limit: int = 5,
) -> tuple[OfferOrchestrator, _FakeTelegram, _FakeReporter]:
    telegram = _FakeTelegram()
    reporter = _FakeReporter()
    resolved_entry = entry if entry is not None else _entry()
    preflight = OfferPreflight(
        offer_writer=offer_writer,
        kill_switch_global=kill_switch,
        lockout_threshold=3,
        daily_limit=daily_limit,
    )
    orchestrator = OfferOrchestrator(
        preflight=preflight,
        fetcher=fetcher,  # type: ignore[arg-type]
        offer_session=session,
        offer_writer=offer_writer,
        telegram_surface=telegram,  # type: ignore[arg-type]
        store=_FakeStore(snapshot),  # type: ignore[arg-type]
        reporter=reporter,  # type: ignore[arg-type]
        wishlist_loader=lambda key: resolved_entry if key == _ENTRY_KEY else None,
        lockout_threshold=3,
    )
    return orchestrator, telegram, reporter


def _success_result(**overrides: Any) -> OfferSuccess:
    base: dict[str, Any] = {
        "offered_eur": Decimal("61"),
        "screenshot_url": "https://shots/offer.png",
        "platform_remaining": 9,
        "total_seconds": 12,
    }
    base.update(overrides)
    return OfferSuccess(**base)


# ─────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────


async def test_happy_path_sends_recomputed_amount_and_audits(
    offer_writer: OfferAuditWriter,
) -> None:
    # Asking 70 € against a 70 € ceiling: fit price is 61 €.
    snapshot = _snapshot()
    fetcher = _FakeFetcher(fresh=_listing())
    session = _FakeOfferSession(_success_result())
    orchestrator, telegram, _ = _orchestrator(
        offer_writer=offer_writer, snapshot=snapshot, fetcher=fetcher, session=session
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeSuccess)
    assert outcome.offered_eur == Decimal("61")
    assert session.calls == [("internal-123", Decimal("61"))]
    assert await offer_writer.has_successful_offer("wallapop", "internal-123") is True
    assert (await offer_writer.read_state()).consecutive_failures == 0
    # Sent alert + terminal badge keyboard.
    assert any("Oferta enviada" in r.text for r in telegram.sends)
    assert telegram.keyboard_edits, "keyboard must be repainted"
    last_keyboard = telegram.keyboard_edits[-1][1]
    assert any("Oferta enviada" in b.text for row in last_keyboard for b in row)


async def test_success_on_phase2_alert_keeps_comprar_row(offer_writer: OfferAuditWriter) -> None:
    listing = _listing(price_eur=Decimal("55.00"))
    snapshot = _snapshot(listing).model_copy(
        update={"phase": "phase2", "phase2_max_price_eur": Decimal("70.00")}
    )
    entry = _entry(target_total_eur=Decimal("60"))
    fetcher = _FakeFetcher(fresh=listing)
    session = _FakeOfferSession(_success_result(offered_eur=Decimal("51")))
    orchestrator, telegram, _ = _orchestrator(
        offer_writer=offer_writer, snapshot=snapshot, fetcher=fetcher, session=session, entry=entry
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeSuccess)
    last_keyboard = telegram.keyboard_edits[-1][1]
    labels = [b.text for row in last_keyboard for b in row]
    assert "✅ Comprar" in labels
    assert any("Oferta enviada" in label for label in labels)


# ─────────────────────────────────────────────────────────────────────────
# Abort paths (no lockout increment, keyboard restored)
# ─────────────────────────────────────────────────────────────────────────


async def test_listing_gone_aborts_fail_closed(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    fetcher = _FakeFetcher(error=WallapopApiError(404, "not found"))
    session = _FakeOfferSession()
    orchestrator, telegram, _ = _orchestrator(
        offer_writer=offer_writer, snapshot=snapshot, fetcher=fetcher, session=session
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.listing_gone
    assert session.calls == []
    assert (await offer_writer.read_state()).consecutive_failures == 0
    # Failure alert carries the reassurance; keyboard restored to Ofertar.
    assert any("No se ha enviado ninguna oferta" in r.text for r in telegram.sends)
    last_keyboard = telegram.keyboard_edits[-1][1]
    assert any("Ofertar" in b.text for row in last_keyboard for b in row)


async def test_price_rise_beyond_tolerance_aborts(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()  # displayed fit: 61 €
    fetcher = _FakeFetcher(fresh=_listing(price_eur=Decimal("95.00")))  # fit now impossible
    session = _FakeOfferSession()
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer, snapshot=snapshot, fetcher=fetcher, session=session
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.reconciliation_tripped
    assert session.calls == []
    assert (await offer_writer.read_state()).consecutive_failures == 0


async def test_reserved_on_refetch_aborts(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    fetcher = _FakeFetcher(fresh=_listing(is_reserved=True))
    session = _FakeOfferSession()
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer, snapshot=snapshot, fetcher=fetcher, session=session
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.reconciliation_tripped
    assert session.calls == []


async def test_duplicate_offer_blocked_before_execution(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    await offer_writer.record_offer_attempt(
        OfferAttemptRecord(
            alert_id=uuid4(),
            listing_id="internal-123",
            marketplace="wallapop",
            entry_key=_ENTRY_KEY,
            offered_eur=Decimal("61"),
            asking_eur=Decimal("70.00"),
            outcome="success",
            attempted_at=_T0,
        )
    )
    fetcher = _FakeFetcher(fresh=_listing())
    session = _FakeOfferSession()
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer, snapshot=snapshot, fetcher=fetcher, session=session
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.duplicate_offer
    assert fetcher.calls == 0
    assert session.calls == []


async def test_daily_budget_blocks_before_execution(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    now = datetime.now(UTC)
    for n in range(2):
        await offer_writer.record_offer_attempt(
            OfferAttemptRecord(
                alert_id=uuid4(),
                listing_id=f"other-{n}",
                marketplace="wallapop",
                entry_key=_ENTRY_KEY,
                offered_eur=Decimal("10"),
                asking_eur=Decimal("20"),
                outcome="success",
                attempted_at=now - timedelta(hours=1),
            )
        )
    fetcher = _FakeFetcher(fresh=_listing())
    session = _FakeOfferSession()
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer,
        snapshot=snapshot,
        fetcher=fetcher,
        session=session,
        daily_limit=2,
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.daily_limit_reached
    assert session.calls == []
    assert (await offer_writer.read_state()).consecutive_failures == 0


async def test_kill_switch_blocks(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer,
        snapshot=snapshot,
        fetcher=_FakeFetcher(fresh=_listing()),
        session=_FakeOfferSession(),
        kill_switch=True,
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.lockout_engaged


async def test_snapshot_missing_restores_keyboard(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    orchestrator, telegram, _ = _orchestrator(
        offer_writer=offer_writer,
        snapshot=None,  # store knows nothing about this alert
        fetcher=_FakeFetcher(fresh=_listing()),
        session=_FakeOfferSession(),
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.reason == "snapshot_not_found"
    assert telegram.keyboard_edits, "the in-flight badge must not stay painted"


# ─────────────────────────────────────────────────────────────────────────
# Failures + lockout
# ─────────────────────────────────────────────────────────────────────────


async def test_execution_failure_counts_and_third_engages_lockout(
    offer_writer: OfferAuditWriter,
) -> None:
    snapshot = _snapshot()
    session = _FakeOfferSession(
        OfferSendFailure(reason=OfferFailureReason.timeout, ctx={"detail": "x"})
    )
    orchestrator, telegram, reporter = _orchestrator(
        offer_writer=offer_writer,
        snapshot=snapshot,
        fetcher=_FakeFetcher(fresh=_listing()),
        session=session,
    )

    for _attempt in (1, 2, 3):
        outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))
        assert isinstance(outcome, OfferOutcomeFailure)

    state = await offer_writer.read_state()
    assert state.globally_disabled is True
    assert state.consecutive_failures == 3
    assert EventName.offer_lockout_engaged in reporter.events

    # The next tap aborts at preflight — no execution.
    session.calls.clear()
    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))
    assert isinstance(outcome, OfferOutcomeAborted)
    assert outcome.rendered_as is OfferFailureReason.lockout_engaged
    assert session.calls == []
    # Keyboard restored on every failure outcome along the way.
    assert len(telegram.keyboard_edits) == 4


async def test_platform_daily_limit_failure_never_counts(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    session = _FakeOfferSession(
        OfferSendFailure(
            reason=OfferFailureReason.daily_limit_reached, ctx={"platform_remaining": 0}
        )
    )
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer,
        snapshot=snapshot,
        fetcher=_FakeFetcher(fresh=_listing()),
        session=session,
    )

    outcome = await orchestrator.execute_offer_from_callback(_event(snapshot))

    assert isinstance(outcome, OfferOutcomeFailure)
    assert outcome.reason is OfferFailureReason.daily_limit_reached
    assert (await offer_writer.read_state()).consecutive_failures == 0


async def test_failure_rows_are_audited(offer_writer: OfferAuditWriter) -> None:
    snapshot = _snapshot()
    session = _FakeOfferSession(OfferSendFailure(reason=OfferFailureReason.amount_rejected, ctx={}))
    orchestrator, _, _ = _orchestrator(
        offer_writer=offer_writer,
        snapshot=snapshot,
        fetcher=_FakeFetcher(fresh=_listing()),
        session=session,
    )

    await orchestrator.execute_offer_from_callback(_event(snapshot))

    connection = open_connection(offer_writer._db_path)
    try:
        row = connection.execute(
            "SELECT outcome, failure_reason FROM offers ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("failure", "amount_rejected")
