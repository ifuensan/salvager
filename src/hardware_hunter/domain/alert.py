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

from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing

Phase = Literal["phase1", "phase2"]
ParseMode = Literal["MarkdownV2"]

# Telegram caps inline-button callback_data at 64 bytes. The locked format
# is `<surface>:<verb>:<id>` per CALLBACK_DATA_FORMAT in the UX spec.
_CALLBACK_DATA_MAX_BYTES = 64
_CALLBACK_DATA_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+:[A-Za-z0-9_\-]+$")

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
}

#: Inline-keyboard button labels (Spanish per UX-DR27). PRD amendment to grow.
BUTTON_LABELS: Final[dict[str, str]] = {
    "view": "👁 Ver",
    "skip_phase1": "🙅 Saltar",
    "snooze": "😴 Posponer 24h",
    "buy": "✅ Comprar",
    "skip_phase2": "❌ Saltar",
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


CallbackVerb = Literal["view", "skip", "snooze", "buy"]


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


def _format_price_es(amount: Decimal) -> str:
    """Format a EUR Decimal in es-ES style — ``1.234,56 €``."""
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
    return f"{sign}{int_grouped},{decimal_part} €"


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


def render_phase1_listing_alert(snapshot: AlertSnapshot) -> RenderedAlert:
    """Render a Phase 1 listing alert (Direction A + Direction E hybrid).

    Anatomy (direct listing):
      1. ``{📦} *<entry_display_name>* — *<price>*``
      2. ``📍 <location> · <marketplace>``
      3. ``_<one_line_take>_``
      4. ``🔍 Confidence: <low|medium|high>``

    When ``snapshot.evaluation.is_container == True``, two indented
    rows are inserted between row 2 and row 3:
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

    if evaluation.is_container:
        wrapper = escape_markdown_v2(evaluation.wrapper_text or "—")
        extracted = escape_markdown_v2(evaluation.extracted_text or "—")
        rows.append(f"  ↪︎ Wrapper: {wrapper}")
        rows.append(f"  ↪︎ Extracted: {extracted}")

    rows.append(f"_{take}_")
    rows.append(f"🔍 Confidence: {confidence}")

    photo_url = listing.photo_urls[0] if listing.photo_urls else None

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=photo_url,
        inline_keyboard=[_phase1_button_row(str(snapshot.alert_id))],
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
    snapshot: AlertSnapshot, phase2_max_price_eur: Decimal
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

    if evaluation.is_container:
        wrapper = escape_markdown_v2(evaluation.wrapper_text or "—")
        extracted = escape_markdown_v2(evaluation.extracted_text or "—")
        rows.append(f"  ↪︎ Wrapper: {wrapper}")
        rows.append(f"  ↪︎ Extracted: {extracted}")

    rows.append(f"_{take}_")
    rows.append(f"🔍 Confidence: {confidence} · Phase 2 max: {max_price}")

    photo_url = listing.photo_urls[0] if listing.photo_urls else None

    return RenderedAlert(
        text="\n".join(rows),
        parse_mode="MarkdownV2",
        photo_url=photo_url,
        inline_keyboard=[_phase2_button_row(str(snapshot.alert_id))],
    )


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
    # TODO(Epic 5 — Story 5.11): Phase 2 operational variants land here —
    # phase2_disabled_global, phase2_disabled_entry, reconciliation_tripped,
    # smoke_test_drift, circuit_breaker_opened.


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
        _prose("Próximo paso: ")
        + _cmd("hardware-hunter login wallapop")
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
        _prose("Próximo paso:"),
        _prose("1. ") + _cmd("hardware-hunter audit show --last 5"),
        _prose("2. revisa la conexión o parchea el adaptador si persiste"),
        _prose("3. ") + _cmd("docker-compose restart hardware-hunter"),
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
        _prose("Próximo paso:"),
        _prose("1. ") + _cmd("hardware-hunter login ebay --ru-name <tu-runame>"),
        _prose("2. ") + _cmd("docker-compose restart hardware-hunter"),
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


def _body_poll_cycle_error(ctx: Mapping[str, Any]) -> list[str]:
    error_class = str(ctx.get("error_class", "—"))
    marketplace = str(ctx.get("marketplace", "—"))
    return [
        _prose(f"Causa: {error_class} en el ciclo de {marketplace}"),
        _prose("Estado actual: el ciclo continuará en el siguiente tick"),
        "",
        _prose("Próximo paso:"),
        _prose("1. ") + _cmd("hardware-hunter audit show --last 5"),
        _prose("2. ") + _cmd("hardware-hunter logs --last 50"),
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
