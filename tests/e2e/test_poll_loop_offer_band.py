"""Negotiable-band poll-cycle tests (wallapop-offer-flow).

The over-ceiling alert gate's single carve-out: Wallapop listings on
offer-enabled entries with a buyer total inside ``ceiling x (1 +
band_pct)`` become negotiable alerts (💰 Ofertar, no Comprar); everything
else — over-band, offer-disabled, eBay — filters exactly as before, and
the confidence gate still applies inside the band.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from salvager.adapters.sqlite_store import MigrationRunner, SqliteStore, open_connection
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.domain.alert import BUTTON_LABELS, InlineButton, RenderedAlert
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, SearchQuery
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.telegram_surface import CallbackHandler, TelegramSurface
from salvager.orchestration.poll_loop import run_poll_cycle

_T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_BAND = Decimal("0.20")


def _entry(*, offer_enabled: bool = True) -> WishlistEntry:
    return WishlistEntry.model_validate(
        {
            "manufacturer": "Corsair",
            "model": "Vengeance LPX 16GB",
            "ref": "CMK16GX4M2D3000C16",
            "type": "ram",
            "keywords": ["corsair vengeance lpx 16gb"],
            "max_price_solo": Decimal("70.00"),
            "confidence_threshold": "medium",
            "offer": {"enabled": offer_enabled},
        }
    )


def _listing(
    listing_id: str,
    *,
    price_eur: Decimal,
    marketplace: str = "wallapop",
) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace=marketplace,  # type: ignore[arg-type]
        url=f"https://es.wallapop.com/item/{listing_id}",
        title="Corsair Vengeance LPX 16GB",
        description="ok",
        price_eur=price_eur,
        location="Madrid",
        photo_urls=["https://cdn/photo.jpg"],
        fetched_at=_T0,
    )


def _evaluation(listing_id: str, *, confidence: str = "high") -> ListingEvaluation:
    return ListingEvaluation(
        listing_id=listing_id,
        entry_key=("Corsair", "Vengeance LPX 16GB", "CMK16GX4M2D3000C16"),
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take="Match.",
        is_container=False,
        evaluated_at=_T0,
    )


class _FixtureFetcher(PageFetcher):
    def __init__(self, listings: list[Listing]) -> None:
        self._listings = listings

    async def search(self, query: SearchQuery) -> list[Listing]:
        return list(self._listings)

    async def fetch(self, listing_url: str) -> Listing:  # pragma: no cover
        raise AssertionError("not exercised")


class _ScriptedEvaluator(ListingEvaluator):
    def __init__(self, by_listing: dict[str, ListingEvaluation]) -> None:
        self._by = by_listing
        self.calls: list[str] = []

    async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
        self.calls.append(listing.listing_id)
        return self._by[listing.listing_id]


class _RecordingTelegram(TelegramSurface):
    def __init__(self) -> None:
        self.sends: list[RenderedAlert] = []
        self._next_message_id = 5000

    async def send(self, rendered: RenderedAlert, *, reply_to_message_id: int | None = None) -> int:
        self.sends.append(rendered)
        self._next_message_id += 1
        return self._next_message_id

    async def edit_alert(
        self, message_id: int, rendered: RenderedAlert, *, has_photo: bool
    ) -> None:
        return None

    async def edit_keyboard(
        self, message_id: int, keyboard: list[list[InlineButton]] | None
    ) -> None:  # pragma: no cover
        return None

    async def listen_callbacks(self, handler: CallbackHandler) -> None:  # pragma: no cover
        _ = handler


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    MigrationRunner().run(connection)
    connection.close()
    return SqliteStore(db_path)


async def _cycle(
    store: SqliteStore,
    listings: list[Listing],
    evals: dict[str, ListingEvaluation],
    *,
    entry: WishlistEntry | None = None,
    band: Decimal | None = _BAND,
) -> tuple[_RecordingTelegram, _ScriptedEvaluator]:
    telegram = _RecordingTelegram()
    evaluator = _ScriptedEvaluator(evals)
    await run_poll_cycle(
        "wallapop",
        wishlist=Wishlist(entries=[entry if entry is not None else _entry()]),
        fetcher=_FixtureFetcher(listings),
        evaluator=evaluator,
        store=store,
        telegram=telegram,
        offer_band_pct=band,
    )
    return telegram, evaluator


def _keyboard_labels(rendered: RenderedAlert) -> list[str]:
    assert rendered.inline_keyboard is not None
    return [button.text for row in rendered.inline_keyboard for button in row]


async def test_in_band_listing_dispatches_negotiable_alert(store: SqliteStore) -> None:
    # 70 € asking against a 70 € ceiling: buyer total ~79.44 € (est. shipping
    # + Protección) is over ceiling but under 84 € (ceiling x 1.2).
    listing = _listing("band1", price_eur=Decimal("70.00"))
    telegram, _ = await _cycle(store, [listing], {"band1": _evaluation("band1")})

    assert len(telegram.sends) == 1
    rendered = telegram.sends[0]
    assert rendered.text.startswith("💰")
    assert "Oferta:" in rendered.text
    labels = _keyboard_labels(rendered)
    assert BUTTON_LABELS["offer"] in labels
    assert BUTTON_LABELS["buy"] not in labels

    snapshot = await store.get_alert_snapshot(1)
    assert snapshot is not None
    assert snapshot.phase == "negotiable"


async def test_over_band_listing_stays_filtered(store: SqliteStore) -> None:
    listing = _listing("far1", price_eur=Decimal("85.00"))  # total ~95 € > 84 €
    telegram, evaluator = await _cycle(store, [listing], {})

    assert telegram.sends == []
    assert evaluator.calls == []  # dropped before the LLM eval, as before


async def test_offer_disabled_entry_filters_the_band(store: SqliteStore) -> None:
    listing = _listing("band2", price_eur=Decimal("70.00"))
    telegram, evaluator = await _cycle(store, [listing], {}, entry=_entry(offer_enabled=False))

    assert telegram.sends == []
    assert evaluator.calls == []


async def test_no_band_config_means_no_carve_out(store: SqliteStore) -> None:
    listing = _listing("band3", price_eur=Decimal("70.00"))
    telegram, evaluator = await _cycle(store, [listing], {}, band=None)

    assert telegram.sends == []
    assert evaluator.calls == []


async def test_confidence_gate_applies_inside_the_band(store: SqliteStore) -> None:
    listing = _listing("band4", price_eur=Decimal("70.00"))
    telegram, evaluator = await _cycle(
        store, [listing], {"band4": _evaluation("band4", confidence="low")}
    )

    assert evaluator.calls == ["band4"]  # evaluated like any candidate
    assert telegram.sends == []  # below the medium threshold


async def test_under_ceiling_alert_unchanged_without_target(store: SqliteStore) -> None:
    # Buyer total fits the ceiling and no offer.target_total_eur is set →
    # the ceiling-fit price lands at/above asking → no offer surface at all.
    listing = _listing("under1", price_eur=Decimal("55.00"))  # total ~63.31 €
    telegram, _ = await _cycle(store, [listing], {"under1": _evaluation("under1")})

    assert len(telegram.sends) == 1
    rendered = telegram.sends[0]
    assert rendered.text.startswith("📦")
    assert "Oferta:" not in rendered.text
    assert BUTTON_LABELS["offer"] not in _keyboard_labels(rendered)


async def test_lower_target_adds_offer_row_to_standard_alert(store: SqliteStore) -> None:
    entry = _entry()
    entry = entry.model_copy(
        update={"offer": entry.offer.model_copy(update={"target_total_eur": Decimal("60")})}
    )
    listing = _listing("under2", price_eur=Decimal("55.00"))  # total ~63.31 € ≤ 70 €
    telegram, _ = await _cycle(store, [listing], {"under2": _evaluation("under2")}, entry=entry)

    assert len(telegram.sends) == 1
    rendered = telegram.sends[0]
    assert rendered.text.startswith("📦")  # still a standard Phase 1 alert
    assert "Oferta:" in rendered.text
    assert BUTTON_LABELS["offer"] in _keyboard_labels(rendered)
