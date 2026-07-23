"""Alert + render-output schema — Story 3.1.

Three types live here:

  - :class:`AlertSnapshot` — the immutable record of what was alerted
    (entry x listing x evaluation). Persisted to SQLite ``alert_snapshots``
    so the callback handler can look up the originating context when
    an operator taps a button hours later.
  - :class:`RenderedAlert` — the data shape every renderer produces
    and the :class:`TelegramSurface` adapter consumes.
  - :class:`InlineButton` — one button on a Telegram inline keyboard
    row. ``callback_data`` follows the locked
    ``<surface>:<verb>:<id>`` format with the 64-byte Telegram cap.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from salvager.domain.comps import CompSummary
from salvager.domain.errors import BuyFailureReason, OfferFailureReason
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.phase2_audit import TransactionRecord
from salvager.domain.pricing import BuyerCost

Phase = Literal["phase1", "phase2", "negotiable"]
ParseMode = Literal["MarkdownV2"]

# Telegram caps inline-button callback_data at 64 bytes. The locked format
# is `<surface>:<verb>:<id>` per CALLBACK_DATA_FORMAT in the UX spec.
_CALLBACK_DATA_MAX_BYTES = 64
_CALLBACK_DATA_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+:[A-Za-z0-9_\-]+$")

# Operator-facing prose fragments reused across many alert templates.
# Centralising them keeps the wording consistent — a copy-edit in one
# place propagates everywhere instead of drifting per-template.
_NEXT_STEP_HEADER: Final[str] = "Próximo paso:"
_CMD_PHASE2_ENABLE: Final[str] = "salvager phase2 enable <entry>"
_CMD_AUDIT_SHOW_LAST5: Final[str] = "salvager audit show --last 5"
_STATUS_PHASE2_GLOBALLY_DISABLED: Final[str] = "Estado actual: Fase 2 desactivada globalmente"

# ─────────────────────────────────────────────────────────────────────────
# Locked UX tokens (UX-DR3 / UX-DR4 / UX-DR5)
# ─────────────────────────────────────────────────────────────────────────

#: Per-surface severity emoji. PRD amendment to grow.
SEVERITY_TOKENS: Final[dict[str, str]] = {
    "operational_warn": "⚠️ ",
    "operational_info": "ℹ️ ",  # noqa: RUF001 — the info glyph is operator-facing
    "phase1_listing": "📦",
    "phase2_listing": "🟢",
    "phase2_buy_success": "✅",
    "phase2_buy_failure": "🚫",
    # Wallapop offer surfaces (wallapop-offer-flow; PRD amendment FR58-FR65).
    "negotiable_listing": "💰",
    "offer_sent": "💰",
    "offer_failure": "🚫",
}

#: Inline-keyboard button labels (Spanish per UX-DR27). PRD amendment to grow.
BUTTON_LABELS: Final[dict[str, str]] = {
    "view": "👁 Ver",
    "skip_phase1": "🙅 Saltar",
    "snooze": "😴 Posponer 24h",
    "buy": "✅ Comprar",
    "skip_phase2": "❌ Saltar",
    "offer": "💰 Ofertar",
}

#: Locked callback_data format. Max 64 bytes per Telegram.
CALLBACK_DATA_FORMAT: Final[str] = "<surface>:<verb>:<id>"

# Characters MarkdownV2 reserves and that user content must escape.
# Order matters for the regex — backslash MUST be first or it would
# double-escape itself.
_MD_V2_RESERVED = r"\_*[]()~`>#+-=|{}.!"
_MD_V2_RE = re.compile(r"([\\_*\[\]()~`>#+\-=|{}.!])")


class InlineButton(BaseModel):
    """One inline-keyboard button on a Telegram alert.

    ``callback_data`` is the value Telegram sends back to the bot when
    the operator taps the button. The format and byte cap are
    contract — :class:`TelegramSurface` does not re-validate.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    callback_data: str

    @field_validator("callback_data")
    @classmethod
    def _enforce_callback_format(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
            raise ValueError(
                f"callback_data exceeds Telegram's {_CALLBACK_DATA_MAX_BYTES}-byte limit"
            )
        if not _CALLBACK_DATA_RE.fullmatch(value):
            raise ValueError(
                "callback_data must match <surface>:<verb>:<id> "
                "(lowercase surface/verb, alphanumeric id)"
            )
        return value


class RenderedAlert(BaseModel):
    """Output of every renderer; input to :class:`TelegramSurface.send`.

    ``photo_url`` is None for non-listing alerts (operational warnings,
    smoke-test results). ``inline_keyboard`` is None when the alert is
    informational (no buttons).
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    parse_mode: ParseMode = "MarkdownV2"
    photo_url: str | None = None
    inline_keyboard: list[list[InlineButton]] | None = None


CallbackVerb = Literal["view", "skip", "snooze", "buy", "offer"]


class CallbackEvent(BaseModel):
    """One inline-button tap received from Telegram.

    The :class:`TelegramSurface` adapter parses Telegram's
    ``CallbackQuery`` into this typed shape and hands it to the poll
    loop's registered handler. ``callback_data`` is the raw
    ``<surface>:<verb>:<id>`` value; ``verb`` is the parsed verb for
    easy dispatch.
    """

    model_config = ConfigDict(extra="forbid")

    callback_query_id: str = Field(min_length=1)
    chat_id: int
    message_id: int
    callback_data: str
    verb: CallbackVerb


class AlertSnapshot(BaseModel):
    """The immutable record of one alert dispatched to the operator.

    Persisted to ``alert_snapshots`` so the callback handler can replay
    the originating context (which entry, which listing, which
    evaluation) when the operator taps a button. Phase 2 adds
    ``phase2_max_price_eur`` for the autonomous-buy gate.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    entry_key: tuple[str, str, str]
    entry_display_name: str = Field(min_length=1)
    listing: Listing
    evaluation: ListingEvaluation
    phase: Phase
    phase2_max_price_eur: Decimal | None = None
    rendered_at: datetime
    #: Telegram message id returned by the send, persisted so the alert can
    #: later be edited in place (edit-alerts-on-state-change). ``None`` only
    #: for rows dispatched before the feature — those are never watched.
    telegram_message_id: int | None = None


# ─────────────────────────────────────────────────────────────────────────
# Rendering helpers — Story 3.11
# ─────────────────────────────────────────────────────────────────────────


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2-reserved character in ``text``.

    Telegram's MarkdownV2 reserves ``_*[]()~`>#+-=|{}.!`` (plus
    backslash). User-supplied content (titles, descriptions, LLM
    takes, locations) MUST pass through this before being interpolated
    into a template — otherwise a stray asterisk in a listing title
    could break the markup or open an injection vector.
    """
    return _MD_V2_RE.sub(r"\\\1", text)


def _format_amount_es(amount: Decimal) -> str:
    """Format a EUR Decimal in es-ES style WITHOUT the unit — ``1.234,56``.

    Split out from :func:`_format_price_es` so the comp line can render a
    ``min - max €`` range that shares a single trailing euro sign.
    """
    quantized = amount.quantize(Decimal("0.01"))
    # Python's built-in locale module is process-global and unreliable
    # across environments; we hand-format to keep snapshot tests stable
    # regardless of the host's locale.
    integer_part, _, decimal_part = str(quantized).partition(".")
    sign = ""
    if integer_part.startswith("-"):
        sign = "-"
        integer_part = integer_part[1:]
    # Insert dot every three digits from the right.
    chunks: list[str] = []
    while len(integer_part) > 3:
        chunks.append(integer_part[-3:])
        integer_part = integer_part[:-3]
    chunks.append(integer_part)
    int_grouped = ".".join(reversed(chunks))
    return f"{sign}{int_grouped},{decimal_part}"


def _format_price_es(amount: Decimal) -> str:
    """Format a EUR Decimal in es-ES style — ``1.234,56 €``."""
    return f"{_format_amount_es(amount)} €"


def _comp_line(summary: CompSummary) -> str:
    """Render the reserved-comp summary row (PR #7 Layer 2).

    Anatomy: ``💬 Comps (<n> reservados): <min> - <max> € · mediana <med> €``.
    The min/max pair shares one trailing ``€`` per the locked format. The
    whole line is plain prose (no intentional markup) so a single
    :func:`escape_markdown_v2` pass over the assembled string is correct —
    the parentheses and the price ``.``/``,`` would otherwise break the
    MarkdownV2 markup.
    """
    low = _format_amount_es(summary.min_eur)
    high = _format_price_es(summary.max_eur)
    median = _format_price_es(summary.median_eur)
    # EN DASH for the price range is the locked operator-facing format.
    plain = f"💬 Comps ({summary.count} reservados): {low} – {high} · mediana {median}"  # noqa: RUF001
    return escape_markdown_v2(plain)


def _md_v2_link(text: str, url: str) -> str:
    """Assemble a MarkdownV2 inline link ``[text](url)``.

    The visible ``text`` is escaped with the standard body escaper. The
    link *target* follows different rules: inside ``(...)`` MarkdownV2
    only treats ``\\`` and ``)`` as special, while ``.``/``-``/``!`` etc.
    (escaped in body text) must NOT be escaped or the URL breaks. So the
    target escapes exactly those two characters and leaves ``?``/``=``/
    ``&``/``#``/``|`` intact so the link still resolves.
    """
    escaped_target = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{escape_markdown_v2(text)}]({escaped_target})"


def _deeplink_row(listing: Listing) -> str:
    """Render the clickable deep-link row (FR18).

    ``🔗 Ver anuncio en <Marketplace>`` linking to ``listing.url``; the
    marketplace label reuses the same ``capitalize()`` form shown on the
    location row. Present on every listing alert — the URL is required.
    """
    marketplace = listing.marketplace.capitalize()
    return "🔗 " + _md_v2_link(f"Ver anuncio en {marketplace}", listing.url)


def _cost_line(cost: BuyerCost) -> str:
    """Render the buyer-total breakdown row (shipping-aware-pricing).

    ``💶 <item> + <shipping> envío[ (est.)][ + <fee> Protección]
    [ + <import> importación (est.)] = <total> €`` so the operator sees the
    full delivered cost before tapping Comprar. ``(est.)`` marks a
    buffer-estimated component; the Protección term shows only on Wallapop
    (fee > 0) and the importación term only for non-EU-located listings
    (always estimated — ebay-import-charges-pricing). Plain prose → one
    escape pass.
    """
    item = _format_amount_es(cost.item_eur)
    shipping = _format_amount_es(cost.shipping_eur)
    shipping_part = f"{shipping} envío (est.)" if cost.shipping_estimated else f"{shipping} envío"
    fee_part = f" + {_format_amount_es(cost.fee_eur)} Protección" if cost.fee_eur > 0 else ""
    import_part = (
        f" + {_format_amount_es(cost.import_charges_eur)} importación (est.)"
        if cost.import_charges_eur > 0
        else ""
    )
    total = _format_price_es(cost.total_eur)
    return escape_markdown_v2(f"💶 {item} + {shipping_part}{fee_part}{import_part} = {total}")


def _phase1_button_row(alert_id: str) -> list[InlineButton]:
    """The Phase 1 button row: Ver · Saltar · Posponer 24h (UX-DR4).

    ``callback_data`` carries the AlertSnapshot's UUID (``alert_id``)
    rather than the raw ``listing_id`` because eBay listing IDs
    contain ``|`` characters that aren't valid callback_data and
    because the callbacks table indexes on ``alert_id`` regardless.
    The callback handler resolves the originating listing by reading
    the alert_snapshot row.
    """
    return [
        InlineButton(text=BUTTON_LABELS["view"], callback_data=f"listing:view:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["skip_phase1"], callback_data=f"listing:skip:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["snooze"], callback_data=f"listing:snooze:{alert_id}"),
    ]


def render_phase1_listing_alert(
    snapshot: AlertSnapshot,
    *,
    comp_summary: CompSummary | None = None,
    buyer_cost: BuyerCost | None = None,
    offer_eur: Decimal | None = None,
    offer_target_total_eur: Decimal | None = None,
) -> RenderedAlert:
    """Render a Phase 1 listing alert (Direction A + Direction E hybrid).

    Anatomy (direct listing):
      1. ``{📦} *<entry_display_name>* — *<price>*``
      2. ``📍 <location> · <marketplace>``
      3. ``🔗 Ver anuncio en <Marketplace>`` — clickable deep link to the
         listing URL (FR18); present on every listing alert.
      4. ``_<one_line_take>_``
      5. ``🔍 Confidence: <low|medium|high>``
      6. (optional) ``💬 Comps (<n> reservados): <min> - <max> € · mediana <med> €``
         — present only when ``comp_summary`` carries in-cycle reserved comps.

    When ``snapshot.evaluation.is_container == True``, two indented
    rows are inserted between the deep-link row and the take row:
      - ``  ↪︎ Wrapper: <wrapper_text>``
      - ``  ↪︎ Extracted: <extracted_text>``

    Every user-supplied substring passes through
    :func:`escape_markdown_v2` so a listing title with an asterisk
    can't break the markup or open an injection vector.

    The output is locked at v1 per FR22; snapshot tests in
    ``test_alert_renderer.py`` fail the build on any drift.
    """
    listing = snapshot.listing
    evaluation = snapshot.evaluation

    severity = SEVERITY_TOKENS["phase1_listing"]
    name = escape_markdown_v2(snapshot.entry_display_name)
    price = escape_markdown_v2(_format_price_es(listing.price_eur))
    location = escape_markdown_v2(listing.location or "—")
    marketplace = escape_markdown_v2(listing.marketplace.capitalize())
    take = escape_markdown_v2(evaluation.one_line_take)
    confidence = escape_markdown_v2(evaluation.confidence)

    rows: list[str] = [
        f"{severity} *{name}* — *{price}*",
        f"📍 {location} · {marketplace}",
    ]
    if buyer_cost is not None:
        rows.append(_cost_line(buyer_cost))
    if offer_eur is not None and offer_target_total_eur is not None:
        rows.append(_offer_line(offer_eur, offer_target_total_eur))
    rows.append(_deeplink_row(listing))

    if evaluation.is_container:
        wrapper = escape_markdown_v2(evaluation.wrapper_text or "—")
        extracted = escape_markdown_v2(evaluation.extracted_text or "—")
        rows.append(f"  ↪︎ Wrapper: {wrapper}")
        rows.append(f"  ↪︎ Extracted: {extracted}")

    rows.append(f"_{take}_")
    rows.append(f"🔍 Confidence: {confidence}")
    if comp_summary is not None:
        rows.append(_comp_line(comp_summary))

    photo_url = listing.photo_urls[0] if listing.photo_urls else None

    keyboard = [_phase1_button_row(str(snapshot.alert_id))]
    if offer_eur is not None and offer_target_total_eur is not None:
        keyboard.append(offer_button_row(str(snapshot.alert_id)))

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=photo_url,
        inline_keyboard=keyboard,
    )


def _phase2_button_row(alert_id: str) -> list[InlineButton]:
    """The Phase 2 button row: Comprar · Saltar · Ver (UX-DR4, FR23).

    Order is locked: ``Comprar`` first so the affirmative action sits in
    the visually-dominant left slot. ``callback_data`` carries the
    AlertSnapshot UUID — same lookup path as Phase 1.
    """
    return [
        InlineButton(text=BUTTON_LABELS["buy"], callback_data=f"listing:buy:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["skip_phase2"], callback_data=f"listing:skip:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["view"], callback_data=f"listing:view:{alert_id}"),
    ]


def render_phase2_listing_alert(
    snapshot: AlertSnapshot,
    phase2_max_price_eur: Decimal,
    *,
    comp_summary: CompSummary | None = None,
    buyer_cost: BuyerCost | None = None,
    offer_eur: Decimal | None = None,
    offer_target_total_eur: Decimal | None = None,
) -> RenderedAlert:
    """Render a Phase 2 listing alert (Story 5.2 / FR23 / FR24 / UX-DR7).

    Identical to :func:`render_phase1_listing_alert` except for three
    locked substitutions:

      - Severity prefix ``📦`` → ``🟢`` (operator-visible signal that
        the Buy button is live for this alert).
      - Confidence row gets a ``· Phase 2 max: <€>`` suffix carrying the
        per-entry ceiling the autonomous buy will honour (FR26).
      - Inline keyboard is ``[Comprar · Saltar · Ver]`` instead of the
        Phase 1 ``[Ver · Saltar · Posponer]`` row.

    The optional ``comp_summary`` row renders identically to Phase 1 —
    after the (Phase 2 max) Confidence row — when in-cycle reserved comps
    exist; the Comprar keyboard is untouched.

    The container-aware Direction E split (Story 3.11) applies here too:
    a Phase 2 alert for a wrapper listing still gets the indented
    wrapper / extracted rows between the location and the take.
    """
    listing = snapshot.listing
    evaluation = snapshot.evaluation

    severity = SEVERITY_TOKENS["phase2_listing"]
    name = escape_markdown_v2(snapshot.entry_display_name)
    price = escape_markdown_v2(_format_price_es(listing.price_eur))
    location = escape_markdown_v2(listing.location or "—")
    marketplace = escape_markdown_v2(listing.marketplace.capitalize())
    take = escape_markdown_v2(evaluation.one_line_take)
    confidence = escape_markdown_v2(evaluation.confidence)
    max_price = escape_markdown_v2(_format_price_es(phase2_max_price_eur))

    rows: list[str] = [
        f"{severity} *{name}* — *{price}*",
        f"📍 {location} · {marketplace}",
    ]
    if buyer_cost is not None:
        rows.append(_cost_line(buyer_cost))
    if offer_eur is not None and offer_target_total_eur is not None:
        rows.append(_offer_line(offer_eur, offer_target_total_eur))
    rows.append(_deeplink_row(listing))

    if evaluation.is_container:
        wrapper = escape_markdown_v2(evaluation.wrapper_text or "—")
        extracted = escape_markdown_v2(evaluation.extracted_text or "—")
        rows.append(f"  ↪︎ Wrapper: {wrapper}")
        rows.append(f"  ↪︎ Extracted: {extracted}")

    rows.append(f"_{take}_")
    rows.append(f"🔍 Confidence: {confidence} · Phase 2 max: {max_price}")
    if comp_summary is not None:
        rows.append(_comp_line(comp_summary))

    photo_url = listing.photo_urls[0] if listing.photo_urls else None

    keyboard = [_phase2_button_row(str(snapshot.alert_id))]
    if offer_eur is not None and offer_target_total_eur is not None:
        keyboard.append(offer_button_row(str(snapshot.alert_id)))

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=photo_url,
        inline_keyboard=keyboard,
    )


# ─────────────────────────────────────────────────────────────────────────
# Wallapop offer surfaces — wallapop-offer-flow
# ─────────────────────────────────────────────────────────────────────────


def _offer_line(offer_eur: Decimal, target_total_eur: Decimal) -> str:
    """Render the computed-offer row on offer-eligible alerts.

    ``💰 Oferta: <offer> € (total ≤ <target> €)`` — the exact amount the
    Ofertar tap will send, shown BEFORE the operator taps (the orchestrator
    recomputes it from the reconciled listing and aborts on drift). Plain
    prose → one escape pass.
    """
    offer = _format_price_es(offer_eur)
    target = _format_price_es(target_total_eur)
    return escape_markdown_v2(f"💰 Oferta: {offer} (total ≤ {target})")


def offer_button_row(alert_id: str) -> list[InlineButton]:
    """The standalone ``💰 Ofertar`` row appended to offer-eligible Phase 1/
    Phase 2 alerts — its own row so the locked Phase 1/Phase 2 button rows
    stay byte-identical (FR22)."""
    return [
        InlineButton(text=BUTTON_LABELS["offer"], callback_data=f"listing:offer:{alert_id}"),
    ]


def negotiable_button_row(alert_id: str) -> list[InlineButton]:
    """The negotiable-alert button row: Ofertar · Saltar · Ver.

    Mirrors the Phase 2 layout (affirmative action in the dominant left
    slot) with Ofertar in place of Comprar — a negotiable listing is over
    ceiling by definition, so it can never carry a buy button.
    """
    return [
        InlineButton(text=BUTTON_LABELS["offer"], callback_data=f"listing:offer:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["skip_phase2"], callback_data=f"listing:skip:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["view"], callback_data=f"listing:view:{alert_id}"),
    ]


def offer_sent_badge_row(alert_id: str) -> list[InlineButton]:
    """Terminal non-tappable ``💰 Oferta enviada`` badge after a successful
    send — the Ofertar row never returns for that listing (per-listing
    dedupe). ``noop`` pattern as the other badges."""
    return [
        InlineButton(text="💰 Oferta enviada", callback_data=f"listing:noop:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["view"], callback_data=f"listing:view:{alert_id}"),
    ]


def render_negotiable_listing_alert(
    snapshot: AlertSnapshot,
    *,
    offer_eur: Decimal,
    offer_target_total_eur: Decimal,
    comp_summary: CompSummary | None = None,
    buyer_cost: BuyerCost | None = None,
) -> RenderedAlert:
    """Render a negotiable-band listing alert (wallapop-offer-flow).

    Same anatomy as :func:`render_phase1_listing_alert` with three locked
    substitutions:

      - Severity prefix ``📦`` → ``💰`` (over ceiling, offerable into it).
      - An offer row after the buyer-total breakdown carrying the computed
        amount and the target it fits.
      - Inline keyboard ``[Ofertar · Saltar · Ver]`` — never Comprar.
    """
    listing = snapshot.listing
    evaluation = snapshot.evaluation

    severity = SEVERITY_TOKENS["negotiable_listing"]
    name = escape_markdown_v2(snapshot.entry_display_name)
    price = escape_markdown_v2(_format_price_es(listing.price_eur))
    location = escape_markdown_v2(listing.location or "—")
    marketplace = escape_markdown_v2(listing.marketplace.capitalize())
    take = escape_markdown_v2(evaluation.one_line_take)
    confidence = escape_markdown_v2(evaluation.confidence)

    rows: list[str] = [
        f"{severity} *{name}* — *{price}*",
        f"📍 {location} · {marketplace}",
    ]
    if buyer_cost is not None:
        rows.append(_cost_line(buyer_cost))
    rows.append(_offer_line(offer_eur, offer_target_total_eur))
    rows.append(_deeplink_row(listing))

    if evaluation.is_container:
        wrapper = escape_markdown_v2(evaluation.wrapper_text or "—")
        extracted = escape_markdown_v2(evaluation.extracted_text or "—")
        rows.append(f"  ↪︎ Wrapper: {wrapper}")
        rows.append(f"  ↪︎ Extracted: {extracted}")

    rows.append(f"_{take}_")
    rows.append(f"🔍 Confidence: {confidence}")
    if comp_summary is not None:
        rows.append(_comp_line(comp_summary))

    photo_url = listing.photo_urls[0] if listing.photo_urls else None

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=photo_url,
        inline_keyboard=[negotiable_button_row(str(snapshot.alert_id))],
    )


# ─────────────────────────────────────────────────────────────────────────
# Live alert updates — edit-alerts-on-state-change
# ─────────────────────────────────────────────────────────────────────────

#: Static banner texts for the non-price transitions. The price-drop banner
#: is built per-edit (it carries both prices). Locked formats (FR22).
UPDATE_BANNERS: Final[dict[str, str]] = {
    "reserved": "🔴 RESERVADO",
    "available": "🟢 Disponible de nuevo",
}


def update_banner_line(
    change_kind: str,
    *,
    old_price_eur: Decimal | None = None,
    new_price_eur: Decimal | None = None,
) -> str:
    """The single status line prepended to an edited alert body.

    Subsequent updates REPLACE this banner (never stack — history lives
    in the ``alert_updates`` audit table). ``price_drop`` shows the new
    price with the last price the operator saw: ``📉 80,00 € (antes
    95,00 €)``. One escape pass, like the other prose rows.
    """
    if change_kind == "price_drop":
        if old_price_eur is None or new_price_eur is None:
            raise ValueError("price_drop banner requires both prices")
        new = _format_price_es(new_price_eur)
        old = _format_price_es(old_price_eur)
        return escape_markdown_v2(f"📉 {new} (antes {old})")
    try:
        return escape_markdown_v2(UPDATE_BANNERS[change_kind])
    except KeyError as exc:
        raise ValueError(f"unknown change_kind: {change_kind!r}") from exc


def apply_update_banner(
    base: RenderedAlert,
    banner_line: str,
    keyboard: list[list[InlineButton]] | None,
) -> RenderedAlert:
    """Prepend ``banner_line`` to a freshly re-rendered base alert.

    The base comes from the ordinary listing renderers (single source of
    truth for alert anatomy — Decision 5), so the body always reflects
    current values; ``keyboard`` is the reconstructed one the message
    currently deserves and is ALWAYS sent explicitly with an edit.
    """
    return base.model_copy(
        update={
            "text": f"{banner_line}\n{base.text}",
            "inline_keyboard": keyboard,
        }
    )


def phase2_dead_reserved_row(alert_id: str) -> list[InlineButton]:
    """Non-tappable ``🔴 Reservado`` badge replacing ``✅ Comprar`` when a
    watched Phase 2 alert's listing is reserved (restored on flip-back).
    ``noop`` is outside the surface's known-verb set, so a stray tap is
    dropped silently — same pattern as the in-flight badge."""
    return [
        InlineButton(text="🔴 Reservado", callback_data=f"listing:noop:{alert_id}"),
        InlineButton(text=BUTTON_LABELS["view"], callback_data=f"listing:view:{alert_id}"),
    ]


def render_price_drop_ping(
    entry_display_name: str,
    *,
    old_price_eur: Decimal,
    new_price_eur: Decimal,
) -> RenderedAlert:
    """The short NEW message sent for a big price drop (≥ ping threshold).

    Telegram edits are silent; a large drop is the one transition worth
    a notification. Sent as a plain text message (no photo, no buttons)
    — the caller pairs it with the edited original alert.
    """
    name = escape_markdown_v2(entry_display_name)
    drop = escape_markdown_v2(
        f"📉 Bajada: {_format_price_es(old_price_eur)} → {_format_price_es(new_price_eur)}"
    )
    return RenderedAlert(
        text=f"{drop} — *{name}*",
        parse_mode="MarkdownV2",
        photo_url=None,
        inline_keyboard=None,
    )


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 receipt + failure renderers — Stories 5.8 / 5.9
# ─────────────────────────────────────────────────────────────────────────

#: Mandatory reassurance line on every buy-failure variant (UX-DR10 /
#: FR28). The trailing period is part of the verbatim text; it renders
#: as ``\.`` in MarkdownV2 (Telegram un-escapes back to ``.`` for the
#: user). Tests assert the escaped form.
REASSURANCE_LINE: Final[str] = "La compra NO se ha ejecutado."

#: Special-case reassurance for ``screenshot_missing`` — the buy may
#: have actually succeeded, just without a captured receipt.
SCREENSHOT_MISSING_REASSURANCE: Final[str] = (
    "La compra puede haberse completado, pero no se capturó el recibo."
)

#: Human-friendly labels for the persisted ``payment_method`` enum.
_PAYMENT_METHOD_LABELS: Final[dict[str, str]] = {
    "wallapop_pay": "Wallapop Pay",
    "ebay_checkout": "eBay Checkout",
}

#: Per-variant short cause label shown on row 2 of the failure alert.
_BUY_FAILURE_CAUSE_LABELS: Final[dict[str, str]] = {
    "listing_gone": "El anuncio ya no está disponible (vendido o retirado)",
    "reconciliation_tripped": "Reconciliación de precios falló",
    "ui_check_failed": "Verificación de UI falló",
    "circuit_open": "Circuit breaker abierto",
    "missing_element": "Elemento esperado no encontrado",
    "marketplace_error": "Error en el marketplace",
    "timeout": "Timeout durante la compra",
    "screenshot_missing": "Captura del recibo no disponible",
    "payment_rail_unavailable": "Raíl de pago no disponible",
}


def render_phase2_buy_success(
    transaction: TransactionRecord,
    *,
    entry_display_name: str,
    audit_id: int,
) -> RenderedAlert:
    """Render the Phase 2 buy-success receipt — Story 5.8 (FR36 / UX-DR9).

    The receipt is *sacred*: the photo is the captured confirmation page
    and the body carries only factual fields (no celebration emoji
    other than the lead ``✅``). The orchestrator MUST divert to
    :func:`render_phase2_buy_failure` with ``reason=screenshot_missing``
    when ``transaction.screenshot_path`` is missing; this renderer
    raises if called without one, so a programming error fails loud
    instead of producing an empty-photo alert.
    """
    if not transaction.screenshot_path:
        raise ValueError(
            "render_phase2_buy_success requires a non-empty screenshot_path; "
            "the orchestrator must call render_phase2_buy_failure"
            "(reason=screenshot_missing) instead (UX-DR9)"
        )

    severity = SEVERITY_TOKENS["phase2_buy_success"]
    price = escape_markdown_v2(_format_price_es(transaction.price_paid_eur))
    payment = escape_markdown_v2(
        _PAYMENT_METHOD_LABELS.get(transaction.payment_method, transaction.payment_method)
    )
    entry = escape_markdown_v2(entry_display_name)

    rows = [
        f"{severity} *Comprado* · {price} · {payment}",
        f"Receipt: `{transaction.receipt_id}`",
        f"Listing: {entry}",
        f"Tiempo total: {transaction.total_seconds} s",
        _cmd(f"salvager audit show --id {audit_id}")
        + _prose(" para el registro completo de eventos."),
    ]

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=transaction.screenshot_path,
        inline_keyboard=None,
    )


def render_phase2_buy_failure(
    reason: BuyFailureReason,
    *,
    entry_display_name: str,
    ctx: Mapping[str, Any] | None = None,
) -> RenderedAlert:
    """Render a Phase 2 buy-failure alert — Story 5.9 (FR28 / UX-DR10).

    The reassurance line — :data:`REASSURANCE_LINE` (or the
    ``screenshot_missing`` special case) — is non-optional: the user
    must answer "did the agent buy it?" from the alert alone. The
    per-variant detail rows and next-step CLI commands live in dedicated
    helpers so the property-test (5.16) can enumerate them.
    """
    ctx_map = dict(ctx) if ctx is not None else {}
    severity = SEVERITY_TOKENS["phase2_buy_failure"]
    entry = escape_markdown_v2(entry_display_name)
    cause = escape_markdown_v2(_BUY_FAILURE_CAUSE_LABELS[reason.value])

    rows: list[str] = [
        f"{severity} *Compra abortada* · {entry}",
        _prose("Causa: ") + cause,
    ]
    rows.extend(_buy_failure_detail_rows(reason, ctx_map))
    rows.append("")
    if reason.value == "screenshot_missing":
        rows.append(_prose(SCREENSHOT_MISSING_REASSURANCE))
    else:
        rows.append(_prose(REASSURANCE_LINE))
    rows.extend(_buy_failure_next_steps(reason, ctx_map))

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=None,
        inline_keyboard=None,
    )


def _buy_failure_detail_rows(reason: BuyFailureReason, ctx: Mapping[str, Any]) -> list[str]:
    """Variant-specific bullet rows that explain the failure."""
    name = reason.value
    if name == "reconciliation_tripped":
        api = _price_or_dash(ctx.get("api_price"))
        html = _price_or_dash(ctx.get("html_price"))
        tol = _price_or_dash(ctx.get("tolerance_eur"))
        return [
            _prose("- Wallapop API: ") + api,
            _prose("- Wallapop HTML: ") + html,
            _prose("- Tolerancia: ") + tol,
        ]
    if name == "circuit_open":
        failures = ctx.get("consecutive_failures", "—")
        threshold = ctx.get("threshold", "—")
        return [
            _prose(f"- {failures} fallos consecutivos · circuito abierto"),
            _prose(f"- Umbral: {threshold}"),
        ]
    if name == "screenshot_missing":
        receipt_id = ctx.get("receipt_id")
        if receipt_id is not None:
            return [_prose("Recibo: ") + f"`{receipt_id}`"]
        return []
    if name in {"ui_check_failed", "missing_element"}:
        missing = ctx.get("missing", "—")
        return [_prose(f"- Elementos faltantes: {missing}")]
    if name in {"marketplace_error", "timeout", "payment_rail_unavailable"}:
        detail = ctx.get("error_class") or ctx.get("detail") or "—"
        return [_prose(f"- Detalle: {detail}")]
    return []


def _buy_failure_next_steps(reason: BuyFailureReason, ctx: Mapping[str, Any]) -> list[str]:
    """Variant-specific numbered next-step CLI hints."""
    name = reason.value
    if name == "reconciliation_tripped":
        return [
            "",
            _prose(_NEXT_STEP_HEADER),
            _prose("1. ") + _cmd("salvager audit show --last 1"),
            _prose("2. Revisa el parser HTML antes de reactivar Fase 2 con ")
            + _cmd(_CMD_PHASE2_ENABLE),
        ]
    if name == "circuit_open":
        return [
            "",
            _prose(_NEXT_STEP_HEADER),
            _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
            _prose("2. ") + _cmd(_CMD_PHASE2_ENABLE),
        ]
    if name == "screenshot_missing":
        transaction_id = ctx.get("transaction_id", "<transaction_id>")
        receipt_id = ctx.get("receipt_id")
        steps = [
            "",
            _prose(_NEXT_STEP_HEADER),
            _prose("1. ") + _cmd(f"salvager audit show --id {transaction_id}"),
        ]
        if receipt_id is not None:
            steps.append(_prose("2. ") + _cmd(f"salvager phase2 reconcile {receipt_id}"))
        return steps
    # Generic catch-all next-step block for the remaining variants.
    return [
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. ") + _cmd(_CMD_PHASE2_ENABLE),
    ]


def _price_or_dash(value: object) -> str:
    """Format a ctx price (Decimal or string) in ES style; em-dash on missing."""
    if value is None:
        return _prose("—")
    if isinstance(value, Decimal):
        return escape_markdown_v2(_format_price_es(value))
    try:
        return escape_markdown_v2(_format_price_es(Decimal(str(value))))
    except (ValueError, ArithmeticError):
        return escape_markdown_v2(str(value))


# ─────────────────────────────────────────────────────────────────────────
# Offer outcome renderers — wallapop-offer-flow
# ─────────────────────────────────────────────────────────────────────────

#: Mandatory reassurance line on every offer-failure variant: the operator
#: must answer "did an offer go out?" from the alert alone.
OFFER_REASSURANCE_LINE: Final[str] = "No se ha enviado ninguna oferta."

#: Special-case reassurance for ``screenshot_missing`` — the send may have
#: actually happened, just without captured evidence.
OFFER_SCREENSHOT_MISSING_REASSURANCE: Final[str] = (
    "La oferta puede haberse enviado, pero no se capturó la confirmación."
)

_CMD_OFFER_ENABLE: Final[str] = "salvager offer enable <entry>"

#: Per-variant short cause label shown on row 2 of the offer-failure alert.
_OFFER_FAILURE_CAUSE_LABELS: Final[dict[str, str]] = {
    "listing_gone": "El anuncio ya no está disponible (vendido o retirado)",
    "reconciliation_tripped": "El anuncio cambió desde la alerta (precio/estado)",
    "offer_unavailable": "El anuncio no admite ofertas",
    "amount_rejected": "Wallapop rechazó el importe ofertado",
    "daily_limit_reached": "Límite diario de ofertas alcanzado",
    "duplicate_offer": "Ya existe una oferta enviada para este anuncio",
    "lockout_engaged": "Envío de ofertas bloqueado por fallos consecutivos",
    "missing_element": "Elemento esperado no encontrado",
    "marketplace_error": "Error en el marketplace",
    "timeout": "Timeout durante el envío de la oferta",
    "screenshot_missing": "Captura de confirmación no disponible",
    "ui_check_failed": "Verificación de UI falló",
}


def render_offer_sent(
    *,
    entry_display_name: str,
    offered_eur: Decimal,
    audit_id: int,
    screenshot_path: str | None = None,
    platform_remaining: int | None = None,
) -> RenderedAlert:
    """Render the offer-sent confirmation (wallapop-offer-flow).

    v1 ends here: the negotiation continues in the Wallapop app. The body
    states exactly what an acceptance means — a 24 h window to buy at the
    accepted price, with the item NOT reserved meanwhile — so the operator
    knows to watch the chat. ``platform_remaining`` is Wallapop's "ofertas
    restantes" counter when the agent captured it.
    """
    severity = SEVERITY_TOKENS["offer_sent"]
    price = escape_markdown_v2(_format_price_es(offered_eur))
    entry = escape_markdown_v2(entry_display_name)

    rows = [
        f"{severity} *Oferta enviada* · {price}",
        f"Listing: {entry}",
        _prose("El vendedor puede aceptar, rechazar o contraofertar — vigila el chat de Wallapop."),
        _prose(
            "Si acepta: tienes 24 h para comprar al precio aceptado en la app "
            "(el artículo NO queda reservado)."
        ),
    ]
    if platform_remaining is not None:
        rows.append(_prose(f"Ofertas restantes hoy: {platform_remaining}"))
    rows.append(
        _cmd(f"salvager audit show --id {audit_id}")
        + _prose(" para el registro completo de eventos.")
    )

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=screenshot_path,
        inline_keyboard=None,
    )


def render_offer_failure(
    reason: OfferFailureReason,
    *,
    entry_display_name: str,
    ctx: Mapping[str, Any] | None = None,
) -> RenderedAlert:
    """Render an offer-failure alert (wallapop-offer-flow).

    Mirrors :func:`render_phase2_buy_failure`: the reassurance line —
    :data:`OFFER_REASSURANCE_LINE` (or the ``screenshot_missing`` ambiguity
    special case) — is non-optional. A variant without a cause label fails
    loud (KeyError) rather than rendering a hole.
    """
    ctx_map = dict(ctx) if ctx is not None else {}
    severity = SEVERITY_TOKENS["offer_failure"]
    entry = escape_markdown_v2(entry_display_name)
    cause = escape_markdown_v2(_OFFER_FAILURE_CAUSE_LABELS[reason.value])

    rows: list[str] = [
        f"{severity} *Oferta no enviada* · {entry}",
        _prose("Causa: ") + cause,
    ]
    rows.extend(_offer_failure_detail_rows(reason, ctx_map))
    rows.append("")
    if reason.value == "screenshot_missing":
        rows.append(_prose(OFFER_SCREENSHOT_MISSING_REASSURANCE))
    else:
        rows.append(_prose(OFFER_REASSURANCE_LINE))
    rows.extend(_offer_failure_next_steps(reason, ctx_map))

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=None,
        inline_keyboard=None,
    )


def _offer_failure_detail_rows(reason: OfferFailureReason, ctx: Mapping[str, Any]) -> list[str]:
    """Variant-specific bullet rows that explain the offer failure."""
    name = reason.value
    if name == "reconciliation_tripped":
        displayed = _price_or_dash(ctx.get("displayed_offer"))
        recomputed = ctx.get("recomputed_offer")
        recomputed_row = (
            _prose("- Oferta recalculada: ") + _price_or_dash(recomputed)
            if recomputed is not None
            else _prose("- Oferta recalculada: ya no es posible (precio/estado nuevo)")
        )
        return [_prose("- Oferta mostrada: ") + displayed, recomputed_row]
    if name == "amount_rejected":
        return [_prose("- Importe intentado: ") + _price_or_dash(ctx.get("offered"))]
    if name == "daily_limit_reached":
        source = ctx.get("limit_source", "propio")
        label = (
            "presupuesto propio (offer.daily_limit)"
            if source == "propio"
            else "límite de Wallapop (10/día)"
        )
        return [_prose(f"- Límite: {label}")]
    if name == "lockout_engaged":
        failures = ctx.get("consecutive_failures", "—")
        threshold = ctx.get("threshold", "—")
        return [_prose(f"- {failures} fallos consecutivos · umbral: {threshold}")]
    if name == "offer_unavailable":
        return [_prose("- Vendedor PRO, producto reacondicionado o categoría excluida")]
    if name in {"ui_check_failed", "missing_element"}:
        missing = ctx.get("missing", "—")
        return [_prose(f"- Elementos faltantes: {missing}")]
    if name in {"marketplace_error", "timeout"}:
        detail = ctx.get("error_class") or ctx.get("detail") or "—"
        return [_prose(f"- Detalle: {detail}")]
    return []


def _offer_failure_next_steps(reason: OfferFailureReason, _ctx: Mapping[str, Any]) -> list[str]:
    """Variant-specific numbered next-step hints."""
    name = reason.value
    if name == "lockout_engaged":
        return [
            "",
            _prose(_NEXT_STEP_HEADER),
            _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
            _prose("2. ") + _cmd(_CMD_OFFER_ENABLE),
        ]
    if name == "daily_limit_reached":
        return [
            "",
            _prose(_NEXT_STEP_HEADER),
            _prose("1. Reintenta cuando la ventana de 24 h libere presupuesto"),
        ]
    if name == "screenshot_missing":
        return [
            "",
            _prose(_NEXT_STEP_HEADER),
            _prose("1. Comprueba el chat del anuncio en la app de Wallapop"),
            _prose("2. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        ]
    return [
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
    ]


# ─────────────────────────────────────────────────────────────────────────
# Operational alert renderer — Story 4.1 (FR21 / UX-DR13 / UX-DR14 / UX-DR15)
# ─────────────────────────────────────────────────────────────────────────

#: Operational-alert severity. ``warn`` renders the ⚠️ anatomy (bold
#: headline + numbered CLI next-steps); ``info`` renders the plain-headline
#: anatomy with an optional single hint.
Severity = Literal["warn", "info"]


class EventName(enum.Enum):
    """Every Phase 1 operational-alert event.

    The variant pool is finite per UX-DR13 — adding a variant is a PRD
    amendment, not a code change. The renderer's spec registry
    (:data:`_OPERATIONAL_EVENT_SPECS`) must carry an entry for every
    member; a missing entry fails loud at render time.
    """

    daemon_started = "daemon_started"
    daemon_stopped = "daemon_stopped"
    wallapop_session_expired = "wallapop_session_expired"
    wallapop_session_renewed = "wallapop_session_renewed"
    wallapop_api_degraded = "wallapop_api_degraded"
    wallapop_both_paths_down = "wallapop_both_paths_down"
    tinyfish_fallback_active = "tinyfish_fallback_active"
    tinyfish_fallback_recovered = "tinyfish_fallback_recovered"
    ebay_token_refresh_failed = "ebay_token_refresh_failed"
    ebay_quota_breach = "ebay_quota_breach"
    llm_provider_rate_limited = "llm_provider_rate_limited"
    entry_snoozed = "entry_snoozed"
    poll_cycle_error = "poll_cycle_error"
    # Phase 2 operational variants (Stories 5.5, 5.6, 5.11). The set is
    # closed: any new variant is a PRD amendment.
    circuit_open = "circuit_open"
    smoke_test_failed = "smoke_test_failed"
    smoke_test_recovered = "smoke_test_recovered"
    phase2_disabled = "phase2_disabled"
    phase2_re_enabled = "phase2_re_enabled"
    phase2_buy_callback_received = "phase2_buy_callback_received"
    phase2_screenshot_missing = "phase2_screenshot_missing"
    phase2_buy_completion_slow = "phase2_buy_completion_slow"
    buy_orchestrator_error = "buy_orchestrator_error"
    # Wallapop offer-flow operational variants (wallapop-offer-flow). The
    # set stays closed: any new variant is a PRD amendment.
    offer_lockout_engaged = "offer_lockout_engaged"
    offer_disabled = "offer_disabled"
    offer_re_enabled = "offer_re_enabled"
    offer_orchestrator_error = "offer_orchestrator_error"


def _prose(text: str) -> str:
    """Escape a plain-text fragment for MarkdownV2.

    Use for every static template string and every ``ctx`` value — the
    whole fragment is plain text with no intentional markup, so a
    single escape pass over the assembled string is correct.
    """
    return escape_markdown_v2(text)


def _cmd(command: str) -> str:
    """Wrap a CLI command in a MarkdownV2 code span.

    Inside a code span only backtick + backslash are special, and CLI
    commands carry neither — so the command text needs no escaping.
    """
    return f"`{command}`"


def _body_daemon_started(ctx: Mapping[str, Any]) -> list[str]:
    version = str(ctx.get("version", "—"))
    jobs = str(ctx.get("jobs", "—"))
    return [_prose(f"Versión: {version} · jobs: {jobs}")]


def _body_daemon_stopped(ctx: Mapping[str, Any]) -> list[str]:
    return [_prose(f"Motivo: {ctx.get('reason', '—')}")]


def _body_wallapop_session_expired(_ctx: Mapping[str, Any]) -> list[str]:
    return [
        _prose("Adapter: wallapop_api (devuelve 401)"),
        _prose("Fallback: wallapop_tinyfish activo (sin alertas perdidas)"),
        "",
        _prose(_NEXT_STEP_HEADER + " ")
        + _cmd("salvager login wallapop")
        + _prose(" cuando puedas"),
    ]


def _body_wallapop_session_renewed(_ctx: Mapping[str, Any]) -> list[str]:
    return [_prose("Adapter: wallapop_api (camino principal reactivado)")]


def _body_wallapop_api_degraded(ctx: Mapping[str, Any]) -> list[str]:
    error_class = str(ctx.get("error_class", "—"))
    return [
        _prose(f"Adapter: wallapop_api ({error_class})"),
        _prose("Fallback: wallapop_tinyfish activo este ciclo"),
    ]


def _body_wallapop_both_paths_down(ctx: Mapping[str, Any]) -> list[str]:
    failures = ctx.get("consecutive_failures", "—")
    error_class = str(ctx.get("last_error_class", "—"))
    return [
        _prose(f"Causa: {failures} fallos consecutivos · último error: {error_class}"),
        _prose("Estado actual: alertas de Wallapop en pausa (eBay no afectado)"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. revisa la conexión o parchea el adaptador si persiste"),
        _prose("3. ") + _cmd("docker-compose restart salvager"),
    ]


def _body_tinyfish_fallback_active(_ctx: Mapping[str, Any]) -> list[str]:
    return [
        _prose("Adapter: wallapop_tinyfish en uso como camino de respaldo"),
        _prose("Estado: sin alertas perdidas"),
    ]


def _body_tinyfish_fallback_recovered(_ctx: Mapping[str, Any]) -> list[str]:
    return [_prose("Adapter: wallapop_api recuperado como camino principal")]


def _body_ebay_token_refresh_failed(_ctx: Mapping[str, Any]) -> list[str]:
    return [
        _prose("Causa: el endpoint de refresco rechazó el refresh token (HTTP 401)"),
        _prose("Estado actual: alertas de eBay en pausa (Wallapop no afectado)"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd("salvager login ebay --ru-name <tu-runame>"),
        _prose("2. ") + _cmd("docker-compose restart salvager"),
    ]


def _body_ebay_quota_breach(ctx: Mapping[str, Any]) -> list[str]:
    used = ctx.get("used", "—")
    budget = ctx.get("budget", "—")
    return [
        _prose(f"Cuota: {used}/{budget} peticiones usadas hoy"),
        _prose("Estado: cadencia de eBay reducida hasta el reset de medianoche UTC"),
    ]


def _body_llm_provider_rate_limited(ctx: Mapping[str, Any]) -> list[str]:
    provider = str(ctx.get("provider", "—"))
    return [
        _prose(f"Proveedor: {provider} (rate-limited)"),
        _prose("Estado: la caché y el reintento absorben el límite"),
    ]


def _body_entry_snoozed(ctx: Mapping[str, Any]) -> list[str]:
    entry = str(ctx.get("entry_display_name", "—"))
    until = str(ctx.get("snooze_until", "—"))
    return [
        _prose(f"Entrada: {entry}"),
        _prose(f"Pospuesta hasta: {until}"),
    ]


def _body_smoke_test_failed(ctx: Mapping[str, Any]) -> list[str]:
    fixture = str(ctx.get("fixture_name", "—"))
    parsed = str(ctx.get("parsed_price", "—"))
    expected = str(ctx.get("expected_price", "—"))
    delta = str(ctx.get("delta_eur", "—"))
    return [
        _prose(f"Fixture: {fixture}"),
        _prose(f"Parser: {parsed} € · esperado: {expected} € · delta: {delta} €"),
        _prose(_STATUS_PHASE2_GLOBALLY_DISABLED),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd("salvager phase2 smoke-test"),
        _prose("2. ") + _cmd("salvager audit show --type phase2_smoke_test --last 3"),
        _prose("3. parchea el parser y reactiva con ") + _cmd(_CMD_PHASE2_ENABLE),
    ]


def _body_smoke_test_recovered(_ctx: Mapping[str, Any]) -> list[str]:
    return [_prose("Estado: parser de precios recuperado; Fase 2 puede reactivarse")]


def _body_phase2_disabled(ctx: Mapping[str, Any]) -> list[str]:
    reason = str(ctx.get("reason", "—"))
    last_entry = str(ctx.get("last_affected_entry", "—"))
    return [
        _prose(f"Causa: {reason}"),
        _prose(f"Última entrada afectada: {last_entry}"),
        _prose(_STATUS_PHASE2_GLOBALLY_DISABLED),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. revisa el motivo y parchea si es un bug"),
        _prose("3. ") + _cmd(_CMD_PHASE2_ENABLE),
    ]


def _body_phase2_re_enabled(ctx: Mapping[str, Any]) -> list[str]:
    entry = str(ctx.get("entry", "—"))
    return [_prose(f"Entrada: {entry}")]


def _body_phase2_buy_callback_received(ctx: Mapping[str, Any]) -> list[str]:
    entry = str(ctx.get("entry", "—"))
    alert_id = str(ctx.get("alert_id", "—"))
    return [
        _prose(f"Entrada: {entry}"),
        _prose(f"Alert: {alert_id}"),
        _prose("Estado: compra en curso"),
    ]


def _body_phase2_screenshot_missing(ctx: Mapping[str, Any]) -> list[str]:
    receipt_id = str(ctx.get("receipt_id", "—"))
    listing_id = str(ctx.get("listing_id", "—"))
    return [
        _prose(f"Recibo: {receipt_id} · listing: {listing_id}"),
        _prose("Estado: la compra puede haberse completado, pero no se capturó el recibo"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. revisa el marketplace manualmente para confirmar la transacción"),
    ]


def _body_phase2_buy_completion_slow(ctx: Mapping[str, Any]) -> list[str]:
    elapsed = ctx.get("elapsed_seconds", "—")
    budget = ctx.get("budget_seconds", "—")
    entry = str(ctx.get("entry", "—"))
    return [
        _prose(f"Entrada: {entry}"),
        _prose(f"Duración: {elapsed}s (presupuesto: {budget}s)"),
        _prose("Estado: la compra terminó pero excedió el presupuesto"),
    ]


def _body_buy_orchestrator_error(ctx: Mapping[str, Any]) -> list[str]:
    error_class = str(ctx.get("error_class", "—"))
    alert_id = str(ctx.get("alert_id", "—"))
    return [
        _prose(f"Causa: {error_class}"),
        _prose(f"Alert: {alert_id}"),
        _prose("Estado actual: Fase 2 desactivada por seguridad"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. ") + _cmd("salvager logs --last 100"),
        _prose("3. ") + _cmd(_CMD_PHASE2_ENABLE),
    ]


def _body_circuit_open(ctx: Mapping[str, Any]) -> list[str]:
    failures = ctx.get("consecutive_failures", "—")
    threshold = ctx.get("threshold", "—")
    last_entry = str(ctx.get("last_affected_entry", "—"))
    return [
        _prose(f"Causa: {failures} fallos consecutivos (umbral: {threshold})"),
        _prose(f"Última entrada afectada: {last_entry}"),
        _prose(_STATUS_PHASE2_GLOBALLY_DISABLED),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. revisa la causa y parchea si es un bug"),
        _prose("3. ") + _cmd(_CMD_PHASE2_ENABLE),
    ]


def _body_poll_cycle_error(ctx: Mapping[str, Any]) -> list[str]:
    error_class = str(ctx.get("error_class", "—"))
    marketplace = str(ctx.get("marketplace", "—"))
    return [
        _prose(f"Causa: {error_class} en el ciclo de {marketplace}"),
        _prose("Estado actual: el ciclo continuará en el siguiente tick"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. ") + _cmd("salvager logs --last 50"),
    ]


def _body_offer_lockout_engaged(ctx: Mapping[str, Any]) -> list[str]:
    failures = ctx.get("consecutive_failures", "—")
    threshold = ctx.get("threshold", "—")
    last_entry = str(ctx.get("last_affected_entry", "—"))
    return [
        _prose(f"Causa: {failures} fallos consecutivos (umbral: {threshold})"),
        _prose(f"Última entrada afectada: {last_entry}"),
        _prose("Estado actual: envío de ofertas desactivado globalmente"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
        _prose("2. revisa la causa y parchea si es un bug"),
        _prose("3. ") + _cmd(_CMD_OFFER_ENABLE),
    ]


def _body_offer_disabled(ctx: Mapping[str, Any]) -> list[str]:
    reason = str(ctx.get("reason", "—"))
    return [
        _prose(f"Causa: {reason}"),
        _prose("Estado actual: envío de ofertas desactivado globalmente"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_OFFER_ENABLE),
    ]


def _body_offer_re_enabled(ctx: Mapping[str, Any]) -> list[str]:
    entry = str(ctx.get("entry", "—"))
    return [_prose(f"Entrada: {entry}")]


def _body_offer_orchestrator_error(ctx: Mapping[str, Any]) -> list[str]:
    error_class = str(ctx.get("error_class", "—"))
    alert_id = str(ctx.get("alert_id", "—"))
    return [
        _prose(f"Causa: {error_class}"),
        _prose(f"Alert: {alert_id}"),
        _prose("Estado: ninguna oferta enviada; el teclado se ha restaurado"),
        "",
        _prose(_NEXT_STEP_HEADER),
        _prose("1. ") + _cmd(_CMD_AUDIT_SHOW_LAST5),
    ]


@dataclass(frozen=True)
class _OperationalEventSpec:
    """Per-event rendering contract: canonical severity + headline + body builder."""

    severity: Severity
    headline: str
    build_body: Callable[[Mapping[str, Any]], list[str]]


#: The closed registry — every :class:`EventName` member MUST appear here.
#: ``render_operational_alert`` raises ``KeyError`` for a missing entry,
#: so a new enum variant without a spec fails the build immediately.
_OPERATIONAL_EVENT_SPECS: Final[dict[EventName, _OperationalEventSpec]] = {
    EventName.daemon_started: _OperationalEventSpec(
        "info", "Daemon iniciado", _body_daemon_started
    ),
    EventName.daemon_stopped: _OperationalEventSpec(
        "info", "Daemon detenido", _body_daemon_stopped
    ),
    EventName.wallapop_session_expired: _OperationalEventSpec(
        "info", "Sesión Wallapop expirada", _body_wallapop_session_expired
    ),
    EventName.wallapop_session_renewed: _OperationalEventSpec(
        "info", "Sesión Wallapop renovada", _body_wallapop_session_renewed
    ),
    EventName.wallapop_api_degraded: _OperationalEventSpec(
        "info", "Wallapop API degradada", _body_wallapop_api_degraded
    ),
    EventName.wallapop_both_paths_down: _OperationalEventSpec(
        "warn", "Wallapop sin servicio", _body_wallapop_both_paths_down
    ),
    EventName.tinyfish_fallback_active: _OperationalEventSpec(
        "info", "Fallback TinyFish activo", _body_tinyfish_fallback_active
    ),
    EventName.tinyfish_fallback_recovered: _OperationalEventSpec(
        "info", "Camino principal de Wallapop recuperado", _body_tinyfish_fallback_recovered
    ),
    EventName.ebay_token_refresh_failed: _OperationalEventSpec(
        "warn", "eBay: token de refresco rechazado", _body_ebay_token_refresh_failed
    ),
    EventName.ebay_quota_breach: _OperationalEventSpec(
        "info", "Cuota diaria de eBay alcanzada", _body_ebay_quota_breach
    ),
    EventName.llm_provider_rate_limited: _OperationalEventSpec(
        "info", "Proveedor LLM con rate-limit", _body_llm_provider_rate_limited
    ),
    EventName.entry_snoozed: _OperationalEventSpec(
        "info", "Entrada pospuesta", _body_entry_snoozed
    ),
    EventName.poll_cycle_error: _OperationalEventSpec(
        "warn", "Error en el ciclo de sondeo", _body_poll_cycle_error
    ),
    EventName.circuit_open: _OperationalEventSpec(
        "warn", "Fase 2 desactivada globalmente", _body_circuit_open
    ),
    EventName.smoke_test_failed: _OperationalEventSpec(
        "warn", "Smoke test fallido", _body_smoke_test_failed
    ),
    EventName.smoke_test_recovered: _OperationalEventSpec(
        "info", "Smoke test recuperado", _body_smoke_test_recovered
    ),
    EventName.phase2_disabled: _OperationalEventSpec(
        "warn", "Fase 2 desactivada globalmente", _body_phase2_disabled
    ),
    EventName.phase2_re_enabled: _OperationalEventSpec(
        "info", "Fase 2 reactivada", _body_phase2_re_enabled
    ),
    EventName.phase2_buy_callback_received: _OperationalEventSpec(
        "info",
        "Buy callback recibido",
        _body_phase2_buy_callback_received,
    ),
    EventName.phase2_screenshot_missing: _OperationalEventSpec(
        "warn",
        "Captura del recibo no disponible",
        _body_phase2_screenshot_missing,
    ),
    EventName.phase2_buy_completion_slow: _OperationalEventSpec(
        "info",
        "Compra completada con retraso",
        _body_phase2_buy_completion_slow,
    ),
    EventName.buy_orchestrator_error: _OperationalEventSpec(
        "warn",
        "Error en el orquestador de compra",
        _body_buy_orchestrator_error,
    ),
    EventName.offer_lockout_engaged: _OperationalEventSpec(
        "warn", "Envío de ofertas desactivado", _body_offer_lockout_engaged
    ),
    EventName.offer_disabled: _OperationalEventSpec(
        "warn", "Envío de ofertas desactivado", _body_offer_disabled
    ),
    EventName.offer_re_enabled: _OperationalEventSpec(
        "info", "Envío de ofertas reactivado", _body_offer_re_enabled
    ),
    EventName.offer_orchestrator_error: _OperationalEventSpec(
        "warn",
        "Error en el orquestador de ofertas",
        _body_offer_orchestrator_error,
    ),
}


def render_operational_alert(
    severity: Severity,
    event: EventName,
    ctx: Mapping[str, Any],
) -> RenderedAlert:
    """Render a non-listing operational alert (FR21).

    ``warn`` anatomy:
      1. ``⚠️ *<bold headline>*``
      2. blank line
      3. cause line, state line, blank, ``Próximo paso:``, numbered
         copy-paste-ready CLI commands

    ``info`` anatomy:
      1. ``<info-glyph> <plain headline>``
      2. blank line
      3. adapter/context line, optional fallback line, optional single
         CLI hint softened with "cuando puedas"

    Operational alerts never carry buttons or photos —
    :attr:`RenderedAlert.inline_keyboard` and :attr:`~RenderedAlert.photo_url`
    are always ``None`` (FR21).

    ``severity`` must match the event's canonical severity; a mismatch
    is a caller bug and raises :class:`ValueError`. The split exists so
    the call site reads self-documenting (``severity="warn"``) while the
    renderer stays the single source of truth for which events are loud.
    """
    spec = _OPERATIONAL_EVENT_SPECS[event]
    if severity != spec.severity:
        raise ValueError(
            f"event {event.value!r} is a {spec.severity!r} alert, "
            f"not {severity!r} — severity is fixed per UX-DR13"
        )

    headline = _prose(spec.headline)
    if severity == "warn":
        prefix = SEVERITY_TOKENS["operational_warn"]
        row1 = f"{prefix}*{headline}*"
    else:
        prefix = SEVERITY_TOKENS["operational_info"]
        row1 = f"{prefix}{headline}"

    body = spec.build_body(ctx)
    return RenderedAlert(
        text="\n".join([row1, "", *body]),
        parse_mode="MarkdownV2",
        photo_url=None,
        inline_keyboard=None,
    )
