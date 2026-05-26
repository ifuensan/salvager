"""Tests for the wishlist loader/saver — Story 2.3 (AR12 round-trip)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from salvager.config.wishlist_yaml import (
    WishlistParseError,
    WishlistScopeError,
    WishlistValidationError,
    load_wishlist,
    save_wishlist,
)

# Lives next to this test file — same content as wishlist.example.yaml
# is the realistic round-trip target.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_WISHLIST = REPO_ROOT / "wishlist.example.yaml"


@pytest.fixture
def example_path(tmp_path: Path) -> Path:
    """Copy of wishlist.example.yaml the test can mutate freely."""
    dest = tmp_path / "wishlist.yaml"
    dest.write_text(EXAMPLE_WISHLIST.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


# ─────────────────────────────────────────────────────────────────────────
# Round-trip preservation — the headline AR12 contract
# ─────────────────────────────────────────────────────────────────────────


def test_load_then_save_is_byte_identical(example_path: Path) -> None:
    """Loading wishlist.example.yaml and immediately saving — no mutation —
    must produce a byte-identical file. This is the AR12 round-trip contract."""
    original = example_path.read_bytes()
    wishlist = load_wishlist(example_path)
    save_wishlist(example_path, wishlist)
    assert example_path.read_bytes() == original


def test_loaded_wishlist_has_expected_typed_shape(example_path: Path) -> None:
    wishlist = load_wishlist(example_path)
    assert len(wishlist.entries) == 4
    first = wishlist.entries[0]
    assert first.manufacturer == "Western Digital"
    assert first.model == "WD Red Plus 4TB"
    assert first.ref == "WD40EFPX"
    assert first.type == "hdd"
    assert first.max_price_solo == Decimal("60.00")
    assert first.confidence_threshold == "high"
    assert first.phase2.enabled is False


# ─────────────────────────────────────────────────────────────────────────
# Mutation preserves comments + quoting
# ─────────────────────────────────────────────────────────────────────────


def test_save_after_phase2_toggle_preserves_comments(example_path: Path) -> None:
    """The key AR12 scenario: `phase2 enable` flips one boolean and the
    surrounding (c3)-scope comment block must survive intact."""
    wishlist = load_wishlist(example_path)
    wishlist.entries[0].phase2.enabled = True

    save_wishlist(example_path, wishlist)
    rewritten = example_path.read_text(encoding="utf-8")

    # The original header block and the per-entry comments are still present.
    assert "(c3) scope contract" in rewritten
    assert "Example 1: 4 TB NAS-grade HDD with container detection" in rewritten
    assert "Entry schema (per FR1/FR2/FR4/FR5)" in rewritten


def test_save_only_changes_targeted_line(example_path: Path) -> None:
    """Exactly the `enabled: false` → `enabled: true` cell flips for entry 0.
    No other diff appears in the rewritten file."""
    original = example_path.read_text(encoding="utf-8").splitlines()
    wishlist = load_wishlist(example_path)
    wishlist.entries[0].phase2.enabled = True

    save_wishlist(example_path, wishlist)
    rewritten = example_path.read_text(encoding="utf-8").splitlines()

    assert len(original) == len(rewritten)
    diffs = [
        (i, orig, new)
        for i, (orig, new) in enumerate(zip(original, rewritten, strict=True))
        if orig != new
    ]
    assert len(diffs) == 1, f"expected exactly one differing line; got {diffs!r}"
    _, orig_line, new_line = diffs[0]
    assert "enabled: false" in orig_line
    assert "enabled: true" in new_line


def test_save_reflects_typed_mutation_on_subsequent_load(example_path: Path) -> None:
    """The mutation we wrote should re-parse to the same typed value."""
    wishlist = load_wishlist(example_path)
    wishlist.entries[0].phase2.enabled = True
    save_wishlist(example_path, wishlist)

    re_loaded = load_wishlist(example_path)
    assert re_loaded.entries[0].phase2.enabled is True
    assert re_loaded.entries[1].phase2.enabled is False  # untouched


# ─────────────────────────────────────────────────────────────────────────
# Scope-guard runs BEFORE pydantic — error anchors to (c3)
# ─────────────────────────────────────────────────────────────────────────


def test_forbidden_field_raises_scope_error_not_validation_error(tmp_path: Path) -> None:
    """If a wishlist has both a forbidden field AND an invalid pydantic
    field, the scope error wins (higher priority — operator sees the (c3)
    pointer first, not a pydantic noise wall)."""
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red
    ref: WD40EFPX
    type: gpu                       # invalid: not in {hdd, ram}
    expected_resale_value: 80.00    # forbidden field
""",
        encoding="utf-8",
    )

    with pytest.raises(WishlistScopeError) as excinfo:
        load_wishlist(bad)
    assert "expected_resale_value" in str(excinfo.value)
    assert excinfo.value.violations[0].line_number == 6


def test_pydantic_field_error_is_wrapped(tmp_path: Path) -> None:
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red
    ref: WD40EFPX
    type: gpu
    max_price_solo: 60.00
    confidence_threshold: high
""",
        encoding="utf-8",
    )

    with pytest.raises(WishlistValidationError) as excinfo:
        load_wishlist(bad)
    err = excinfo.value
    assert err.path == bad
    assert err.errors, "validation errors list should be non-empty"
    first = err.errors[0]
    assert first["loc_str"].startswith("entries[0].type")
    # Line number resolves to the `type:` row in the YAML above.
    assert first["line_number"] == 5


# ─────────────────────────────────────────────────────────────────────────
# Parse errors
# ─────────────────────────────────────────────────────────────────────────


def test_malformed_yaml_raises_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "wishlist.yaml"
    bad.write_text(
        """\
entries:
  - manufacturer: WD
    model: Red
   bad_indent: oops
""",
        encoding="utf-8",
    )
    with pytest.raises(WishlistParseError) as excinfo:
        load_wishlist(bad)
    err = excinfo.value
    assert err.path == bad
    assert err.line > 0
    assert err.column > 0


# ─────────────────────────────────────────────────────────────────────────
# Synthesized wishlist (no preserved doc) — save still works
# ─────────────────────────────────────────────────────────────────────────


def test_save_synthesized_wishlist_serializes_from_scratch(tmp_path: Path) -> None:
    """A Wishlist constructed in Python (not via load_wishlist) has no
    preserved doc; save_wishlist falls back to a fresh serialize."""
    from salvager.domain.wishlist import Wishlist, WishlistEntry

    wishlist = Wishlist(
        entries=[
            WishlistEntry(
                manufacturer="Crucial",
                model="DDR4-3200",
                ref="CT16G4DFRA32A",
                type="ram",
                max_price_solo=Decimal("25.00"),
                max_price_in_device=Decimal("60.00"),
                keywords=["Crucial 16GB"],
                container_keywords=[],
                confidence_threshold="high",
            )
        ]
    )

    out = tmp_path / "wishlist.yaml"
    save_wishlist(out, wishlist)

    written = out.read_text(encoding="utf-8")
    assert "Crucial" in written
    assert "CT16G4DFRA32A" in written

    # Round-tripping the freshly written file should re-parse equally.
    re_loaded = load_wishlist(out)
    assert re_loaded.entries[0].ref == "CT16G4DFRA32A"
