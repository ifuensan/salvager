"""Tests for Phase 1 domain models — Story 3.1."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from hardware_hunter.domain.alert import (
    AlertSnapshot,
    InlineButton,
    RenderedAlert,
)
from hardware_hunter.domain.audit import (
    AlertSnapshotAudit,
    AuditEntry,
    CallbackAudit,
    Phase2GuardrailTripped,
    TapEventAudit,
    TransactionAudit,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing, SearchQuery

# ─────────────────────────────────────────────────────────────────────────
# Listing + SearchQuery
# ─────────────────────────────────────────────────────────────────────────


def _valid_listing(**overrides: object) -> Listing:
    base: dict[str, object] = {
        "listing_id": "abc123",
        "marketplace": "wallapop",
        "url": "https://wallapop.com/item/abc123",
        "title": "WD Red Plus 4TB",
        "description": "Used, like new",
        "price_eur": Decimal("55.00"),
        "fetched_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def test_listing_accepts_minimum_valid_payload() -> None:
    listing = _valid_listing()
    assert listing.marketplace == "wallapop"
    assert listing.price_eur == Decimal("55.00")
    assert listing.entry_key_match is None  # default until evaluated
    assert listing.photo_urls == []


def test_listing_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _valid_listing(arbitrage_score=0.85)


def test_listing_marketplace_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        _valid_listing(marketplace="milanuncios")


def test_listing_entry_key_match_accepts_tuple() -> None:
    listing = _valid_listing(entry_key_match=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"))
    assert listing.entry_key_match == (
        "Western Digital",
        "WD Red Plus 4TB",
        "WD40EFPX",
    )


def test_search_query_requires_keywords() -> None:
    with pytest.raises(ValidationError):
        SearchQuery(keywords=[], marketplace="wallapop")


def test_search_query_accepts_max_price() -> None:
    q = SearchQuery(
        keywords=["WD Red Plus 4TB"],
        marketplace="ebay",
        max_price_eur=Decimal("90.00"),
    )
    assert q.max_price_eur == Decimal("90.00")


# ─────────────────────────────────────────────────────────────────────────
# ListingEvaluation
# ─────────────────────────────────────────────────────────────────────────


def _valid_evaluation(**overrides: object) -> ListingEvaluation:
    base: dict[str, object] = {
        "listing_id": "abc123",
        "entry_key": ("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        "confidence": "high",
        "one_line_take": "WD Red Plus 4TB at €55 — strong match.",
        "is_container": False,
        "evaluated_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return ListingEvaluation(**base)  # type: ignore[arg-type]


def test_listing_evaluation_defaults() -> None:
    evaluation = _valid_evaluation()
    assert evaluation.cache_hit is False
    assert evaluation.wrapper_text is None
    assert evaluation.extracted_text is None


def test_listing_evaluation_container_with_wrapper_text() -> None:
    evaluation = _valid_evaluation(
        is_container=True,
        wrapper_text="Synology DS220+ NAS *including* 2x WD Red Plus 4TB",
    )
    assert evaluation.is_container is True
    assert "Synology" in (evaluation.wrapper_text or "")


def test_listing_evaluation_confidence_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        _valid_evaluation(confidence="very-high")


# ─────────────────────────────────────────────────────────────────────────
# InlineButton + RenderedAlert
# ─────────────────────────────────────────────────────────────────────────


def test_inline_button_accepts_locked_callback_format() -> None:
    button = InlineButton(text="👁 Ver", callback_data="phase1:view:abc123")
    assert button.callback_data == "phase1:view:abc123"


@pytest.mark.parametrize(
    "bad",
    [
        "phase1:view",  # missing id
        "PHASE1:view:abc",  # uppercase surface
        "phase1:View:abc",  # uppercase verb
        "phase1:view:has space",  # space in id
        "x" * 100,  # exceeds 64-byte cap
    ],
)
def test_inline_button_rejects_malformed_callback(bad: str) -> None:
    with pytest.raises(ValidationError):
        InlineButton(text="x", callback_data=bad)


def test_rendered_alert_defaults_to_markdownv2() -> None:
    rendered = RenderedAlert(text="hello world")
    assert rendered.parse_mode == "MarkdownV2"
    assert rendered.photo_url is None
    assert rendered.inline_keyboard is None


def test_rendered_alert_with_keyboard() -> None:
    rendered = RenderedAlert(
        text="Listing matched",
        photo_url="https://cdn/photo.jpg",
        inline_keyboard=[
            [
                InlineButton(text="👁 Ver", callback_data="phase1:view:abc"),
                InlineButton(text="🙅 Saltar", callback_data="phase1:skip:abc"),
            ]
        ],
    )
    assert rendered.inline_keyboard is not None
    assert len(rendered.inline_keyboard[0]) == 2


# ─────────────────────────────────────────────────────────────────────────
# AlertSnapshot — composite
# ─────────────────────────────────────────────────────────────────────────


def test_alert_snapshot_composes_listing_and_evaluation() -> None:
    snapshot = AlertSnapshot(
        alert_id=uuid4(),
        entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        entry_display_name="Western Digital WD Red Plus 4TB (WD40EFPX)",
        listing=_valid_listing(),
        evaluation=_valid_evaluation(),
        phase="phase1",
        rendered_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    )
    assert snapshot.phase == "phase1"
    assert snapshot.phase2_max_price_eur is None
    assert snapshot.listing.marketplace == "wallapop"


# ─────────────────────────────────────────────────────────────────────────
# Audit — Phase 1 variants work, Phase 2 variants raise
# ─────────────────────────────────────────────────────────────────────────


def test_alert_snapshot_audit_constructs_cleanly() -> None:
    audit = AlertSnapshotAudit(
        audit_id=uuid4(),
        alert_id=uuid4(),
        entry_key=("a", "b", "c"),
        listing_id="abc123",
        marketplace="wallapop",
        phase="phase1",
        telegram_message_id=42,
        occurred_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    )
    assert audit.kind == "alert_snapshot"


def test_callback_audit_constructs_cleanly() -> None:
    audit = CallbackAudit(
        audit_id=uuid4(),
        alert_id=uuid4(),
        telegram_message_id=42,
        callback_data="phase1:view:abc",
        verb="view",
        chat_id=12345,
        occurred_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    )
    assert audit.verb == "view"


def test_tap_event_audit_construction_raises_phase2_guardrail() -> None:
    """AR24: Phase 2 audit types refuse construction at v0.x."""
    with pytest.raises(Phase2GuardrailTripped):
        TapEventAudit(
            audit_id=uuid4(),
            alert_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )


def test_transaction_audit_construction_raises_phase2_guardrail() -> None:
    with pytest.raises(Phase2GuardrailTripped):
        TransactionAudit(
            audit_id=uuid4(),
            alert_id=uuid4(),
            price_paid_eur=Decimal("55.00"),
            succeeded=True,
            occurred_at=datetime.now(UTC),
        )


def test_audit_entry_discriminated_union_phase1_roundtrip() -> None:
    """Pydantic resolves the discriminator on ``kind`` without firing
    the Phase 2 guardrail, since the union picks the variant from the
    literal field before validation runs."""
    adapter: TypeAdapter[AuditEntry] = TypeAdapter(AuditEntry)
    payload = {
        "kind": "alert_snapshot",
        "audit_id": str(uuid4()),
        "alert_id": str(uuid4()),
        "entry_key": ["a", "b", "c"],
        "listing_id": "abc123",
        "marketplace": "wallapop",
        "phase": "phase1",
        "telegram_message_id": 42,
        "occurred_at": "2026-05-12T12:00:00Z",
    }
    parsed = adapter.validate_python(payload)
    assert isinstance(parsed, AlertSnapshotAudit)


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline — domain/ stays pure
# ─────────────────────────────────────────────────────────────────────────


_ALLOWED_DOMAIN_TOP_LEVELS = frozenset(
    {
        # stdlib
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "re",
        "typing",
        "uuid",
        "warnings",
        # blessed third-party
        "pydantic",
        # in-package
        "hardware_hunter",
    }
)


@pytest.mark.parametrize(
    "module_path",
    [
        "src/hardware_hunter/domain/alert.py",
        "src/hardware_hunter/domain/audit.py",
        "src/hardware_hunter/domain/evaluation.py",
        "src/hardware_hunter/domain/listing.py",
        "src/hardware_hunter/domain/scope_guard.py",
        "src/hardware_hunter/domain/wishlist.py",
    ],
)
def test_domain_module_imports_only_whitelisted_packages(module_path: str) -> None:
    """Story 3.1 AC: no file in domain/ imports anything outside stdlib +
    pydantic + the in-package types. This is the AST-level check that
    runs alongside the deny-list adapter-discipline lint."""
    repo_root = Path(__file__).resolve().parents[2]
    tree = ast.parse((repo_root / module_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in _ALLOWED_DOMAIN_TOP_LEVELS, (
                    f"{module_path}: forbidden import {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative
                continue
            top = (node.module or "").split(".")[0]
            assert top in _ALLOWED_DOMAIN_TOP_LEVELS, (
                f"{module_path}: forbidden 'from {node.module} import …'"
            )
