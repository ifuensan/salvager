"""Edit-surface snapshot tests — release-audit re-capture for v0.4.x.

The live-updating alerts feature (v0.4.0, edit-alerts-on-state-change)
edits a dispatched alert in place: a replaceable status banner is
prepended to a freshly re-rendered body, and a big price drop
additionally sends a short NEW ping message. Five fixtures lock the
edit-surface shapes for the Story 5.17 release audit (ROADMAP
criterion 3), exactly like the listing/buy snapshot files lock theirs.

The rendered fixtures come straight from the ``dev emit-alert``
registry (:mod:`salvager.cli.dev_alert_fixtures`), so the variant the
operator emits to a real Telegram client during the audit and the
reference text dumped from these snapshots are byte-for-byte the same
MarkdownV2 string. The bodies carry the ``💶`` buyer-total row because
the production edit path (``orchestration/alert_updater.py``) always
re-renders with ``buyer_cost`` — and omit the comp row, which the
updater drops by design (an in-cycle signal, not current data).
"""

from __future__ import annotations

from syrupy.assertion import SnapshotAssertion

from salvager.cli.dev_alert_fixtures import build_rendered_variant
from salvager.domain.alert import apply_update_banner, update_banner_line

_ALERT_ID = "12345678-1234-1234-1234-123456789abc"


# ─────────────────────────────────────────────────────────────────────────
# Five fixtures
# ─────────────────────────────────────────────────────────────────────────


def test_snapshot_edited_reserved(snapshot: SnapshotAssertion) -> None:
    edited = build_rendered_variant("phase1_listing_edited_reserved")
    assert edited.text == snapshot
    assert edited.text.split("\n")[0] == "🔴 RESERVADO"


def test_snapshot_edited_available(snapshot: SnapshotAssertion) -> None:
    """The flip-back banner over the same re-rendered body. Not a registry
    variant of its own — it differs from ``edited_reserved`` only in the
    banner line, which this snapshot locks."""
    base = build_rendered_variant("phase1_listing_with_cost")
    edited = apply_update_banner(base, update_banner_line("available"), base.inline_keyboard)
    assert edited.text == snapshot
    assert edited.text.split("\n")[0] == "🟢 Disponible de nuevo"


def test_snapshot_edited_price_drop(snapshot: SnapshotAssertion) -> None:
    """The body reflects the NEW price everywhere (headline + 💶 row); the
    banner alone carries the price the operator last saw."""
    edited = build_rendered_variant("phase1_listing_edited_price_drop")
    assert edited.text == snapshot
    assert edited.text.split("\n")[0].startswith("📉 ")
    assert "antes" in edited.text


def test_snapshot_edited_reserved_phase2(snapshot: SnapshotAssertion) -> None:
    """A reserved Phase 2 listing swaps ✅ Comprar for the non-tappable
    🔴 Reservado badge (the ``noop`` verb) while keeping 👁 Ver."""
    edited = build_rendered_variant("phase2_listing_edited_reserved")
    assert edited.text == snapshot
    assert edited.inline_keyboard is not None
    assert [b.text for b in edited.inline_keyboard[0]] == ["🔴 Reservado", "👁 Ver"]
    assert edited.inline_keyboard[0][0].callback_data == f"listing:noop:{_ALERT_ID}"


def test_snapshot_price_drop_ping(snapshot: SnapshotAssertion) -> None:
    """The standalone ping is plain text — no photo, no keyboard."""
    rendered = build_rendered_variant("price_drop_ping")
    assert rendered.text == snapshot
    assert rendered.photo_url is None
    assert rendered.inline_keyboard is None
