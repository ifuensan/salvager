"""Pydantic schema for the Wallapop unofficial-API response — NFR-I4.

The shapes here are the contract we expect from
``api.wallapop.com/api/v3/search/section`` (the
``organic_search_results`` section).

Migration note (2026-05-18)
---------------------------
Wallapop deprecated ``/api/v3/general/search`` (which had
``{search_objects: [...]}`` at the top) at some point before
2026-05-18. The current SPA uses ``/api/v3/search/section`` and
returns ``{data: {section: {items: [...]}}}``. Per-item field names
also shifted (``images[].original/medium/small`` →
``images[].urls.{small,medium,big}``, ``user.id`` → flat
``user_id``, ``publish_date`` ISO string → ``created_at`` /
``modified_at`` unix-millis). This module reflects the new shape.

Required fields are strict — a missing one trips
:class:`WallapopSchemaDrift` via pydantic's ``ValidationError`` (the
fetcher wraps it). Extras are tolerated (Wallapop adds fields over
time; we read what we need and ignore the rest).

Only the fields that map to ``domain.listing.Listing`` are declared
beyond the structural wrappers; this is intentionally a *projection*
of the upstream response, not a mirror.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class WallapopApiPrice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount: Decimal
    currency: str


class WallapopApiLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    city: str | None = None
    country_code: str | None = None


class WallapopApiImageUrls(BaseModel):
    """The ``images[].urls`` nested object in the v3 search response."""

    model_config = ConfigDict(extra="ignore")

    small: str | None = None
    medium: str | None = None
    big: str | None = None


class WallapopApiImage(BaseModel):
    """One image entry. The v3 response nests sizes under ``urls``;
    earlier versions had them flat. Kept ``urls`` as the only required
    field so future schema drift on this nested object surfaces clearly.
    """

    model_config = ConfigDict(extra="ignore")

    urls: WallapopApiImageUrls = Field(default_factory=WallapopApiImageUrls)


class WallapopApiReserved(BaseModel):
    """Wallapop wraps the reserved-flag in a tiny object: ``{"flag": bool}``.

    Mapping into ``Listing.is_reserved`` happens in the fetcher; here
    we just parse the envelope so the boolean is reachable. A missing
    ``reserved`` object on the upstream item is normal (older listings,
    forward-compat) and treated as not-reserved.
    """

    model_config = ConfigDict(extra="ignore")

    flag: bool = False


class WallapopApiItem(BaseModel):
    """One result row from the v3 search-section endpoint.

    ``user_id`` replaces the old ``user.{id,items_count}`` nested
    object. ``items_count`` (seller_history_count in domain) is no
    longer exposed by this endpoint; the domain field is populated
    from a separate ``/api/v3/users/{id}`` call by callers that need
    it. ``web_slug`` is the human-readable URL slug Wallapop uses
    (already contains the numeric listing id); the canonical item URL
    is ``https://es.wallapop.com/item/{web_slug}``.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    user_id: str | None = None
    title: str
    description: str = ""
    price: WallapopApiPrice
    location: WallapopApiLocation | None = None
    images: list[WallapopApiImage] = Field(default_factory=list)
    reserved: WallapopApiReserved | None = None
    web_slug: str | None = None
    #: Unix milliseconds. ``None`` is tolerated for forward-compat,
    #: but the live API always emits it.
    created_at: int | None = None
    modified_at: int | None = None

    def preferred_photo_url(self) -> str | None:
        """Pick the highest-quality image URL we have, falling back gracefully."""
        for image in self.images:
            if image.urls.big:
                return image.urls.big
            if image.urls.medium:
                return image.urls.medium
            if image.urls.small:
                return image.urls.small
        return None


class WallapopApiSearchSection(BaseModel):
    """The ``data.section`` wrapper in the v3 response."""

    model_config = ConfigDict(extra="ignore")

    items: list[WallapopApiItem] = Field(default_factory=list)


class WallapopApiSearchData(BaseModel):
    """The ``data`` wrapper in the v3 response."""

    model_config = ConfigDict(extra="ignore")

    section: WallapopApiSearchSection = Field(default_factory=WallapopApiSearchSection)


class WallapopApiSearchResponse(BaseModel):
    """Top-level shape of the v3 ``/api/v3/search/section`` response."""

    model_config = ConfigDict(extra="ignore")

    data: WallapopApiSearchData = Field(default_factory=WallapopApiSearchData)

    @property
    def items(self) -> list[WallapopApiItem]:
        """Flat accessor for the items list — hides the
        ``data.section`` wrapper from callers that don't care about
        the envelope shape."""
        return self.data.section.items
