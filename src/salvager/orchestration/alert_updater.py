"""Watch-diff + alert-edit dispatch — edit-alerts-on-state-change.

The poll cycle calls :func:`process_entry_watches` with the freshly
fetched listings BEFORE the dedup filter discards the already-alerted
ones — the state-change signal is in data we currently throw away, so
detection costs zero extra marketplace calls.

Detected transitions (design.md Resolved Question 1):

- ``is_reserved`` false→true → banner ``🔴 RESERVADO`` (+ dead Comprar
  badge on Phase 2 alerts);
- true→false flip-back → banner ``🟢 Disponible de nuevo`` (Comprar
  restored);
- price drops ≥ ``min_price_drop_pct`` AND ≥ ``min_price_drop_eur`` →
  banner ``📉 <new> (antes <old>)``; a drop ≥ ``price_drop_ping_pct``
  ALSO sends a short new reply message (edits are silent).

Price increases and sub-threshold drops advance the watch silently.
Edits are best-effort and single-attempt: the watch's last-known state
advances ONLY after the whole unit of work (edit + optional ping)
succeeded, so a failure re-fires the same diff next cycle (Decision 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from salvager.domain.alert import (
    AlertSnapshot,
    InlineButton,
    RenderedAlert,
    _phase1_button_row,
    _phase2_button_row,
    apply_update_banner,
    phase2_dead_reserved_row,
    render_phase1_listing_alert,
    render_phase2_listing_alert,
    render_price_drop_ping,
    update_banner_line,
)
from salvager.domain.alert_watch import AlertUpdate, AlertWatch, ChangeKind
from salvager.domain.errors import TelegramMessageGone
from salvager.domain.listing import Listing
from salvager.domain.pricing import buyer_cost
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.store import Store
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.orchestration.callback_handler import _acknowledgment_keyboard

#: Callback verbs that leave the acknowledgment row on the message.
_ACKED_VERBS: frozenset[str] = frozenset({"view", "skip", "snooze"})


@dataclass(frozen=True)
class AlertUpdatePolicy:
    """The ``alerts`` config section, flattened for orchestration.

    Composer builds it from ``config.alerts``; defaults mirror
    ``config.example.yaml`` for Phase 1-only / test construction.
    """

    watch_days: int = 7
    min_price_drop_pct: Decimal = Decimal("1")
    min_price_drop_eur: Decimal = Decimal("0.50")
    price_drop_ping_pct: Decimal = Decimal("10")
    #: How long a `buy` tap suppresses edits. Callbacks are append-only, so
    #: without an age-out a completed buy would suppress edits forever
    #: (CodeRabbit, PR #41); real buys resolve in minutes.
    buy_suppression_minutes: int = 30


DEFAULT_ALERT_UPDATE_POLICY = AlertUpdatePolicy()


@dataclass(frozen=True)
class _Diff:
    """Outcome of diffing one watch against its freshly fetched listing."""

    change: ChangeKind | None  # None = nothing to edit
    ping: bool = False  # big drop → also send the reply ping
    advance_silently: bool = False  # price moved but below threshold / increase


def detect_change(watch: AlertWatch, listing: Listing, policy: AlertUpdatePolicy) -> _Diff:
    """Pure diff of a watch's last-known state against the fresh listing.

    Reserved flips take precedence over simultaneous price movement —
    the banner shows the state change and the re-rendered body carries
    the current price anyway.
    """
    if listing.is_reserved != watch.last_is_reserved:
        return _Diff(change="reserved" if listing.is_reserved else "available")

    drop = watch.last_price_eur - listing.price_eur
    if drop <= 0:
        # Increase (or no change): never edit; advance so a later drop is
        # measured against the newest price.
        return _Diff(change=None, advance_silently=listing.price_eur != watch.last_price_eur)

    drop_pct = drop / watch.last_price_eur * 100
    if drop_pct >= policy.min_price_drop_pct and drop >= policy.min_price_drop_eur:
        return _Diff(change="price_drop", ping=drop_pct >= policy.price_drop_ping_pct)
    return _Diff(change=None, advance_silently=True)


def reconstruct_keyboard(
    snapshot: AlertSnapshot,
    last_verb: str | None,
    *,
    now_reserved: bool,
) -> list[list[InlineButton]] | None:
    """The keyboard the edited message currently deserves.

    Telegram drops the keyboard on any body edit that omits
    ``reply_markup``, and the current keyboard may no longer be the one
    sent (the callback dispatcher repaints ack rows). The caller has
    already excluded the in-flight ``buy`` verb (never repaint under a
    running buy).
    """
    alert_id = str(snapshot.alert_id)
    if last_verb in _ACKED_VERBS:
        return _acknowledgment_keyboard(last_verb, snapshot.alert_id)
    if snapshot.phase == "phase2":
        if now_reserved:
            return [phase2_dead_reserved_row(alert_id)]
        return [_phase2_button_row(alert_id)]
    return [_phase1_button_row(alert_id)]


async def process_entry_watches(
    entry: WishlistEntry,
    listings_by_id: dict[str, Listing],
    *,
    marketplace: str,
    store: Store,
    telegram: TelegramSurface,
    policy: AlertUpdatePolicy,
    assumed_shipping_eur: Decimal,
    assumed_import_charges_eur: Decimal,
    now: datetime,
    log: object,
) -> None:
    """Diff the entry's active watches against this cycle's fetch and edit
    changed alerts. Never raises — edits are strictly best-effort."""
    try:
        watches = await store.active_watches(entry.entry_key, marketplace=marketplace, now=now)
    except Exception as exc:
        log.exception(  # type: ignore[attr-defined]
            "alert_watch_read_failed",
            extra={"entry_display_name": entry.display_name, "error_class": type(exc).__name__},
        )
        return

    for watch in watches:
        listing = listings_by_id.get(watch.listing_id)
        if listing is None:
            # Absence is NOT sold (pagination drift / marketplace hiccup) —
            # leave the watch untouched (design.md Resolved Question 2).
            continue
        try:
            await _process_one_watch(
                watch,
                listing,
                store=store,
                telegram=telegram,
                policy=policy,
                assumed_shipping_eur=assumed_shipping_eur,
                assumed_import_charges_eur=assumed_import_charges_eur,
                now=now,
                log=log,
            )
        except Exception as exc:
            log.exception(  # type: ignore[attr-defined]
                "alert_update_failed",
                extra={
                    "alert_id": str(watch.alert_id),
                    "listing_id": watch.listing_id,
                    "error_class": type(exc).__name__,
                },
            )


async def _process_one_watch(
    watch: AlertWatch,
    listing: Listing,
    *,
    store: Store,
    telegram: TelegramSurface,
    policy: AlertUpdatePolicy,
    assumed_shipping_eur: Decimal,
    assumed_import_charges_eur: Decimal,
    now: datetime,
    log: object,
) -> None:
    diff = detect_change(watch, listing, policy)
    if diff.change is None:
        if diff.advance_silently:
            await store.advance_watch(
                watch.alert_id,
                price_eur=listing.price_eur,
                is_reserved=listing.is_reserved,
            )
        return

    last_callback = await store.get_last_callback_verb(watch.alert_id)
    last_verb = last_callback[0] if last_callback is not None else None
    if last_verb == "buy":
        # Never repaint under a RUNNING buy — but callbacks are append-only,
        # so the marker must age out or a completed buy would suppress edits
        # forever. Real buys resolve in minutes; after the window the diff
        # proceeds (a bought listing shows as reserved → dead badge, correct).
        tapped_at = last_callback[1] if last_callback is not None else now
        if (now - tapped_at) <= timedelta(minutes=policy.buy_suppression_minutes):
            log.info(  # type: ignore[attr-defined]
                "alert_update_skipped_buy_in_flight",
                extra={"alert_id": str(watch.alert_id), "change_kind": diff.change},
            )
            return
        last_verb = None  # aged out — reconstruct the phase keyboard

    snapshot = await store.get_alert_snapshot_by_alert_id(watch.alert_id)
    if snapshot is None:
        # No snapshot to re-render from — nothing sane to edit; drop the watch.
        log.warning(  # type: ignore[attr-defined]
            "alert_update_snapshot_missing",
            extra={"alert_id": str(watch.alert_id)},
        )
        await store.close_watch(watch.alert_id)
        return

    rendered = _render_update(
        snapshot,
        listing,
        diff.change,
        last_verb,
        previous_price_eur=watch.last_price_eur,
        assumed_shipping_eur=assumed_shipping_eur,
        assumed_import_charges_eur=assumed_import_charges_eur,
    )
    # The photo/text branch follows the ORIGINAL message's shape.
    has_photo = bool(snapshot.listing.photo_urls)

    edit_ok = False
    try:
        await telegram.edit_alert(watch.telegram_message_id, rendered, has_photo=has_photo)
        if diff.ping:
            await telegram.send(
                render_price_drop_ping(
                    snapshot.entry_display_name,
                    old_price_eur=watch.last_price_eur,
                    new_price_eur=listing.price_eur,
                ),
                reply_to_message_id=watch.telegram_message_id,
            )
        edit_ok = True
    except TelegramMessageGone:
        # Operator deleted the alert → terminal; close silently, no resend.
        await store.close_watch(watch.alert_id)
    except Exception as exc:
        # Single attempt: the state does NOT advance, so the next cycle
        # re-detects the same diff and retries (Decision 10).
        log.warning(  # type: ignore[attr-defined]
            "alert_edit_attempt_failed",
            extra={
                "alert_id": str(watch.alert_id),
                "change_kind": diff.change,
                "error_class": type(exc).__name__,
                "error": str(exc)[:200],
            },
        )

    try:
        await store.record_alert_update(
            AlertUpdate(
                alert_id=watch.alert_id,
                change_kind=diff.change,
                old_value=(
                    str(watch.last_price_eur)
                    if diff.change == "price_drop"
                    else str(watch.last_is_reserved)
                ),
                new_value=(
                    str(listing.price_eur)
                    if diff.change == "price_drop"
                    else str(listing.is_reserved)
                ),
                edited_at=now,
                edit_ok=edit_ok,
                rendered_text=rendered.text,
            )
        )
    except Exception as exc:
        log.exception(  # type: ignore[attr-defined]
            "alert_update_audit_failed",
            extra={"alert_id": str(watch.alert_id), "error_class": type(exc).__name__},
        )

    if edit_ok:
        await store.advance_watch(
            watch.alert_id,
            price_eur=listing.price_eur,
            is_reserved=listing.is_reserved,
            edited_at=now,
        )
        log.info(  # type: ignore[attr-defined]
            "alert_edited",
            extra={
                "alert_id": str(watch.alert_id),
                "listing_id": watch.listing_id,
                "change_kind": diff.change,
                "pinged": diff.ping,
            },
        )


def _render_update(
    snapshot: AlertSnapshot,
    listing: Listing,
    change: ChangeKind,
    last_verb: str | None,
    *,
    previous_price_eur: Decimal,
    assumed_shipping_eur: Decimal,
    assumed_import_charges_eur: Decimal,
) -> RenderedAlert:
    """Re-render the full body with current values + replaceable banner.

    The base renderers stay the single source of truth for alert anatomy
    (Decision 5); the comp row is omitted (it was an in-cycle signal at
    dispatch time, not current data).
    """
    updated = snapshot.model_copy(update={"listing": listing})
    cost = buyer_cost(
        listing,
        assumed_shipping_eur=assumed_shipping_eur,
        assumed_import_charges_eur=assumed_import_charges_eur,
    )
    if snapshot.phase == "phase2" and snapshot.phase2_max_price_eur is not None:
        base = render_phase2_listing_alert(updated, snapshot.phase2_max_price_eur, buyer_cost=cost)
    else:
        base = render_phase1_listing_alert(updated, buyer_cost=cost)

    banner = update_banner_line(
        change,
        old_price_eur=previous_price_eur,
        new_price_eur=listing.price_eur,
    )
    keyboard = reconstruct_keyboard(snapshot, last_verb, now_reserved=listing.is_reserved)
    return apply_update_banner(base, banner, keyboard)


__all__ = [
    "DEFAULT_ALERT_UPDATE_POLICY",
    "AlertUpdatePolicy",
    "detect_change",
    "process_entry_watches",
    "reconstruct_keyboard",
]
