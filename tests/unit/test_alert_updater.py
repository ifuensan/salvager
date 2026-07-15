"""Unit tests for the watch-diff + edit-render machinery
(edit-alerts-on-state-change): pure change detection, keyboard
reconstruction, banner rendering, and the caption budget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from salvager.domain.alert import (
    AlertSnapshot,
    apply_update_banner,
    render_phase1_listing_alert,
    render_phase2_listing_alert,
    render_price_drop_ping,
    update_banner_line,
)
from salvager.domain.alert_watch import AlertWatch
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.orchestration.alert_updater import (
    AlertUpdatePolicy,
    detect_change,
    reconstruct_keyboard,
)

_T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
_POLICY = AlertUpdatePolicy()  # defaults: 1 % / 0,50 € edit, 10 % ping


def _listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": "lst-1",
        "marketplace": "wallapop",
        "url": "https://es.wallapop.com/item/lst-1",
        "title": "WD Red Plus 4TB",
        "description": "ok",
        "price_eur": Decimal("100.00"),
        "photo_urls": ["https://cdn/photo.jpg"],
        "fetched_at": _T0,
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def _watch(**overrides: object) -> AlertWatch:
    base: dict[str, object] = {
        "alert_id": uuid4(),
        "listing_id": "lst-1",
        "entry_key": ("WD", "Red Plus 4TB", "WD40EFPX"),
        "telegram_message_id": 4711,
        "last_price_eur": Decimal("100.00"),
        "last_is_reserved": False,
        "watch_until": _T0 + timedelta(days=7),
    }
    base.update(overrides)
    return AlertWatch(**base)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> AlertSnapshot:
    base: dict[str, object] = {
        "alert_id": uuid4(),
        "entry_key": ("WD", "Red Plus 4TB", "WD40EFPX"),
        "entry_display_name": "WD Red Plus 4TB (WD40EFPX)",
        "listing": _listing(),
        "evaluation": ListingEvaluation(
            listing_id="lst-1",
            entry_key=("WD", "Red Plus 4TB", "WD40EFPX"),
            confidence="high",
            one_line_take="Strong match.",
            is_container=False,
            evaluated_at=_T0,
        ),
        "phase": "phase1",
        "rendered_at": _T0,
    }
    base.update(overrides)
    return AlertSnapshot(**base)  # type: ignore[arg-type]


# ── detect_change ─────────────────────────────────────────────────────────


def test_reserved_flip_detected() -> None:
    diff = detect_change(_watch(), _listing(is_reserved=True), _POLICY)
    assert diff.change == "reserved"


def test_flip_back_detected_as_available() -> None:
    diff = detect_change(_watch(last_is_reserved=True), _listing(is_reserved=False), _POLICY)
    assert diff.change == "available"


def test_reserved_flip_takes_precedence_over_price_drop() -> None:
    diff = detect_change(_watch(), _listing(is_reserved=True, price_eur=Decimal("50.00")), _POLICY)
    assert diff.change == "reserved"


def test_threshold_drop_edits_without_ping() -> None:
    # 5 € on 100 € = 5 %: above 1 %/0,50 € edit floor, below 10 % ping.
    diff = detect_change(_watch(), _listing(price_eur=Decimal("95.00")), _POLICY)
    assert diff.change == "price_drop"
    assert diff.ping is False


def test_big_drop_edits_and_pings() -> None:
    diff = detect_change(_watch(), _listing(price_eur=Decimal("85.00")), _POLICY)
    assert diff.change == "price_drop"
    assert diff.ping is True


def test_sub_threshold_pct_advances_silently() -> None:
    # 0,90 € on 100 € = 0.9 % < 1 % → no edit, silent advance.
    diff = detect_change(_watch(), _listing(price_eur=Decimal("99.10")), _POLICY)
    assert diff.change is None
    assert diff.advance_silently is True


def test_sub_threshold_absolute_floor_advances_silently() -> None:
    # 0,40 € on 10 € = 4 % ≥ 1 % BUT below the 0,50 € absolute floor.
    watch = _watch(last_price_eur=Decimal("10.00"))
    diff = detect_change(watch, _listing(price_eur=Decimal("9.60")), _POLICY)
    assert diff.change is None
    assert diff.advance_silently is True


def test_price_increase_never_edits_but_advances() -> None:
    diff = detect_change(_watch(), _listing(price_eur=Decimal("110.00")), _POLICY)
    assert diff.change is None
    assert diff.advance_silently is True


def test_unchanged_listing_is_a_full_no_op() -> None:
    diff = detect_change(_watch(), _listing(), _POLICY)
    assert diff.change is None
    assert diff.advance_silently is False


# ── reconstruct_keyboard ──────────────────────────────────────────────────


def test_untapped_phase1_keeps_original_row() -> None:
    keyboard = reconstruct_keyboard(_snapshot(), None, now_reserved=False)
    assert keyboard is not None
    labels = [b.text for b in keyboard[0]]
    assert any("Ver" in text for text in labels)


def test_acked_alert_keeps_ack_row() -> None:
    keyboard = reconstruct_keyboard(_snapshot(), "view", now_reserved=True)
    assert keyboard is not None
    assert "visto" in keyboard[0][0].text


def test_phase2_reserved_gets_dead_badge() -> None:
    snapshot = _snapshot(phase="phase2", phase2_max_price_eur=Decimal("60.00"))
    keyboard = reconstruct_keyboard(snapshot, None, now_reserved=True)
    assert keyboard is not None
    assert keyboard[0][0].text == "🔴 Reservado"
    assert keyboard[0][0].callback_data.startswith("listing:noop:")
    assert not any("Comprar" in b.text for b in keyboard[0])


def test_phase2_flip_back_restores_comprar_row() -> None:
    snapshot = _snapshot(phase="phase2", phase2_max_price_eur=Decimal("60.00"))
    keyboard = reconstruct_keyboard(snapshot, None, now_reserved=False)
    assert keyboard is not None
    assert any("Comprar" in b.text for b in keyboard[0])


# ── banners + ping renderer ───────────────────────────────────────────────


def test_reserved_banner_text() -> None:
    assert update_banner_line("reserved") == "🔴 RESERVADO"


def test_available_banner_text() -> None:
    assert update_banner_line("available") == "🟢 Disponible de nuevo"


def test_price_drop_banner_carries_both_prices() -> None:
    banner = update_banner_line(
        "price_drop",
        old_price_eur=Decimal("95.00"),
        new_price_eur=Decimal("80.00"),
    )
    assert "80,00" in banner
    assert "95,00" in banner
    assert "antes" in banner


def test_unknown_change_kind_rejected() -> None:
    with pytest.raises(ValueError):
        update_banner_line("sold")


def test_banner_is_prepended_and_replaced_not_stacked() -> None:
    base = render_phase1_listing_alert(_snapshot())
    first = apply_update_banner(base, update_banner_line("reserved"), keyboard=None)
    assert first.text.splitlines()[0] == "🔴 RESERVADO"
    # A second update re-renders from the BASE, so banners never stack.
    second = apply_update_banner(base, update_banner_line("available"), keyboard=None)
    assert second.text.splitlines()[0] == "🟢 Disponible de nuevo"
    assert "RESERVADO" not in second.text


def test_edited_phase2_body_fits_the_caption_cap() -> None:
    """Photo captions cap at 1024 chars; the edit variant (banner + longest
    body shape: Phase 2 + container rows + max-length take) must budget it."""
    snapshot = _snapshot(
        phase="phase2",
        phase2_max_price_eur=Decimal("60.00"),
        evaluation=ListingEvaluation(
            listing_id="lst-1",
            entry_key=("WD", "Red Plus 4TB", "WD40EFPX"),
            confidence="medium",
            one_line_take="x" * 120,
            is_container=True,
            wrapper_text="NAS Synology DS920+ completo con discos y accesorios varios",
            extracted_text="WD Red Plus 4TB WD40EFPX en el interior del NAS anunciado",
            evaluated_at=_T0,
        ),
    )
    base = render_phase2_listing_alert(snapshot, Decimal("60.00"))
    banner = update_banner_line(
        "price_drop",
        old_price_eur=Decimal("1095.99"),
        new_price_eur=Decimal("1080.49"),
    )
    edited = apply_update_banner(base, banner, keyboard=None)
    assert len(edited.text) <= 1024


def test_price_drop_ping_is_plain_text_reply_material() -> None:
    ping = render_price_drop_ping(
        "WD Red Plus 4TB (WD40EFPX)",
        old_price_eur=Decimal("95.00"),
        new_price_eur=Decimal("80.00"),
    )
    assert ping.photo_url is None
    assert ping.inline_keyboard is None
    assert "Bajada" in ping.text
    assert "80,00" in ping.text and "95,00" in ping.text
