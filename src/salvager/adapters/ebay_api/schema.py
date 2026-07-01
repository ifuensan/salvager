"""Pydantic projection of eBay's Browse API response — NFR-I4.

We model only the fields that map to :class:`Listing`. Extras are
tolerated (eBay adds fields over time); a missing required field is
schema drift and surfaces as :class:`EbaySchemaDrift` via the fetcher.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class EbayApiPrice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: Decimal
    currency: str


class EbayApiLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    city: str | None = None
    country: str | None = None


class EbayApiImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    imageUrl: str | None = None


class EbayApiSeller(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str | None = None
    feedbackScore: int | None = None


class EbayApiShippingOption(BaseModel):
    """One shipping option from a Browse item summary. ``shippingCost`` is
    absent for some options (e.g. local pickup); we read the cheapest priced
    one when projecting onto ``Listing.shipping_eur``."""

    model_config = ConfigDict(extra="ignore")

    shippingCost: EbayApiPrice | None = None


class EbayApiItem(BaseModel):
    """One result row from ``buy/browse/v1/item_summary/search``."""

    model_config = ConfigDict(extra="ignore")

    itemId: str
    title: str
    shortDescription: str = ""
    price: EbayApiPrice
    itemLocation: EbayApiLocation | None = None
    image: EbayApiImage | None = None
    itemWebUrl: str | None = None
    seller: EbayApiSeller | None = None
    itemCreationDate: str | None = None
    shippingOptions: list[EbayApiShippingOption] = Field(default_factory=list)


class EbayApiSearchResponse(BaseModel):
    """Top-level shape of the Browse API search response."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    itemSummaries: list[EbayApiItem] = Field(default_factory=list)
