"""Variant → :class:`RenderedAlert` registry — Story 5.17 release-audit.

One closed map: every release-gate variant name (66 entries: 45 at
v0.4.4 — 37 at the original v1.0 audit + ``listing_gone`` at v0.4.1 +
the 💶 buyer-total and edit-surface variants of the v0.4.3 re-audit —
plus the 21 wallapop-offer-flow surfaces: 4 offer-eligible listing
shapes, ``offer_sent``, 12 offer failures, 4 operational events) to a
zero-arg builder that returns a :class:`RenderedAlert`. The
fixture data mirrors what the snapshot tests use, so the dispatched
Telegram message and the file under
``docs/release-audits/v1.0/reference-text/<variant>.txt`` are
byte-for-byte the same MarkdownV2 string.

Why a separate module:

  - keeps :mod:`cli.commands.dev_cmd` thin (CLI wiring only) so the
    rendering is testable without a Typer app + Telegram surface;
  - lets the property tests in ``tests/unit/test_dev_emit_alert.py``
    iterate the closed enumeration and verify every renderer runs
    cleanly + produces a non-empty MarkdownV2 body.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from salvager.domain.alert import (
    AlertSnapshot,
    EventName,
    RenderedAlert,
    Severity,
    apply_update_banner,
    phase2_dead_reserved_row,
    render_negotiable_listing_alert,
    render_offer_failure,
    render_offer_sent,
    render_operational_alert,
    render_phase1_listing_alert,
    render_phase2_buy_failure,
    render_phase2_buy_success,
    render_phase2_listing_alert,
    render_price_drop_ping,
    update_banner_line,
)
from salvager.domain.errors import BuyFailureReason, OfferFailureReason
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.phase2_audit import TransactionRecord
from salvager.domain.pricing import BuyerCost, buyer_cost

_FIXED_ALERT_ID = UUID("12345678-1234-1234-1234-123456789abc")
_FIXED_TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
_PHASE2_MAX = Decimal("60.00")
_ENTRY_KEY = ("Western Digital", "WD Red Plus 4TB", "WD40EFPX")
_ENTRY_DISPLAY = "WD Red Plus 4TB (WD40EFPX)"


# ─────────────────────────────────────────────────────────────────────────
# Listing fixtures (shared between Phase 1 + Phase 2)
# ─────────────────────────────────────────────────────────────────────────


def _listing(**overrides: Any) -> Listing:
    base: dict[str, Any] = {
        "listing_id": "abc123",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/abc123",
        "title": "WD Red Plus 4TB",
        "description": "Como nuevo, en caja.",
        "price_eur": Decimal("55.00"),
        "location": "Madrid",
        # Placeholder image hosted by placehold.co (free, no auth, no
        # rate limits for occasional use) — Telegram fetches it during
        # send_photo. The fake `https://cdn/photo.jpg` we use in unit
        # tests is rejected by the real API ("Wrong http url specified").
        "photo_urls": ["https://placehold.co/600x400/png?text=Listing+photo"],
        "fetched_at": _FIXED_TS,
    }
    base.update(overrides)
    return Listing(**base)


def _evaluation(**overrides: Any) -> ListingEvaluation:
    base: dict[str, Any] = {
        "listing_id": "abc123",
        "entry_key": _ENTRY_KEY,
        "confidence": "high",
        "one_line_take": "WD Red Plus 4TB at 55€ — strong match.",
        "is_container": False,
        "evaluated_at": _FIXED_TS,
    }
    base.update(overrides)
    return ListingEvaluation(**base)


def _snapshot(
    *,
    phase: str = "phase1",
    listing_overrides: dict[str, Any] | None = None,
    evaluation_overrides: dict[str, Any] | None = None,
) -> AlertSnapshot:
    return AlertSnapshot(
        alert_id=_FIXED_ALERT_ID,
        entry_key=_ENTRY_KEY,
        entry_display_name=_ENTRY_DISPLAY,
        listing=_listing(**(listing_overrides or {})),
        evaluation=_evaluation(**(evaluation_overrides or {})),
        phase=phase,  # type: ignore[arg-type]
        phase2_max_price_eur=_PHASE2_MAX if phase == "phase2" else None,
        rendered_at=_FIXED_TS,
    )


# ─────────────────────────────────────────────────────────────────────────
# Phase 1 + Phase 2 listing builders
# ─────────────────────────────────────────────────────────────────────────


def _phase1_direct() -> RenderedAlert:
    return render_phase1_listing_alert(_snapshot())


def _phase1_container() -> RenderedAlert:
    return render_phase1_listing_alert(
        _snapshot(
            evaluation_overrides={
                "is_container": True,
                "wrapper_text": "Pack 4x HDD",
                "extracted_text": "WD Red Plus 4TB inside",
            }
        )
    )


def _phase1_missing_photo() -> RenderedAlert:
    return render_phase1_listing_alert(_snapshot(listing_overrides={"photo_urls": []}))


def _phase2_direct() -> RenderedAlert:
    return render_phase2_listing_alert(_snapshot(phase="phase2"), _PHASE2_MAX)


def _phase2_container() -> RenderedAlert:
    return render_phase2_listing_alert(
        _snapshot(
            phase="phase2",
            evaluation_overrides={
                "is_container": True,
                "wrapper_text": "Pack 4x HDD",
                "extracted_text": "WD Red Plus 4TB inside",
            },
        ),
        _PHASE2_MAX,
    )


def _phase2_missing_photo() -> RenderedAlert:
    return render_phase2_listing_alert(
        _snapshot(phase="phase2", listing_overrides={"photo_urls": []}),
        _PHASE2_MAX,
    )


def _cost(listing: Listing) -> BuyerCost:
    """The buyer-total breakdown production attaches to every listing alert
    (shipping-aware-pricing). Fixed buffers keep the fixture deterministic
    — same defaults the daemon falls back to."""
    return buyer_cost(
        listing,
        assumed_shipping_eur=Decimal("3.50"),
        assumed_import_charges_eur=Decimal("3.63"),
    )


def _phase1_with_cost() -> RenderedAlert:
    return render_phase1_listing_alert(_snapshot(), buyer_cost=_cost(_listing()))


def _phase1_with_import() -> RenderedAlert:
    overrides: dict[str, Any] = {
        "marketplace": "ebay",
        "url": "https://www.ebay.es/itm/123456",
        "shipping_eur": Decimal("16.82"),
        "country": "GB",
    }
    return render_phase1_listing_alert(
        _snapshot(listing_overrides=overrides), buyer_cost=_cost(_listing(**overrides))
    )


def _phase2_with_cost() -> RenderedAlert:
    return render_phase2_listing_alert(
        _snapshot(phase="phase2"), _PHASE2_MAX, buyer_cost=_cost(_listing())
    )


# ─────────────────────────────────────────────────────────────────────────
# Edit-surface builders — edit-alerts-on-state-change (v0.4.0)
# ─────────────────────────────────────────────────────────────────────────


def _phase1_edited_reserved() -> RenderedAlert:
    base = _phase1_with_cost()
    return apply_update_banner(base, update_banner_line("reserved"), base.inline_keyboard)


def _phase1_edited_price_drop() -> RenderedAlert:
    overrides: dict[str, Any] = {"price_eur": Decimal("48.00")}
    base = render_phase1_listing_alert(
        _snapshot(listing_overrides=overrides), buyer_cost=_cost(_listing(**overrides))
    )
    banner = update_banner_line(
        "price_drop",
        old_price_eur=Decimal("55.00"),
        new_price_eur=Decimal("48.00"),
    )
    return apply_update_banner(base, banner, base.inline_keyboard)


def _phase2_edited_reserved() -> RenderedAlert:
    base = _phase2_with_cost()
    keyboard = [phase2_dead_reserved_row(str(_FIXED_ALERT_ID))]
    return apply_update_banner(base, update_banner_line("reserved"), keyboard)


def _price_drop_ping() -> RenderedAlert:
    return render_price_drop_ping(
        _ENTRY_DISPLAY,
        old_price_eur=Decimal("55.00"),
        new_price_eur=Decimal("48.00"),
    )


# ─────────────────────────────────────────────────────────────────────────
# Offer-surface builders — wallapop-offer-flow
# ─────────────────────────────────────────────────────────────────────────

#: Negotiable fixture: 70 € asking against a 60 € target → 51 € fit
#: (largest whole euro with item + 3,50 shipping buffer + Protección ≤ 60,
#: and ≥ 70 % of asking). Values pinned as literals so a pricing change
#: breaks these fixtures loudly instead of silently re-deriving.
_NEGOTIABLE_ASKING = Decimal("70.00")
_OFFER_TARGET = Decimal("60.00")
_OFFER_FIT = Decimal("51")
#: Lower-target fixture on an under-ceiling listing: 55 € asking, 50 €
#: target → 42 € fit.
_UNDER_TARGET = Decimal("50.00")
_UNDER_FIT = Decimal("42")


def _negotiable_direct() -> RenderedAlert:
    overrides: dict[str, Any] = {"price_eur": _NEGOTIABLE_ASKING}
    return render_negotiable_listing_alert(
        _snapshot(phase="negotiable", listing_overrides=overrides),
        offer_eur=_OFFER_FIT,
        offer_target_total_eur=_OFFER_TARGET,
        buyer_cost=_cost(_listing(**overrides)),
    )


def _negotiable_missing_photo() -> RenderedAlert:
    overrides: dict[str, Any] = {"price_eur": _NEGOTIABLE_ASKING, "photo_urls": []}
    return render_negotiable_listing_alert(
        _snapshot(phase="negotiable", listing_overrides=overrides),
        offer_eur=_OFFER_FIT,
        offer_target_total_eur=_OFFER_TARGET,
        buyer_cost=_cost(_listing(**overrides)),
    )


def _phase1_with_offer() -> RenderedAlert:
    return render_phase1_listing_alert(
        _snapshot(),
        buyer_cost=_cost(_listing()),
        offer_eur=_UNDER_FIT,
        offer_target_total_eur=_UNDER_TARGET,
    )


def _phase2_with_offer() -> RenderedAlert:
    return render_phase2_listing_alert(
        _snapshot(phase="phase2"),
        _PHASE2_MAX,
        buyer_cost=_cost(_listing()),
        offer_eur=_UNDER_FIT,
        offer_target_total_eur=_UNDER_TARGET,
    )


def _offer_sent() -> RenderedAlert:
    return render_offer_sent(
        entry_display_name=_ENTRY_DISPLAY,
        offered_eur=_OFFER_FIT,
        audit_id=7,
        screenshot_path="https://placehold.co/600x400/png?text=Oferta+enviada",
        platform_remaining=9,
    )


_GENERIC_OFFER_FAILURE_CTX: Final[dict[str, Any]] = {
    "displayed_offer": _OFFER_FIT,
    "recomputed_offer": Decimal("49"),
    "offered": _OFFER_FIT,
    "limit_source": "propio",
    "consecutive_failures": 3,
    "threshold": 3,
    "missing": ["offer_button"],
    "error_class": "TinyFishUnavailable",
}


def _make_offer_failure(reason: OfferFailureReason) -> Callable[[], RenderedAlert]:
    def _build() -> RenderedAlert:
        return render_offer_failure(
            reason, entry_display_name=_ENTRY_DISPLAY, ctx=_GENERIC_OFFER_FAILURE_CTX
        )

    _build.__name__ = f"_offer_failure_{reason.value}"
    return _build


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 buy success + failure builders
# ─────────────────────────────────────────────────────────────────────────


def _buy_success() -> RenderedAlert:
    return render_phase2_buy_success(
        TransactionRecord(
            alert_id=_FIXED_ALERT_ID,
            price_paid_eur=Decimal("55.00"),
            payment_method="wallapop_pay",
            receipt_id="WP-2026-0001",
            # The buy_success renderer accepts any non-empty path; in
            # production this is the captured receipt screenshot on
            # local disk. For audit purposes we route Telegram at a
            # public placeholder so send_photo succeeds without
            # changing the renderer's contract.
            screenshot_path="https://placehold.co/600x400/png?text=Receipt+WP-2026-0001",
            total_seconds=42,
            committed_at=_FIXED_TS,
        ),
        entry_display_name=_ENTRY_DISPLAY,
        audit_id=42,
    )


_GENERIC_FAILURE_CTX: Final[dict[str, Any]] = {
    "api_price": Decimal("53.00"),
    "html_price": Decimal("0.53"),
    "tolerance_eur": Decimal("1.00"),
    "consecutive_failures": 3,
    "threshold": 3,
    "missing": ["buy_button"],
    "error_class": "TinyFishUnavailable",
    "transaction_id": 42,
    "receipt_id": "WP-2026-0001",
}


def _make_buy_failure(reason: BuyFailureReason) -> Callable[[], RenderedAlert]:
    def _build() -> RenderedAlert:
        return render_phase2_buy_failure(
            reason, entry_display_name=_ENTRY_DISPLAY, ctx=_GENERIC_FAILURE_CTX
        )

    _build.__name__ = f"_buy_failure_{reason.value}"
    return _build


# ─────────────────────────────────────────────────────────────────────────
# Operational EventName builders
# ─────────────────────────────────────────────────────────────────────────


_OPERATIONAL_FIXTURES: Final[dict[EventName, tuple[Severity, dict[str, Any]]]] = {
    EventName.daemon_started: ("info", {"version": "0.1.0", "jobs": "wallapop_poll, ebay_poll"}),
    EventName.daemon_stopped: ("info", {"reason": "SIGTERM"}),
    EventName.wallapop_session_expired: ("info", {}),
    EventName.wallapop_session_renewed: ("info", {}),
    EventName.wallapop_api_degraded: ("info", {"error_class": "WallapopApiError"}),
    EventName.wallapop_both_paths_down: (
        "warn",
        {"consecutive_failures": 3, "last_error_class": "TinyFishUnavailable"},
    ),
    EventName.tinyfish_fallback_active: ("info", {}),
    EventName.tinyfish_fallback_recovered: ("info", {}),
    EventName.ebay_token_refresh_failed: ("warn", {}),
    EventName.ebay_quota_breach: ("info", {"used": 5000, "budget": 5000}),
    EventName.llm_provider_rate_limited: ("info", {"provider": "gemini-flash"}),
    EventName.entry_snoozed: (
        "info",
        {"entry_display_name": _ENTRY_DISPLAY, "snooze_until": "2026-05-17T12:00:00Z"},
    ),
    EventName.poll_cycle_error: (
        "warn",
        {"error_class": "RuntimeError", "marketplace": "wallapop"},
    ),
    EventName.circuit_open: (
        "warn",
        {"consecutive_failures": 3, "threshold": 3, "last_affected_entry": _ENTRY_DISPLAY},
    ),
    EventName.smoke_test_failed: (
        "warn",
        {
            "fixture_name": "wallapop_html_comma_vs_dot",
            "parsed_price": "0.53",
            "expected_price": "53.00",
            "delta_eur": "52.47",
            "parser_error_class": "—",
        },
    ),
    EventName.smoke_test_recovered: ("info", {}),
    EventName.phase2_disabled: (
        "warn",
        {"reason": "receipt_mismatch", "last_affected_entry": _ENTRY_DISPLAY},
    ),
    EventName.phase2_re_enabled: ("info", {"entry": _ENTRY_DISPLAY}),
    EventName.phase2_buy_callback_received: (
        "info",
        {"entry": _ENTRY_DISPLAY, "alert_id": str(_FIXED_ALERT_ID)},
    ),
    EventName.phase2_screenshot_missing: (
        "warn",
        {"receipt_id": "WP-2026-0001", "listing_id": "abc123"},
    ),
    EventName.phase2_buy_completion_slow: (
        "info",
        {"entry": _ENTRY_DISPLAY, "elapsed_seconds": 87, "budget_seconds": 60},
    ),
    EventName.buy_orchestrator_error: (
        "warn",
        {"error_class": "TinyFishSessionLost", "alert_id": str(_FIXED_ALERT_ID)},
    ),
    EventName.offer_lockout_engaged: (
        "warn",
        {"consecutive_failures": 3, "threshold": 3, "last_affected_entry": _ENTRY_DISPLAY},
    ),
    EventName.offer_disabled: ("warn", {"reason": "kill_switch_global"}),
    EventName.offer_re_enabled: ("info", {"entry": _ENTRY_DISPLAY}),
    EventName.offer_orchestrator_error: (
        "warn",
        {"error_class": "TinyFishSessionLost", "alert_id": str(_FIXED_ALERT_ID)},
    ),
}


def _make_operational(event: EventName) -> Callable[[], RenderedAlert]:
    severity, ctx = _OPERATIONAL_FIXTURES[event]

    def _build() -> RenderedAlert:
        return render_operational_alert(severity, event, ctx)

    _build.__name__ = f"_operational_{event.value}"
    return _build


# ─────────────────────────────────────────────────────────────────────────
# Closed registry — the audit catalog
# ─────────────────────────────────────────────────────────────────────────


def _build_registry() -> dict[str, Callable[[], RenderedAlert]]:
    registry: dict[str, Callable[[], RenderedAlert]] = {
        "phase1_listing_direct": _phase1_direct,
        "phase1_listing_container": _phase1_container,
        "phase1_listing_missing_photo": _phase1_missing_photo,
        "phase2_listing_direct": _phase2_direct,
        "phase2_listing_container": _phase2_container,
        "phase2_listing_missing_photo": _phase2_missing_photo,
        "phase1_listing_with_cost": _phase1_with_cost,
        "phase1_listing_with_import": _phase1_with_import,
        "phase2_listing_with_cost": _phase2_with_cost,
        "phase1_listing_edited_reserved": _phase1_edited_reserved,
        "phase1_listing_edited_price_drop": _phase1_edited_price_drop,
        "phase2_listing_edited_reserved": _phase2_edited_reserved,
        "price_drop_ping": _price_drop_ping,
        "buy_success": _buy_success,
        "negotiable_listing_direct": _negotiable_direct,
        "negotiable_listing_missing_photo": _negotiable_missing_photo,
        "phase1_listing_with_offer": _phase1_with_offer,
        "phase2_listing_with_offer": _phase2_with_offer,
        "offer_sent": _offer_sent,
    }
    for reason in BuyFailureReason:
        registry[f"buy_failure_{reason.value}"] = _make_buy_failure(reason)
    for offer_reason in OfferFailureReason:
        registry[f"offer_failure_{offer_reason.value}"] = _make_offer_failure(offer_reason)
    for event in _OPERATIONAL_FIXTURES:
        registry[event.value] = _make_operational(event)
    return registry


#: The audit catalog — name → zero-arg :class:`RenderedAlert` builder.
VARIANT_REGISTRY: Final[dict[str, Callable[[], RenderedAlert]]] = _build_registry()


def build_rendered_variant(name: str) -> RenderedAlert:
    """Resolve a variant name to its rendered alert. Raises
    :class:`KeyError` if the name is not in the registry — callers
    (the CLI) should pre-check membership and surface a friendly error."""
    return VARIANT_REGISTRY[name]()


__all__ = ["VARIANT_REGISTRY", "build_rendered_variant"]
