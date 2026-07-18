"""Integration tests for :class:`BuyOrchestrator` — Story 5.7.

The full Phase 2 buy pipeline wired with:

  - **Real** :class:`Phase2AuditWriter` against a migrated tmp-path
    SQLite (audit rows actually land in tables).
  - **Real** :class:`Reconciler`, :class:`CircuitBreaker`,
    :class:`Phase2Preflight` — the modules under the NFR-M2 90%
    coverage gate.
  - **Fakes** for the ports the orchestrator depends on:
    :class:`BrowserSession`, :class:`TelegramSurface`,
    :class:`Store`, :class:`Reporter`, :class:`Phase2StateReader`,
    :class:`PageFetcher` (cross-source).

Eight AC scenarios, each one assertion-rich enough to pin the
expected outcome + audit-log rows + Telegram dispatch + circuit-counter
change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from salvager.adapters.sqlite_store import (
    MigrationRunner,
    Phase2AuditWriter,
    open_connection,
)
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.domain.alert import (
    AlertSnapshot,
    CallbackEvent,
    EventName,
    RenderedAlert,
)
from salvager.domain.errors import BuyFailureReason
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.phase2_audit import Phase2StateSnapshot
from salvager.domain.pricing import buyer_total_eur
from salvager.domain.wishlist import (
    Phase2Settings,
    WishlistEntry,
)
from salvager.interfaces.browser_session import BuyFailure, BuySuccess
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.orchestration.buy_orchestrator import (
    RECEIPT_MISMATCH_REASON,
    BuyOrchestrator,
    BuyOutcomeAborted,
    BuyOutcomeFailure,
    BuyOutcomeSuccess,
)
from salvager.orchestration.circuit_breaker import CircuitBreaker
from salvager.orchestration.phase2_preflight import Phase2Preflight
from salvager.orchestration.reconciler import Reconciler

# ─────────────────────────────────────────────────────────────────────────
# Fixtures + data builders
# ─────────────────────────────────────────────────────────────────────────


_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
_ENTRY_KEY = ("Western Digital", "WD Red Plus 4TB", "WD40EFPX")
# Ceiling sits above the delivered buyer total of the 55 € default listing
# (item + shipping buffer + Wallapop Protección ≈ 63 €) so the buy gate passes
# — that headroom is exactly what shipping-aware-pricing is about.
_MAX_PRICE = Decimal("70.00")
_TOL_EUR = Decimal("1.00")
_TOL_PCT = Decimal("2.0")
_ASSUMED_SHIPPING = Decimal("3.50")


def _listing(**overrides: Any) -> Listing:
    base: dict[str, Any] = {
        "listing_id": "abc123",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/abc123",
        "title": "WD Red Plus 4TB",
        "description": "Como nuevo.",
        "price_eur": Decimal("55.00"),
        "location": "Madrid",
        "photo_urls": ["https://cdn/photo.jpg"],
        "fetched_at": _FIXED_TS,
    }
    base.update(overrides)
    return Listing(**base)


# The delivered total the buyer pays for the default listing (item + shipping
# buffer + Wallapop Protección) — what the marketplace charges and therefore
# what a clean receipt reconciles against (shipping-aware-pricing).
_DELIVERED_TOTAL = buyer_total_eur(_listing(), assumed_shipping_eur=_ASSUMED_SHIPPING)


def _evaluation(**overrides: Any) -> ListingEvaluation:
    base: dict[str, Any] = {
        "listing_id": "abc123",
        "entry_key": _ENTRY_KEY,
        "confidence": "high",
        "one_line_take": "Strong match.",
        "is_container": False,
        "evaluated_at": _FIXED_TS,
    }
    base.update(overrides)
    return ListingEvaluation(**base)


def _alert_snapshot(**overrides: Any) -> AlertSnapshot:
    base: dict[str, Any] = {
        "alert_id": _FIXED_ALERT_ID,
        "entry_key": _ENTRY_KEY,
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": _listing(),
        "evaluation": _evaluation(),
        "phase": "phase2",
        "phase2_max_price_eur": _MAX_PRICE,
        "rendered_at": _FIXED_TS,
    }
    base.update(overrides)
    return AlertSnapshot(**base)


def _entry(*, phase2_enabled: bool = True, max_price: Decimal | None = _MAX_PRICE) -> WishlistEntry:
    return WishlistEntry(
        manufacturer=_ENTRY_KEY[0],
        model=_ENTRY_KEY[1],
        ref=_ENTRY_KEY[2],
        type="hdd",
        max_price_solo=Decimal("70.00"),
        keywords=["wd red 4tb"],
        confidence_threshold="high",
        phase2=Phase2Settings(enabled=phase2_enabled, max_price_eur=max_price),
    )


def _callback_event(alert_id: UUID = _FIXED_ALERT_ID) -> CallbackEvent:
    return CallbackEvent(
        callback_query_id="cb-1",
        chat_id=99,
        message_id=42,
        callback_data=f"listing:buy:{alert_id}",
        verb="buy",
    )


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class FakeStore:
    snapshot: AlertSnapshot | None

    async def get_alert_snapshot_by_alert_id(self, alert_id: UUID) -> AlertSnapshot | None:
        return self.snapshot if self.snapshot and self.snapshot.alert_id == alert_id else None


@dataclass
class FakeTelegram(TelegramSurface):
    sent: list[RenderedAlert] = field(default_factory=list)
    keyboard_edits: list[tuple[int, Any]] = field(default_factory=list)

    async def send(self, rendered: RenderedAlert, *, reply_to_message_id: int | None = None) -> int:
        self.sent.append(rendered)
        return len(self.sent)

    async def edit_alert(self, message_id: int, rendered: Any, *, has_photo: bool) -> None:
        pass

    async def edit_keyboard(self, message_id: int, keyboard: Any) -> None:
        self.keyboard_edits.append((message_id, keyboard))

    async def listen_callbacks(self, handler: Any) -> None:
        pass


@dataclass
class FakeReporter:
    reports: list[tuple[str, EventName, dict[str, Any]]] = field(default_factory=list)

    async def report(self, severity: str, event: EventName, ctx: Any) -> None:
        self.reports.append((severity, event, dict(ctx)))


@dataclass
class FakeStateReader:
    snapshot: Phase2StateSnapshot

    async def read(self) -> Phase2StateSnapshot:
        return self.snapshot


@dataclass
class FakeBrowser:
    next_result: BuySuccess | BuyFailure | None = None
    next_exception: BaseException | None = None
    calls: list[tuple[str, Decimal]] = field(default_factory=list)

    async def execute_buy(self, listing: Listing, max_price_eur: Decimal) -> Any:
        self.calls.append((listing.url, max_price_eur))
        if self.next_exception is not None:
            raise self.next_exception
        assert self.next_result is not None
        return self.next_result


@dataclass
class FakeCrossSourceFetcher:
    """A trivial :class:`PageFetcher`-shaped fake — only ``fetch`` is used."""

    next_listing: Listing | None = None
    next_exception: BaseException | None = None

    async def search(self, *_a: Any, **_kw: Any) -> list[Listing]:
        raise NotImplementedError

    async def fetch_listing(self, listing: Listing) -> Listing:
        return await self.fetch(listing.url)

    async def fetch(self, listing_url: str) -> Listing:
        if self.next_exception is not None:
            raise self.next_exception
        assert self.next_listing is not None
        return self.next_listing


# ─────────────────────────────────────────────────────────────────────────
# Wiring helper
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Wired:
    orchestrator: BuyOrchestrator
    audit_writer: Phase2AuditWriter
    store: FakeStore
    telegram: FakeTelegram
    reporter: FakeReporter
    browser: FakeBrowser
    cross_source: FakeCrossSourceFetcher
    state_reader: FakeStateReader
    db_path: Path


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    return db_path


def _wire(
    migrated_db: Path,
    *,
    snapshot: AlertSnapshot | None = None,
    state: Phase2StateSnapshot | None = None,
    entry: WishlistEntry | None = None,
) -> _Wired:
    snap = snapshot if snapshot is not None else _alert_snapshot()
    state_snapshot = state or Phase2StateSnapshot(
        globally_disabled=False,
        consecutive_failures=0,
        last_smoke_result="pass",
        last_smoke_at=_FIXED_TS - timedelta(hours=1),
    )
    state_reader = FakeStateReader(snapshot=state_snapshot)
    audit_writer = Phase2AuditWriter(migrated_db)
    store = FakeStore(snapshot=snap)
    telegram = FakeTelegram()
    reporter = FakeReporter()
    browser = FakeBrowser()
    cross_source = FakeCrossSourceFetcher()

    preflight = Phase2Preflight(
        state_reader=state_reader,
        circuit_breaker_threshold=3,
        clock=lambda: _FIXED_TS,
    )
    reconciler = Reconciler(
        cross_source_fetcher=cross_source,  # type: ignore[arg-type]
        tolerance_eur=_TOL_EUR,
        tolerance_pct=_TOL_PCT,
        assumed_shipping_eur=_ASSUMED_SHIPPING,
    )
    circuit = CircuitBreaker(
        audit_writer=audit_writer,
        state_reader=state_reader,
        reporter=reporter,
        threshold=3,
    )
    resolved_entry = entry if entry is not None else _entry()
    orchestrator = BuyOrchestrator(
        preflight=preflight,
        reconciler=reconciler,
        browser=browser,  # type: ignore[arg-type]
        circuit_breaker=circuit,
        audit_writer=audit_writer,
        telegram_surface=telegram,
        store=store,  # type: ignore[arg-type]
        reporter=reporter,
        wishlist_loader=lambda _k: resolved_entry,
        clock=lambda: _FIXED_TS,
    )
    return _Wired(
        orchestrator=orchestrator,
        audit_writer=audit_writer,
        store=store,
        telegram=telegram,
        reporter=reporter,
        browser=browser,
        cross_source=cross_source,
        state_reader=state_reader,
        db_path=migrated_db,
    )


def _row(db_path: Path, sql: str) -> dict[str, object] | None:
    connection = open_connection(db_path)
    try:
        cursor = connection.execute(sql)
        row = cursor.fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _rows(db_path: Path, sql: str) -> list[dict[str, object]]:
    connection = open_connection(db_path)
    try:
        return [dict(r) for r in connection.execute(sql).fetchall()]
    finally:
        connection.close()


# ─────────────────────────────────────────────────────────────────────────
# Scenario 1 — Happy path
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_1_happy_path(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuySuccess(
        price_paid_eur=_DELIVERED_TOTAL,
        payment_method="wallapop_pay",
        receipt_id="WP-2026-0001",
        screenshot_url="/app/data/screenshots/WP-2026-0001.png",
        total_seconds=42,
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeSuccess)
    assert outcome.transaction.receipt_id == "WP-2026-0001"
    assert outcome.audit_id == 1
    # One tap + one transaction landed in the audit log.
    assert _row(migrated_db, "SELECT * FROM tap_events WHERE audit_id = 1") is not None
    assert _row(migrated_db, "SELECT * FROM transactions WHERE audit_id = 1") is not None
    # One success message dispatched.
    assert len(wired.telegram.sent) == 1
    assert wired.telegram.sent[0].text.startswith("✅ ")
    # The cross-source path was consulted.
    assert wired.browser.calls == [(str(_listing().url), _MAX_PRICE)]


# ─────────────────────────────────────────────────────────────────────────
# Scenario 2 — Cross-source reconciliation tripped
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_2_reconciliation_tripped(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    # Wallapop API says 55€, cross-source HTML says 0.53€ — Q9 silent
    # failure caught by the reconciler.
    wired.cross_source.next_listing = _listing(price_eur=Decimal("0.53"))
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.reconciliation_tripped
    # The browser was NEVER consulted — the buy aborted before checkout.
    assert wired.browser.calls == []
    # The Telegram dispatch is the failure variant carrying the price rows.
    assert len(wired.telegram.sent) == 1
    assert wired.telegram.sent[0].text.startswith("🚫 ")
    # The tap row landed (the operator did tap).
    assert _row(migrated_db, "SELECT * FROM tap_events WHERE audit_id = 1") is not None
    # No transaction row.
    assert _rows(migrated_db, "SELECT * FROM transactions") == []


# ─────────────────────────────────────────────────────────────────────────
# Scenario 3 — UI check failed (browser returned BuyFailure(ui_check_failed))
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_3_ui_check_failed(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuyFailure(
        reason=BuyFailureReason.ui_check_failed,
        ctx={"missing": ["seller_block"]},
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.ui_check_failed
    assert outcome.ctx["missing"] == ["seller_block"]
    # No transaction row.
    assert _rows(migrated_db, "SELECT * FROM transactions") == []
    # The dispatch is the failure variant.
    assert len(wired.telegram.sent) == 1


# ─────────────────────────────────────────────────────────────────────────
# Scenario 4 — Marketplace error from cross-source fetch
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_4_marketplace_error_during_cross_source(
    migrated_db: Path,
) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_exception = RuntimeError("upstream 503")
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.marketplace_error
    assert outcome.ctx["error_class"] == "RuntimeError"
    assert wired.browser.calls == []  # the buy never started


# ─────────────────────────────────────────────────────────────────────────
# Scenario 5 — Timeout from the browser
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_5_timeout(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuyFailure(
        reason=BuyFailureReason.timeout,
        ctx={"budget_s": 120, "detail": "confirmation page did not load"},
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.timeout
    # Circuit incremented.
    counter_row = _row(migrated_db, "SELECT consecutive_failures FROM phase2_state WHERE id = 1")
    assert counter_row is not None
    assert int(str(counter_row["consecutive_failures"])) == 1


# ─────────────────────────────────────────────────────────────────────────
# Scenario 6 — Screenshot missing
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_6_screenshot_missing(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuyFailure(
        reason=BuyFailureReason.screenshot_missing,
        ctx={"receipt_id": "WP-2026-0001"},
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.screenshot_missing
    # The screenshot-missing alternate reassurance + the receipt
    # appear in the dispatched text.
    assert len(wired.telegram.sent) == 1
    text = wired.telegram.sent[0].text
    assert "WP-2026-0001" in text


# ─────────────────────────────────────────────────────────────────────────
# Scenario 7 — Receipt-vs-alert reconciliation mismatch (post-buy)
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_7_receipt_mismatch_disables_phase2_globally(
    migrated_db: Path,
) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    # Alert/listing price 55€; receipt 100€ — way outside tolerance.
    wired.browser.next_result = BuySuccess(
        price_paid_eur=Decimal("100.00"),
        payment_method="wallapop_pay",
        receipt_id="WP-2026-0002",
        screenshot_url="/app/data/screenshots/WP-2026-0002.png",
        total_seconds=40,
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    # The buy itself succeeded (we paid and got a receipt); the
    # outcome reflects that.
    assert isinstance(outcome, BuyOutcomeSuccess)
    # Phase 2 is now globally disabled, with the receipt-mismatch reason.
    state = _row(
        migrated_db,
        "SELECT globally_disabled, disabled_reason FROM phase2_state WHERE id = 1",
    )
    assert state is not None
    assert int(str(state["globally_disabled"])) == 1
    assert state["disabled_reason"] == RECEIPT_MISMATCH_REASON
    # A `phase2_disabled` operational warn alert was reported (separately
    # from the success Telegram dispatch).
    disabled_reports = [r for r in wired.reporter.reports if r[1] is EventName.phase2_disabled]
    assert len(disabled_reports) == 1
    assert disabled_reports[0][2]["reason"] == RECEIPT_MISMATCH_REASON


# ─────────────────────────────────────────────────────────────────────────
# Scenario 8 — Circuit already open at pre-flight (no marketplace touch)
# ─────────────────────────────────────────────────────────────────────────


async def test_scenario_8_preflight_blocks_when_globally_disabled(
    migrated_db: Path,
) -> None:
    wired = _wire(
        migrated_db,
        state=Phase2StateSnapshot(
            globally_disabled=True,
            disabled_reason="circuit_breaker_open",
            consecutive_failures=3,
            last_smoke_result="pass",
            last_smoke_at=_FIXED_TS - timedelta(hours=1),
        ),
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    assert outcome.reason == "globally_disabled"
    assert outcome.rendered_as is BuyFailureReason.circuit_open
    # The marketplace was never touched.
    assert wired.browser.calls == []
    assert wired.cross_source.next_listing is None  # never set; never read
    # The tap row was NOT recorded because preflight refused.
    assert _rows(migrated_db, "SELECT * FROM tap_events") == []
    # A failure-shaped Telegram dispatch was sent so the operator gets
    # an answer to their tap.
    assert len(wired.telegram.sent) == 1
    assert wired.telegram.sent[0].text.startswith("🚫 ")


# ─────────────────────────────────────────────────────────────────────────
# Extra coverage — boundary failure modes the AC implies
# ─────────────────────────────────────────────────────────────────────────


async def test_snapshot_not_found_returns_aborted_without_dispatch(
    migrated_db: Path,
) -> None:
    wired = _wire(migrated_db)
    wired.store.snapshot = None  # no snapshot for any alert_id
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    assert outcome.reason == "snapshot_not_found"
    # No dispatch — we have no entry context to render anything for.
    assert wired.telegram.sent == []


async def test_unparseable_callback_data_returns_aborted(
    migrated_db: Path,
) -> None:
    wired = _wire(migrated_db)
    bad = CallbackEvent(
        callback_query_id="cb-1",
        chat_id=99,
        message_id=42,
        callback_data="listing:buy:not-a-uuid",
        verb="buy",
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(bad)
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    assert outcome.reason == "callback_data_unparseable"


async def test_unexpected_exception_emits_buy_orchestrator_error_alert(
    migrated_db: Path,
) -> None:
    """The catch-all: if a downstream step raises an unexpected
    exception, the orchestrator emits ``buy_orchestrator_error`` and
    returns a marketplace-error outcome — and the circuit increments."""
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_exception = RuntimeError("unexpected boom")
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.marketplace_error
    assert outcome.ctx["error_class"] == "RuntimeError"
    orchestrator_alerts = [
        r for r in wired.reporter.reports if r[1] is EventName.buy_orchestrator_error
    ]
    assert len(orchestrator_alerts) == 1
    # Counter incremented on the failure.
    counter_row = _row(migrated_db, "SELECT consecutive_failures FROM phase2_state WHERE id = 1")
    assert counter_row is not None
    assert int(str(counter_row["consecutive_failures"])) == 1


async def test_entry_no_longer_in_wishlist_aborts_cleanly(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    wired.orchestrator.wishlist_loader = lambda _k: None
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    assert outcome.reason == "entry_not_in_wishlist"
    # Browser never touched.
    assert wired.browser.calls == []


async def test_snapshot_without_phase2_max_price_falls_back_to_entry_ceiling(
    migrated_db: Path,
) -> None:
    """A pre-Story-5.2 snapshot may have ``phase2_max_price_eur=None``.
    The orchestrator falls back to ``entry.phase2.max_price_eur`` so
    the buy still respects FR26."""
    snapshot = _alert_snapshot(phase2_max_price_eur=None)
    wired = _wire(migrated_db, snapshot=snapshot)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuySuccess(
        price_paid_eur=_DELIVERED_TOTAL,
        payment_method="wallapop_pay",
        receipt_id="WP-2026-0099",
        screenshot_url="/app/data/screenshots/WP-2026-0099.png",
        total_seconds=30,
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeSuccess)
    # The entry's phase2.max_price_eur (70€) was used as the ceiling.
    assert wired.browser.calls == [(str(_listing().url), Decimal("70.00"))]


async def test_callback_data_with_wrong_part_count_returns_aborted(
    migrated_db: Path,
) -> None:
    """A two-segment payload (missing the UUID) parses to ``None``
    and aborts cleanly — same path as the unparseable-uuid case but
    via a different branch in :func:`_parse_alert_id`."""
    wired = _wire(migrated_db)
    bad = CallbackEvent(
        callback_query_id="cb-1",
        chat_id=99,
        message_id=42,
        callback_data="listing:buy",  # no UUID segment
        verb="buy",
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(bad)
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    assert outcome.reason == "callback_data_unparseable"


async def test_telegram_dispatch_failure_is_swallowed_outcome_still_returned(
    migrated_db: Path,
) -> None:
    """Telegram is best-effort. If ``send`` raises after a happy buy,
    the outcome still surfaces — the operator can replay the receipt
    via the audit log."""
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuySuccess(
        price_paid_eur=_DELIVERED_TOTAL,
        payment_method="wallapop_pay",
        receipt_id="WP-2026-0050",
        screenshot_url="/app/data/screenshots/WP-2026-0050.png",
        total_seconds=20,
    )

    async def _boom(_rendered: Any) -> int:
        raise RuntimeError("telegram offline")

    wired.telegram.send = _boom  # type: ignore[method-assign,assignment]
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeSuccess)
    # And the audit row still landed.
    assert _row(migrated_db, "SELECT * FROM transactions WHERE audit_id = 1") is not None


async def test_catch_all_swallows_secondary_reporter_and_circuit_failures(
    migrated_db: Path,
) -> None:
    """When the catch-all fires, a *further* failure in the operational
    alert or the circuit-record step must not mask the original error
    nor crash the orchestrator. The outcome still returns marketplace_error."""
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_exception = RuntimeError("primary failure")

    async def _report_boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("reporter offline")

    async def _record_boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("circuit write failed")

    wired.reporter.report = _report_boom  # type: ignore[method-assign]
    wired.orchestrator.circuit_breaker.record_outcome = _record_boom  # type: ignore[method-assign]

    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.marketplace_error


_ = uuid4  # silence the unused-import hint (kept for future scenarios)


# ─────────────────────────────────────────────────────────────────────────
# Scenario — listing gone (sold/withdrawn) between alert and tap
# ─────────────────────────────────────────────────────────────────────────


async def test_listing_gone_404_aborts_without_circuit_increment(
    migrated_db: Path,
) -> None:
    """A 404 on the pre-buy re-fetch means the listing sold or was withdrawn
    between the alert and the tap — a normal marketplace outcome, not a
    system failure: the operator gets a plain 'ya no está disponible'
    message and the circuit breaker is NOT incremented (first real tap,
    2026-07-16, almost opened the breaker on two overnight sales)."""
    from salvager.domain.errors import WallapopApiError

    wired = _wire(migrated_db)
    wired.cross_source.next_exception = WallapopApiError(404, "item not found")
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    assert outcome.reason == "listing_gone"
    assert outcome.rendered_as is BuyFailureReason.listing_gone
    assert wired.browser.calls == []  # no checkout ever started

    # The operator-facing message names the real cause, in plain Spanish.
    failure_msgs = [m for m in wired.telegram.sent if "no está disponible" in m.text]
    assert len(failure_msgs) == 1
    assert "La compra NO se ha ejecutado" in failure_msgs[0].text

    # Circuit breaker NOT incremented — this is an abort, not a failure.
    counter_row = _row(migrated_db, "SELECT consecutive_failures FROM phase2_state WHERE id = 1")
    assert counter_row is not None
    assert int(str(counter_row["consecutive_failures"])) == 0


async def test_non_404_cross_source_error_still_counts_as_failure(
    migrated_db: Path,
) -> None:
    """The listing-gone carve-out is exactly status 404 — any other
    marketplace error keeps the existing failure semantics (circuit
    increments, marketplace_error variant)."""
    from salvager.domain.errors import WallapopApiError

    wired = _wire(migrated_db)
    wired.cross_source.next_exception = WallapopApiError(503, "upstream sad")
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    assert outcome.reason is BuyFailureReason.marketplace_error
    counter_row = _row(migrated_db, "SELECT consecutive_failures FROM phase2_state WHERE id = 1")
    assert counter_row is not None
    assert int(str(counter_row["consecutive_failures"])) == 1


# ─────────────────────────────────────────────────────────────────────────
# Keyboard restoration after every outcome (found live 2026-07-18)
# ─────────────────────────────────────────────────────────────────────────


async def test_failed_buy_restores_the_comprar_keyboard(migrated_db: Path) -> None:
    """The callback handler paints 🟡 Comprando… before handing off; a
    failure must repaint the Comprar row or the operator can never retry
    from that message (two zombie keyboards in one day, 2026-07-18)."""
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_exception = RuntimeError("tinyfish auth failed")
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeFailure)
    [(message_id, keyboard)] = wired.telegram.keyboard_edits
    assert message_id == 42  # the tapped message
    labels = [b.text for b in keyboard[0]]
    assert any("Comprar" in text for text in labels)


async def test_listing_gone_abort_also_restores_keyboard(migrated_db: Path) -> None:
    from salvager.domain.errors import WallapopApiError

    wired = _wire(migrated_db)
    wired.cross_source.next_exception = WallapopApiError(404, "gone")
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeAborted)
    [(_, keyboard)] = wired.telegram.keyboard_edits
    assert any("Comprar" in b.text for b in keyboard[0])


async def test_successful_buy_paints_terminal_comprado_badge(migrated_db: Path) -> None:
    wired = _wire(migrated_db)
    wired.cross_source.next_listing = _listing(price_eur=Decimal("55.00"))
    wired.browser.next_result = BuySuccess(
        price_paid_eur=_DELIVERED_TOTAL,
        payment_method="wallapop_pay",
        receipt_id="WP-2026-0042",
        screenshot_url="/app/data/screenshots/ok.png",
        total_seconds=41,
    )
    try:
        outcome = await wired.orchestrator.execute_buy_from_callback(_callback_event())
    finally:
        await wired.audit_writer.close()

    assert isinstance(outcome, BuyOutcomeSuccess)
    [(_, keyboard)] = wired.telegram.keyboard_edits
    assert keyboard[0][0].text == "✅ Comprado"
    assert keyboard[0][0].callback_data.startswith("listing:noop:")
