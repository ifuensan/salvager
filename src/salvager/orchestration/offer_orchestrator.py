"""Offer orchestrator (wallapop-offer-flow, FR50-FR57).

The single end-to-end flow the operator's Ofertar tap drives:

    snapshot lookup
       → re-run offer pre-flight (state may have changed since the alert)
       → reconciliation re-fetch by internal listing id (404 → listing_gone)
       → recompute the offer amount from the fresh listing (drift → abort)
       → execute_offer via the OfferSession adapter
       → append the offers audit row
       → lockout outcome (safety aborts never count; success resets)
       → Telegram dispatch (sent / failure / aborted)
       → keyboard restore on EVERY path (the v0.4.3 lesson)

Sibling of :class:`BuyOrchestrator` with the same composure discipline:
ports and typed collaborators only, no SDK imports, and a typed
:class:`OfferOutcome` union so the callback handler branches on ``kind``.

No money moves here — an offer is a negotiation message; the purchase
(if the seller accepts) stays behind the Comprar path, manually, in v1.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter
from salvager.domain.alert import (
    CallbackEvent,
    EventName,
    InlineButton,
    _phase1_button_row,
    _phase2_button_row,
    negotiable_button_row,
    offer_button_row,
    offer_sent_badge_row,
    render_offer_failure,
    render_offer_sent,
)
from salvager.domain.errors import OfferFailureReason
from salvager.domain.offer_audit import OfferAttemptRecord
from salvager.domain.pricing import offer_item_price_eur
from salvager.interfaces.offer_session import OfferSendFailure, OfferSession, OfferSuccess
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.store import Store
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.observability.logging import get_logger
from salvager.orchestration.degradation_reporter import Reporter
from salvager.orchestration.offer_preflight import OfferPreflight

if TYPE_CHECKING:
    from salvager.domain.alert import AlertSnapshot
    from salvager.domain.wishlist import WishlistEntry

EntryKey = tuple[str, str, str]
WishlistLoader = Callable[[EntryKey], "WishlistEntry | None"]

#: The reason persisted in ``offer_state.disabled_reason`` when the
#: consecutive-failure threshold engages the lockout.
OFFER_LOCKOUT_REASON: Final[str] = "offer_lockout_threshold"

#: Safety aborts that never increment the lockout counter (spec: the
#: offer path is healthy — the abort itself proves the guardrails work).
_NO_LOCKOUT_REASONS: Final[frozenset[OfferFailureReason]] = frozenset(
    {
        OfferFailureReason.listing_gone,
        OfferFailureReason.reconciliation_tripped,
        OfferFailureReason.duplicate_offer,
        OfferFailureReason.lockout_engaged,
        OfferFailureReason.daily_limit_reached,
    }
)

#: Preflight reason id → the closed render variant. The raw reason is
#: preserved in the outcome ctx for audits.
_PREFLIGHT_REASON_TO_FAILURE: Final[dict[str, OfferFailureReason]] = {
    "offer_disabled_for_entry": OfferFailureReason.ui_check_failed,
    "not_wallapop": OfferFailureReason.ui_check_failed,
    "listing_refurbished": OfferFailureReason.offer_unavailable,
    "listing_reserved": OfferFailureReason.reconciliation_tripped,
    "offer_kill_switch": OfferFailureReason.lockout_engaged,
    "offer_lockout_engaged": OfferFailureReason.lockout_engaged,
    "offer_daily_limit_reached": OfferFailureReason.daily_limit_reached,
    "duplicate_offer": OfferFailureReason.duplicate_offer,
}


# ─────────────────────────────────────────────────────────────────────────
# OfferOutcome — discriminated union returned to the callback handler
# ─────────────────────────────────────────────────────────────────────────


class OfferOutcomeSuccess(BaseModel):
    """The offer was verifiably sent and audited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["success"] = "success"
    offered_eur: Decimal
    audit_id: int


class OfferOutcomeFailure(BaseModel):
    """The send was attempted and failed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["failure"] = "failure"
    reason: OfferFailureReason
    ctx: dict[str, Any] = Field(default_factory=dict)


class OfferOutcomeAborted(BaseModel):
    """A guardrail refused before the marketplace was touched.

    ``reason`` is the raw preflight/reconciliation identifier;
    ``rendered_as`` is the closed variant the operator-facing alert used.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["aborted"] = "aborted"
    reason: str
    rendered_as: OfferFailureReason | None = None
    ctx: dict[str, Any] = Field(default_factory=dict)


OfferOutcome = Annotated[
    OfferOutcomeSuccess | OfferOutcomeFailure | OfferOutcomeAborted,
    Field(discriminator="kind"),
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────────
# OfferOrchestrator
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class OfferOrchestrator:
    """Composes the offer collaborators into ``execute_offer_from_callback``.

    ``fetcher`` is the Wallapop :class:`PageFetcher` — the reconciliation
    re-fetch goes through ``fetch_listing`` (internal listing id; slugs
    404, the v0.4.2 lesson). ``tolerance_eur``/``tolerance_pct`` reuse
    the Phase 2 reconciliation tolerances for displayed-vs-recomputed
    amount drift.
    """

    preflight: OfferPreflight
    fetcher: PageFetcher
    offer_session: OfferSession
    offer_writer: OfferAuditWriter
    telegram_surface: TelegramSurface
    store: Store
    reporter: Reporter
    wishlist_loader: WishlistLoader
    lockout_threshold: int
    tolerance_eur: Decimal = Decimal("1.00")
    tolerance_pct: Decimal = Decimal("5")
    assumed_shipping_eur: Decimal = Decimal("3.50")
    clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        self._log = get_logger("orchestration.offer_orchestrator")

    async def execute_offer_from_callback(self, callback_event: CallbackEvent) -> OfferOutcome:
        """Drive the full offer pipeline for one Ofertar tap.

        A single ``try/except`` wraps everything downstream of the
        snapshot lookup; an unexpected exception emits
        ``EventName.offer_orchestrator_error``, counts a lockout failure
        and returns ``OfferOutcomeFailure(marketplace_error)``. The
        keyboard is restored on EVERY path.
        """
        alert_id = _parse_alert_id(callback_event.callback_data)
        if alert_id is None:
            self._log.warning(
                "offer_orchestrator_callback_data_unparseable",
                extra={"callback_data": callback_event.callback_data},
            )
            return OfferOutcomeAborted(reason="callback_data_unparseable")

        snapshot = await self.store.get_alert_snapshot_by_alert_id(alert_id)
        if snapshot is None:
            self._log.warning(
                "offer_orchestrator_snapshot_missing", extra={"alert_id": str(alert_id)}
            )
            aborted = OfferOutcomeAborted(reason="snapshot_not_found")
            await self._restore_keyboard(callback_event, None, alert_id, aborted)
            return aborted

        try:
            outcome: OfferOutcome = await self._run(snapshot, callback_event)
        except Exception as exc:
            outcome = await self._handle_unexpected(snapshot, exc)
        await self._restore_keyboard(callback_event, snapshot, alert_id, outcome)
        return outcome

    # ─────────────────────────────────────────────────────────────────
    # Steps
    # ─────────────────────────────────────────────────────────────────

    async def _run(self, snapshot: AlertSnapshot, callback_event: CallbackEvent) -> OfferOutcome:
        _ = callback_event  # the tap itself is audited by the callbacks table
        entry = self.wishlist_loader(snapshot.entry_key)
        if entry is None:
            ctx: dict[str, Any] = {"detail": "entry_not_in_wishlist"}
            await self._dispatch_failure(snapshot, OfferFailureReason.ui_check_failed, ctx)
            return OfferOutcomeAborted(
                reason="entry_not_in_wishlist",
                rendered_as=OfferFailureReason.ui_check_failed,
                ctx=ctx,
            )

        check = await self.preflight.check(entry, snapshot.listing)
        if not check.eligible:
            reason = check.reason or "unknown"
            mapped = _PREFLIGHT_REASON_TO_FAILURE.get(reason, OfferFailureReason.ui_check_failed)
            ctx = await self._preflight_ctx(reason)
            await self._dispatch_failure(snapshot, mapped, ctx)
            return OfferOutcomeAborted(reason=reason, rendered_as=mapped, ctx=ctx)

        # ── Reconciliation re-fetch by internal id ───────────────────
        try:
            fresh = await self.fetcher.fetch_listing(snapshot.listing)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                self._log.info(
                    "offer_orchestrator_listing_gone",
                    extra={"listing_id": snapshot.listing.listing_id},
                )
                gone_ctx: dict[str, Any] = {"detail": "listing returned 404 on re-fetch"}
                await self._dispatch_failure(snapshot, OfferFailureReason.listing_gone, gone_ctx)
                return OfferOutcomeAborted(
                    reason="listing_gone",
                    rendered_as=OfferFailureReason.listing_gone,
                    ctx=gone_ctx,
                )
            self._log.error(
                "offer_orchestrator_refetch_failed",
                extra={"error_class": exc.__class__.__name__, "error": str(exc)[:200]},
            )
            fail_ctx: dict[str, Any] = {
                "error_class": exc.__class__.__name__,
                "detail": str(exc),
            }
            return await self._fail(snapshot, OfferFailureReason.marketplace_error, fail_ctx)

        target = entry.offer.target_total_eur or _entry_ceiling(entry)
        displayed = offer_item_price_eur(
            snapshot.listing,
            target_total_eur=target,
            assumed_shipping_eur=self.assumed_shipping_eur,
        )
        recon_ctx: dict[str, Any] = {"displayed_offer": displayed}

        if fresh.is_reserved:
            recon_ctx["detail"] = "listing is now reserved"
            await self._dispatch_failure(
                snapshot, OfferFailureReason.reconciliation_tripped, recon_ctx
            )
            return OfferOutcomeAborted(
                reason="listing_reserved_on_refetch",
                rendered_as=OfferFailureReason.reconciliation_tripped,
                ctx=recon_ctx,
            )

        recomputed = offer_item_price_eur(
            fresh,
            target_total_eur=target,
            assumed_shipping_eur=self.assumed_shipping_eur,
        )
        if recomputed is None or (
            displayed is not None and not self._within_tolerance(displayed, recomputed)
        ):
            recon_ctx["recomputed_offer"] = recomputed
            recon_ctx["fresh_price_eur"] = str(fresh.price_eur)
            await self._dispatch_failure(
                snapshot, OfferFailureReason.reconciliation_tripped, recon_ctx
            )
            return OfferOutcomeAborted(
                reason="offer_amount_drifted",
                rendered_as=OfferFailureReason.reconciliation_tripped,
                ctx=recon_ctx,
            )

        # ── Execute the send ─────────────────────────────────────────
        result = await self.offer_session.execute_offer(fresh, recomputed)

        if isinstance(result, OfferSendFailure):
            await self._record_attempt(
                snapshot,
                fresh,
                recomputed,
                outcome="failure",
                failure_reason=result.reason,
                platform_remaining=result.ctx.get("platform_remaining"),
            )
            return await self._fail(snapshot, result.reason, dict(result.ctx))

        assert isinstance(result, OfferSuccess)  # type-narrowing
        audit_id = await self._record_attempt(
            snapshot,
            fresh,
            recomputed,
            outcome="success",
            screenshot_path=result.screenshot_url,
            platform_remaining=result.platform_remaining,
        )
        await self.offer_writer.reset_failure_counter()
        await self._dispatch_sent(snapshot, recomputed, audit_id, result)
        return OfferOutcomeSuccess(offered_eur=recomputed, audit_id=audit_id)

    def _within_tolerance(self, displayed: Decimal, recomputed: Decimal) -> bool:
        tolerance = max(self.tolerance_eur, displayed * self.tolerance_pct / 100)
        return abs(displayed - recomputed) <= tolerance

    async def _preflight_ctx(self, reason: str) -> dict[str, Any]:
        ctx: dict[str, Any] = {"detail": reason}
        if reason == "offer_lockout_engaged":
            state = await self.offer_writer.read_state()
            ctx["consecutive_failures"] = state.consecutive_failures
            ctx["threshold"] = self.lockout_threshold
        if reason == "offer_daily_limit_reached":
            ctx["limit_source"] = "propio"
        return ctx

    async def _fail(
        self,
        snapshot: AlertSnapshot,
        reason: OfferFailureReason,
        ctx: dict[str, Any],
    ) -> OfferOutcomeFailure:
        """Common failure path: lockout accounting + operator alert."""
        if reason not in _NO_LOCKOUT_REASONS:
            failures = await self.offer_writer.increment_failure_counter()
            if failures >= self.lockout_threshold:
                await self.offer_writer.set_global_disable(OFFER_LOCKOUT_REASON)
                await self._report(
                    EventName.offer_lockout_engaged,
                    {
                        "consecutive_failures": failures,
                        "threshold": self.lockout_threshold,
                        "last_affected_entry": snapshot.entry_display_name,
                    },
                )
        await self._dispatch_failure(snapshot, reason, ctx)
        return OfferOutcomeFailure(reason=reason, ctx=ctx)

    async def _record_attempt(
        self,
        snapshot: AlertSnapshot,
        fresh: Any,
        offered: Decimal,
        *,
        outcome: Literal["success", "failure", "aborted"],
        failure_reason: OfferFailureReason | None = None,
        screenshot_path: str | None = None,
        platform_remaining: int | None = None,
    ) -> int:
        """Append the offers audit row. Best-effort on failure outcomes;
        a success MUST land (the dedupe depends on it) so it propagates."""
        record = OfferAttemptRecord(
            alert_id=snapshot.alert_id,
            listing_id=snapshot.listing.listing_id,
            marketplace=snapshot.listing.marketplace,
            entry_key=snapshot.entry_key,
            offered_eur=offered,
            asking_eur=fresh.price_eur,
            outcome=outcome,
            failure_reason=failure_reason,
            screenshot_path=screenshot_path,
            platform_remaining=platform_remaining,
            attempted_at=self.clock(),
        )
        if outcome == "success":
            return await self.offer_writer.record_offer_attempt(record)
        try:
            return await self.offer_writer.record_offer_attempt(record)
        except Exception as exc:
            self._log.warning(
                "offer_orchestrator_audit_failed",
                extra={"error_class": exc.__class__.__name__, "detail": str(exc)},
            )
            return 0

    async def _handle_unexpected(self, snapshot: AlertSnapshot, exc: Exception) -> OfferOutcome:
        self._log.error(
            "offer_orchestrator_unexpected_error",
            extra={"error_class": exc.__class__.__name__, "detail": str(exc)},
        )
        await self._report(
            EventName.offer_orchestrator_error,
            {"error_class": exc.__class__.__name__, "alert_id": str(snapshot.alert_id)},
        )
        ctx: dict[str, Any] = {"error_class": exc.__class__.__name__, "detail": str(exc)}
        try:
            return await self._fail(snapshot, OfferFailureReason.marketplace_error, ctx)
        except Exception:
            self._log.error("offer_orchestrator_failure_path_failed_too")
            return OfferOutcomeFailure(reason=OfferFailureReason.marketplace_error, ctx=ctx)

    async def _report(self, event: EventName, ctx: dict[str, Any]) -> None:
        try:
            await self.reporter.report("warn", event, ctx=ctx)
        except Exception:
            self._log.error("offer_orchestrator_reporter_failed", extra={"event": event.value})

    # ─────────────────────────────────────────────────────────────────
    # Telegram dispatch + keyboard restore
    # ─────────────────────────────────────────────────────────────────

    async def _dispatch_sent(
        self,
        snapshot: AlertSnapshot,
        offered: Decimal,
        audit_id: int,
        result: OfferSuccess,
    ) -> None:
        rendered = render_offer_sent(
            entry_display_name=snapshot.entry_display_name,
            offered_eur=offered,
            audit_id=audit_id,
            screenshot_path=result.screenshot_url,
            platform_remaining=result.platform_remaining,
        )
        await self._send("sent", rendered, snapshot)

    async def _dispatch_failure(
        self,
        snapshot: AlertSnapshot,
        reason: OfferFailureReason,
        ctx: dict[str, Any],
    ) -> None:
        rendered = render_offer_failure(
            reason, entry_display_name=snapshot.entry_display_name, ctx=ctx
        )
        await self._send("failure", rendered, snapshot)

    async def _send(self, kind: str, rendered: Any, snapshot: AlertSnapshot) -> None:
        try:
            await self.telegram_surface.send(rendered)
        except Exception as exc:
            self._log.error(
                "offer_orchestrator_telegram_dispatch_failed",
                extra={
                    "kind": kind,
                    "alert_id": str(snapshot.alert_id),
                    "error_class": exc.__class__.__name__,
                },
            )

    async def _restore_keyboard(
        self,
        callback_event: CallbackEvent,
        snapshot: AlertSnapshot | None,
        alert_id: UUID,
        outcome: OfferOutcome,
    ) -> None:
        """Repaint the tapped message's keyboard to match the outcome.

        Success → the terminal ``💰 Oferta enviada`` badge (plus the
        phase's own row on non-negotiable alerts, so Comprar survives an
        offer). Failure/abort → the original rows so the operator can
        retry (the preflight re-gates every tap). Best-effort.
        """
        try:
            keyboard = _outcome_keyboard(snapshot, alert_id, outcome)
            await self.telegram_surface.edit_keyboard(callback_event.message_id, keyboard)
        except Exception as exc:
            self._log.warning(
                "offer_keyboard_restore_failed",
                extra={
                    "alert_id": str(alert_id),
                    "telegram_message_id": callback_event.message_id,
                    "error_class": exc.__class__.__name__,
                },
            )


def _entry_ceiling(entry: WishlistEntry) -> Decimal:
    ceiling = entry.max_price_solo or entry.max_price_in_device
    assert ceiling is not None, "wishlist validation guarantees a ceiling"
    return ceiling


def _outcome_keyboard(
    snapshot: AlertSnapshot | None,
    alert_id: UUID,
    outcome: OfferOutcome,
) -> list[list[InlineButton]]:
    """The keyboard the message deserves after an offer outcome."""
    aid = str(alert_id)
    phase = snapshot.phase if snapshot is not None else "phase1"
    if isinstance(outcome, OfferOutcomeSuccess):
        if phase == "negotiable":
            return [offer_sent_badge_row(aid)]
        base = _phase2_button_row(aid) if phase == "phase2" else _phase1_button_row(aid)
        return [base, offer_sent_badge_row(aid)]
    if phase == "negotiable":
        return [negotiable_button_row(aid)]
    base = _phase2_button_row(aid) if phase == "phase2" else _phase1_button_row(aid)
    return [base, offer_button_row(aid)]


def _parse_alert_id(callback_data: str) -> UUID | None:
    parts = callback_data.split(":")
    if len(parts) != 3:
        return None
    try:
        return uuid_module.UUID(parts[2])
    except ValueError:
        return None


__all__ = [
    "OFFER_LOCKOUT_REASON",
    "OfferOrchestrator",
    "OfferOutcome",
    "OfferOutcomeAborted",
    "OfferOutcomeFailure",
    "OfferOutcomeSuccess",
    "WishlistLoader",
]
