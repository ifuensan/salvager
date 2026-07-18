"""Phase 2 buy orchestrator — Story 5.7 (FR24-FR30 / NFR-M2).

The single end-to-end flow the operator's Comprar tap drives:

    snapshot lookup
       → re-run pre-flight (state may have changed since the alert)
       → cross-source reconciliation (FR31 — refuse if prices diverge)
       → execute_buy via the BrowserSession adapter (FR25)
       → write the audit rows (tap + transaction)
       → receipt-vs-alert reconciliation (FR32 — auto-disable on drift)
       → circuit-breaker outcome
       → Telegram dispatch (success / failure / aborted)

The orchestrator is a composer — it depends only on ports (BrowserSession,
TelegramSurface, Phase2StateReader, Reporter, Store) plus the typed
collaborators already in `orchestration/` (Reconciler, CircuitBreaker,
Phase2Preflight, Phase2AuditWriter). Adapter discipline (NFR-M1) holds
because no marketplace SDK / TinyFish / Hermes import lands here; every
external touch is mediated.

The return value is the typed :class:`BuyOutcome` discriminated union so
the caller (Story 5.10's callback handler) can branch on ``.kind``
without re-classifying exceptions.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from salvager.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from salvager.domain.alert import (
    CallbackEvent,
    EventName,
    InlineButton,
    _phase2_button_row,
    render_phase2_buy_failure,
    render_phase2_buy_success,
)
from salvager.domain.errors import BuyFailureReason
from salvager.domain.phase2_audit import TapEventRecord, TransactionRecord
from salvager.interfaces.browser_session import (
    BrowserSession,
    BuyFailure,
    BuySuccess,
)
from salvager.interfaces.store import Store
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.observability.logging import get_logger
from salvager.orchestration.circuit_breaker import CircuitBreaker
from salvager.orchestration.degradation_reporter import Reporter
from salvager.orchestration.phase2_preflight import Phase2Preflight
from salvager.orchestration.reconciler import Reconciler

if TYPE_CHECKING:
    from salvager.domain.alert import AlertSnapshot
    from salvager.domain.wishlist import WishlistEntry

EntryKey = tuple[str, str, str]
WishlistLoader = Callable[[EntryKey], "WishlistEntry | None"]

#: The reason string persisted in ``phase2_state.disabled_reason`` when
#: receipt-vs-alert reconciliation trips after a successful buy.
#: Operators see it via ``salvager health`` / ``phase2 status``.
RECEIPT_MISMATCH_REASON: Final[str] = "receipt_mismatch"


# Map a Phase 2 preflight failure reason to the closest
# :class:`BuyFailureReason`. The renderer needs a closed variant; the
# orchestrator keeps the raw preflight reason in the outcome's ctx so
# downstream audits can drill in.
_PREFLIGHT_REASON_TO_FAILURE: Final[dict[str, BuyFailureReason]] = {
    "phase2_disabled_for_entry": BuyFailureReason.ui_check_failed,
    "phase2_max_price_unset": BuyFailureReason.ui_check_failed,
    "phase2_max_price_below_listing": BuyFailureReason.ui_check_failed,
    "confidence_below_threshold": BuyFailureReason.ui_check_failed,
    "globally_disabled": BuyFailureReason.circuit_open,
    "circuit_breaker_open": BuyFailureReason.circuit_open,
    "smoke_test_never_run": BuyFailureReason.circuit_open,
    "smoke_test_failed": BuyFailureReason.circuit_open,
    "smoke_test_stale": BuyFailureReason.circuit_open,
}


# ─────────────────────────────────────────────────────────────────────────
# BuyOutcome — discriminated union returned to the callback handler
# ─────────────────────────────────────────────────────────────────────────


class BuyOutcomeSuccess(BaseModel):
    """The autonomous purchase committed and the receipt was captured."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    kind: Literal["success"] = "success"
    transaction: TransactionRecord
    audit_id: int


class BuyOutcomeFailure(BaseModel):
    """The buy attempt happened and failed — operator owes a triage step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["failure"] = "failure"
    reason: BuyFailureReason
    ctx: dict[str, Any] = Field(default_factory=dict)


class BuyOutcomeAborted(BaseModel):
    """Pre-flight (or snapshot lookup) refused — the marketplace was
    never touched. ``reason`` is the raw preflight identifier so the
    audit log preserves it; ``rendered_as`` is the
    :class:`BuyFailureReason` the user-facing alert used."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["aborted"] = "aborted"
    reason: str
    rendered_as: BuyFailureReason | None = None
    ctx: dict[str, Any] = Field(default_factory=dict)


BuyOutcome = Annotated[
    BuyOutcomeSuccess | BuyOutcomeFailure | BuyOutcomeAborted,
    Field(discriminator="kind"),
]


# ─────────────────────────────────────────────────────────────────────────
# BuyOrchestrator
# ─────────────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BuyOrchestrator:
    """Composes the seven dependencies into one ``execute_buy_from_callback``.

    The constructor parameters mirror Story 5.7's named collaborators.
    ``wishlist_loader`` resolves an :class:`EntryKey` to its currently
    declared :class:`WishlistEntry` (or ``None`` if the operator
    removed the entry between alert and tap) — preflight needs the
    entry to re-evaluate per-entry settings (``phase2.enabled``,
    ``max_price_eur``, ``confidence_threshold``).
    """

    preflight: Phase2Preflight
    reconciler: Reconciler
    browser: BrowserSession
    circuit_breaker: CircuitBreaker
    audit_writer: Phase2AuditWriter
    telegram_surface: TelegramSurface
    store: Store
    reporter: Reporter
    wishlist_loader: WishlistLoader
    clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        self._log = get_logger("orchestration.buy_orchestrator")

    async def execute_buy_from_callback(self, callback_event: CallbackEvent) -> BuyOutcome:
        """Drive the full buy pipeline for one verified Buy tap.

        Single ``try/except`` wraps every step downstream of the
        snapshot lookup — an unexpected exception emits
        ``EventName.buy_orchestrator_error``, increments the circuit
        and returns ``BuyOutcomeFailure(marketplace_error)``. The audit
        log is left consistent (we record what we reached, never half a
        transaction).
        """
        alert_id = _parse_alert_id(callback_event.callback_data)
        if alert_id is None:
            self._log.warning(
                "buy_orchestrator_callback_data_unparseable",
                extra={"callback_data": callback_event.callback_data},
            )
            return BuyOutcomeAborted(reason="callback_data_unparseable")

        snapshot = await self.store.get_alert_snapshot_by_alert_id(alert_id)
        if snapshot is None:
            self._log.warning(
                "buy_orchestrator_snapshot_missing", extra={"alert_id": str(alert_id)}
            )
            return BuyOutcomeAborted(reason="snapshot_not_found")

        try:
            outcome: BuyOutcome = await self._run(snapshot, callback_event)
        except Exception as exc:
            outcome = await self._handle_unexpected(snapshot, callback_event, exc)
        # The callback handler painted 🟡 Comprando… (a noop badge) before
        # handing off — without a repaint here, EVERY non-success outcome
        # left the alert permanently un-tappable (found live 2026-07-18:
        # the operator could not retry after a failed buy).
        await self._restore_keyboard(callback_event, alert_id, outcome)
        return outcome

    async def _restore_keyboard(
        self,
        callback_event: CallbackEvent,
        alert_id: UUID,
        outcome: BuyOutcome,
    ) -> None:
        """Repaint the tapped message's keyboard to match the outcome.

        Success → a terminal ``✅ Comprado`` badge (noop). Failure/abort →
        the original Comprar row so the operator can retry (the preflight
        re-gates every tap, so re-enabling the button is always safe).
        Best-effort: a Telegram hiccup here must not mask the outcome.
        """
        try:
            if isinstance(outcome, BuyOutcomeSuccess):
                keyboard = [
                    [
                        InlineButton(
                            text="✅ Comprado",
                            callback_data=f"listing:noop:{alert_id}",
                        )
                    ]
                ]
            else:
                keyboard = [_phase2_button_row(str(alert_id))]
            await self.telegram_surface.edit_keyboard(callback_event.message_id, keyboard)
        except Exception as exc:
            self._log.warning(
                "buy_keyboard_restore_failed",
                extra={
                    "alert_id": str(alert_id),
                    "telegram_message_id": callback_event.message_id,
                    "error_class": exc.__class__.__name__,
                },
            )

    # ─────────────────────────────────────────────────────────────────
    # Steps
    # ─────────────────────────────────────────────────────────────────

    async def _run(self, snapshot: AlertSnapshot, callback_event: CallbackEvent) -> BuyOutcome:
        # ── Re-run preflight ─────────────────────────────────────────
        entry = self.wishlist_loader(snapshot.entry_key)
        if entry is None:
            outcome = BuyOutcomeAborted(
                reason="entry_not_in_wishlist",
                rendered_as=BuyFailureReason.ui_check_failed,
            )
            await self._dispatch_failure(snapshot, BuyFailureReason.ui_check_failed, {})
            return outcome

        check = await self.preflight.check(entry, snapshot.listing, snapshot.evaluation)
        if not check.eligible:
            reason = check.reason or "unknown"
            mapped = _PREFLIGHT_REASON_TO_FAILURE.get(reason, BuyFailureReason.ui_check_failed)
            await self._dispatch_failure(snapshot, mapped, {"detail": reason})
            return BuyOutcomeAborted(reason=reason, rendered_as=mapped, ctx={"detail": reason})

        # ── Record the Buy tap (audit-first) ─────────────────────────
        await self._record_tap(snapshot, callback_event)

        # ── Cross-source reconciliation ──────────────────────────────
        try:
            cross = await self.reconciler.reconcile_cross_source(snapshot.listing)
        except Exception as exc:
            # A 404 from either marketplace API means the listing no longer
            # exists — sold or withdrawn between the alert and the tap. That
            # is a normal marketplace outcome, not a system failure: tell the
            # operator plainly and do NOT count it toward the circuit breaker
            # (two overnight sales almost opened it, 2026-07-16).
            if getattr(exc, "status_code", None) == 404:
                self._log.info(
                    "buy_orchestrator_listing_gone",
                    extra={"listing_id": snapshot.listing.listing_id},
                )
                gone_ctx: dict[str, Any] = {"detail": "listing returned 404 on re-fetch"}
                await self._dispatch_failure(snapshot, BuyFailureReason.listing_gone, gone_ctx)
                return BuyOutcomeAborted(
                    reason="listing_gone",
                    rendered_as=BuyFailureReason.listing_gone,
                    ctx=gone_ctx,
                )
            self._log.error(
                "buy_orchestrator_cross_source_failed",
                extra={"error_class": exc.__class__.__name__, "error": str(exc)[:200]},
            )
            await self.circuit_breaker.record_outcome(
                "failure", last_affected_entry=snapshot.entry_display_name
            )
            ctx: dict[str, Any] = {
                "error_class": exc.__class__.__name__,
                "detail": str(exc),
            }
            await self._dispatch_failure(snapshot, BuyFailureReason.marketplace_error, ctx)
            return BuyOutcomeFailure(reason=BuyFailureReason.marketplace_error, ctx=ctx)

        if not cross.result.passed:
            ctx = {
                "api_price": cross.primary_price_eur,
                "html_price": cross.cross_source_price_eur,
                "tolerance_eur": cross.result.tolerance_used,
            }
            await self.circuit_breaker.record_outcome(
                "failure", last_affected_entry=snapshot.entry_display_name
            )
            await self._dispatch_failure(snapshot, BuyFailureReason.reconciliation_tripped, ctx)
            return BuyOutcomeFailure(reason=BuyFailureReason.reconciliation_tripped, ctx=ctx)

        # ── Execute the buy ─────────────────────────────────────────
        # The snapshot is authoritative for the ceiling (set at render
        # time per Story 5.2); a pre-5.2 snapshot can fall back to the
        # current entry config. Preflight has already guaranteed at
        # least one of these is set.
        max_price = snapshot.phase2_max_price_eur or entry.phase2.max_price_eur
        assert max_price is not None, "preflight guarantees a Phase 2 ceiling exists"
        buy_result = await self.browser.execute_buy(snapshot.listing, max_price)

        if isinstance(buy_result, BuyFailure):
            await self.circuit_breaker.record_outcome(
                "failure", last_affected_entry=snapshot.entry_display_name
            )
            await self._dispatch_failure(snapshot, buy_result.reason, buy_result.ctx)
            return BuyOutcomeFailure(reason=buy_result.reason, ctx=buy_result.ctx)

        assert isinstance(buy_result, BuySuccess)  # for type-narrowing

        # ── Persist the transaction ─────────────────────────────────
        txn = TransactionRecord(
            alert_id=snapshot.alert_id,
            price_paid_eur=buy_result.price_paid_eur,
            payment_method=buy_result.payment_method,
            receipt_id=buy_result.receipt_id,
            screenshot_path=buy_result.screenshot_url,
            total_seconds=buy_result.total_seconds,
            committed_at=self.clock(),
        )
        audit_id = await self.audit_writer.record_transaction(txn)

        # ── Receipt-vs-alert reconciliation (post-buy) ──────────────
        receipt_check = self.reconciler.reconcile_receipt_vs_alert(snapshot, txn)
        if not receipt_check.passed:
            await self.audit_writer.set_global_disable(RECEIPT_MISMATCH_REASON)
            await self.reporter.report(
                "warn",
                EventName.phase2_disabled,
                ctx={
                    "reason": RECEIPT_MISMATCH_REASON,
                    "last_affected_entry": snapshot.entry_display_name,
                    # Surface the breakdown so the operator can see whether the
                    # drift is in the item price or shipping/fees: reconciliation
                    # now compares delivered totals (shipping-aware-pricing).
                    "item_price_eur": str(snapshot.listing.price_eur),
                    "receipt_total_eur": str(txn.price_paid_eur),
                    "delta_eur": str(receipt_check.delta_eur),
                },
            )

        # The buy itself succeeded — the breaker counter resets even when
        # the receipt drifts (the breaker is about the buy attempt, the
        # global-disable is about the price-parser drift).
        await self.circuit_breaker.record_outcome("success")

        await self._dispatch_success(snapshot, txn, audit_id)
        return BuyOutcomeSuccess(transaction=txn, audit_id=audit_id)

    async def _record_tap(self, snapshot: AlertSnapshot, callback_event: CallbackEvent) -> None:
        """Append the Buy-tap audit row. Best-effort: a failure here
        logs but does not abort the buy — the operator already tapped
        and they get a clearer outcome from the buy itself."""
        try:
            await self.audit_writer.record_tap_event(
                TapEventRecord(
                    alert_id=snapshot.alert_id,
                    verb="buy",
                    raw_payload={
                        "callback_query_id": callback_event.callback_query_id,
                        "callback_data": callback_event.callback_data,
                        "message_id": callback_event.message_id,
                    },
                    tapped_at=self.clock(),
                    ip_or_chat_id=str(callback_event.chat_id),
                )
            )
        except Exception as exc:
            self._log.warning(
                "buy_orchestrator_tap_audit_failed",
                extra={"error_class": exc.__class__.__name__, "detail": str(exc)},
            )

    async def _handle_unexpected(
        self,
        snapshot: AlertSnapshot,
        callback_event: CallbackEvent,
        exc: Exception,
    ) -> BuyOutcome:
        """The catch-all: emit the operational alert, fail the circuit,
        and return a typed marketplace-error outcome."""
        self._log.error(
            "buy_orchestrator_unexpected_error",
            extra={"error_class": exc.__class__.__name__, "detail": str(exc)},
        )
        ctx_alert = {
            "error_class": exc.__class__.__name__,
            "alert_id": str(snapshot.alert_id),
        }
        try:
            await self.reporter.report("warn", EventName.buy_orchestrator_error, ctx=ctx_alert)
        except Exception:
            self._log.error("buy_orchestrator_reporter_failed_too")
        try:
            await self.circuit_breaker.record_outcome(
                "failure", last_affected_entry=snapshot.entry_display_name
            )
        except Exception:
            self._log.error("buy_orchestrator_circuit_record_failed_too")

        failure_ctx: dict[str, Any] = {
            "error_class": exc.__class__.__name__,
            "detail": str(exc),
        }
        await self._dispatch_failure(snapshot, BuyFailureReason.marketplace_error, failure_ctx)
        # Suppress the unused parameter warning — callback_event reserved
        # for future telemetry / retry contexts.
        _ = callback_event
        return BuyOutcomeFailure(reason=BuyFailureReason.marketplace_error, ctx=failure_ctx)

    # ─────────────────────────────────────────────────────────────────
    # Telegram dispatch
    # ─────────────────────────────────────────────────────────────────

    async def _dispatch_success(
        self,
        snapshot: AlertSnapshot,
        transaction: TransactionRecord,
        audit_id: int,
    ) -> None:
        rendered = render_phase2_buy_success(
            transaction,
            entry_display_name=snapshot.entry_display_name,
            audit_id=audit_id,
        )
        await self._send(rendered_kind="success", rendered=rendered, snapshot=snapshot)

    async def _dispatch_failure(
        self,
        snapshot: AlertSnapshot,
        reason: BuyFailureReason,
        ctx: dict[str, Any],
    ) -> None:
        rendered = render_phase2_buy_failure(
            reason, entry_display_name=snapshot.entry_display_name, ctx=ctx
        )
        await self._send(rendered_kind="failure", rendered=rendered, snapshot=snapshot)

    async def _send(
        self,
        *,
        rendered_kind: str,
        rendered: Any,
        snapshot: AlertSnapshot,
    ) -> None:
        try:
            await self.telegram_surface.send(rendered)
        except Exception as exc:
            self._log.error(
                "buy_orchestrator_telegram_dispatch_failed",
                extra={
                    "kind": rendered_kind,
                    "alert_id": str(snapshot.alert_id),
                    "error_class": exc.__class__.__name__,
                },
            )


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _parse_alert_id(callback_data: str) -> uuid_module.UUID | None:
    """Pull the UUID off a ``listing:<verb>:<uuid>`` callback_data."""
    parts = callback_data.split(":")
    if len(parts) != 3:
        return None
    try:
        return uuid_module.UUID(parts[2])
    except ValueError:
        return None


__all__ = [
    "RECEIPT_MISMATCH_REASON",
    "BuyOrchestrator",
    "BuyOutcome",
    "BuyOutcomeAborted",
    "BuyOutcomeFailure",
    "BuyOutcomeSuccess",
    "WishlistLoader",
]
