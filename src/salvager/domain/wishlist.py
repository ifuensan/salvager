"""Wishlist domain schema — FR1 / FR2 / FR4 / FR5.

This module is the single source of truth for the wishlist contract.
Every other package (`config/wishlist_yaml.py`, `domain/scope_guard.py`,
the orchestration poll loop, the alert renderers) consumes these models;
none of them re-derive field names, validation rules, or the entry-key
shape.

Why pydantic v2
---------------
v2's ``ConfigDict(extra="forbid")`` is the mechanism that rejects unknown
top-level fields at parse time. The scope-guard layer (Story 2.2) catches
forbidden-arbitrage fields *before* pydantic so the error is anchored to
the (c3) scope contract instead of looking like a generic typo; this
module's strict mode is the second line of defense.

FR5 nullability rules
---------------------
``max_price_in_device = None`` is the documented switch that disables
container detection for an entry (helper: :meth:`container_detection_enabled`).
At least one of ``max_price_solo`` or ``max_price_in_device`` must be set —
an entry with neither has no price ceiling and is rejected.
"""

from __future__ import annotations

import warnings
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

# ─────────────────────────────────────────────────────────────────────────
# Soft cap on wishlist size (FR3). Hard cap is not enforced — at ~100
# entries the operator is leaving the "personal monitoring tool" framing
# and should reconsider scope. The warning is a nudge, not a hard error.
# ─────────────────────────────────────────────────────────────────────────
SOFT_ENTRY_CAP = 100


PartType = Literal["hdd", "ram"]
ConfidenceThreshold = Literal["low", "medium", "high"]


class Phase2Settings(BaseModel):
    """Per-entry Phase 2 autonomous-purchase settings (FR26).

    ``enabled`` is toggled exclusively via ``salvager phase2 enable/
    disable`` — operators are documented (in the example wishlist) not to
    edit it by hand, because the CLI also writes audit-log entries.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_price_eur: Decimal | None = None


class OfferSettings(BaseModel):
    """Per-entry Wallapop offer settings (wallapop-offer-flow).

    ``enabled`` gates the whole offer surface for the entry (Ofertar button,
    negotiable-band alerts); toggled via ``salvager offer enable/disable``
    like its Phase 2 sibling. ``target_total_eur`` optionally aims offers at a
    delivered total BELOW the entry ceiling ("I'd accept 80 € but I want to
    pay 70 €"); ``None`` targets the effective ceiling (``max_price_solo``).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    target_total_eur: Decimal | None = None


class WishlistEntry(BaseModel):
    """One declared wish — what the operator is hunting and at what ceiling.

    The triplet ``(manufacturer, model, ref)`` is the entry key (FR4); it is
    the join column for ``alert_snapshots``, the dedup key for SQLite
    ``seen_listings``, and the slug shown on Telegram cards.
    """

    model_config = ConfigDict(extra="forbid")

    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    type: PartType
    max_price_solo: Decimal | None = None
    max_price_in_device: Decimal | None = None
    keywords: list[str] = Field(default_factory=list)
    container_keywords: list[str] = Field(default_factory=list)
    phase2: Phase2Settings = Field(default_factory=Phase2Settings)
    offer: OfferSettings = Field(default_factory=OfferSettings)
    confidence_threshold: ConfidenceThreshold

    @model_validator(mode="after")
    def _at_least_one_price_ceiling(self) -> WishlistEntry:
        """FR5: an entry with no price ceiling at all is rejected."""
        if self.max_price_solo is None and self.max_price_in_device is None:
            raise ValueError("at least one of max_price_solo or max_price_in_device must be set")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def entry_key(self) -> tuple[str, str, str]:
        """FR4 entry-key tuple: ``(manufacturer, model, ref)``."""
        return (self.manufacturer, self.model, self.ref)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        """Human-readable label used in Telegram cards and CLI tables."""
        return f"{self.manufacturer} {self.model} ({self.ref})"

    def container_detection_enabled(self) -> bool:
        """FR5: container detection is opted-in via a non-null in-device ceiling."""
        return self.max_price_in_device is not None


class Wishlist(BaseModel):
    """Top-level YAML wrapper — ``entries: list[WishlistEntry]``."""

    model_config = ConfigDict(extra="forbid")

    entries: list[WishlistEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _entry_keys_are_unique(self) -> Wishlist:
        """Duplicate entry keys would collide in the SQLite dedup index;
        catch them at parse time rather than at first-poll."""
        seen: dict[tuple[str, str, str], int] = {}
        for index, entry in enumerate(self.entries):
            key = entry.entry_key
            if key in seen:
                raise ValueError(
                    f"duplicate entry key {key!r}: entries[{seen[key]}] and entries[{index}]"
                )
            seen[key] = index
        return self

    @model_validator(mode="after")
    def _warn_on_soft_cap(self) -> Wishlist:
        """FR3 soft cap: > 100 entries is a nudge, not a hard error."""
        if len(self.entries) > SOFT_ENTRY_CAP:
            warnings.warn(
                f"wishlist has {len(self.entries)} entries; soft cap is {SOFT_ENTRY_CAP} "
                "(FR3). Consider whether all entries still match the personal-monitoring "
                "scope.",
                UserWarning,
                stacklevel=2,
            )
        return self
