"""Telegram callback dispatcher — Stories 3.13 + 5.10.

Wires inbound :class:`CallbackEvent` taps to the per-verb effects:

  - **view** / **skip** / **snooze** (Phase 1, Story 3.13):
    append a row to the ``callbacks`` audit table, mutate snooze state
    if applicable, and replace the inline keyboard with the locked
    acknowledgment row (``[✓ visto] / [✓ saltado] / [✓ pospuesto 24h]``)
    per UX-DR12.
  - **buy** (Phase 2, Story 5.10):
    append the audit row, immediately edit the keyboard to a single
    non-tappable ``[🟡 Comprando…]`` row (UX-DR11), then fire the
    :class:`BuyOrchestrator` in the background. The orchestrator
    dispatches the receipt or failure message as a NEW Telegram message
    so the original alert's ``🟡 Comprando…`` is preserved as the
    "what happened" history (per UX-DR17 — no spinner).

The dispatcher depends only on the :class:`Store`, :class:`TelegramSurface`
and :class:`BuyExecutor` ports — never on a specific adapter. The
poll-loop orchestrator wires it up with the live surface via
``TelegramSurface.listen_callbacks``.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID

from salvager.domain.alert import CallbackEvent, InlineButton
from salvager.domain.audit import CallbackAudit
from salvager.interfaces.store import Store
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.observability.logging import get_logger

#: Verbs the dispatcher acts on. Anything else logs at warn level and
#: is dropped silently — the surface layer (``TelegramBotSurface``)
#: should have already filtered, this is defence in depth.
HANDLED_VERBS: Final[frozenset[str]] = frozenset({"view", "skip", "snooze", "buy"})

#: Phase 1 acknowledgment-row labels (UX-DR12). Spanish past-participles
#: match :data:`BUTTON_LABELS`' present-tense verbs (Ver → visto, Saltar →
#: saltado, Posponer → pospuesto). ``buy`` is intentionally absent — its
#: in-flight badge is :data:`BUY_IN_FLIGHT_LABEL`, not an ack row.
ACK_LABELS: Final[dict[str, str]] = {
    "view": "✓ visto",
    "skip": "✓ saltado",
    "snooze": "✓ pospuesto 24h",
}

#: The non-tappable in-flight badge shown on the original alert while
#: the buy orchestrator runs (UX-DR11). The yellow circle is the
#: locked surface token for "in progress" and the trailing ellipsis is
#: a U+2026 character (no MarkdownV2 escape concerns — it's the button
#: text, not the message body).
BUY_IN_FLIGHT_LABEL: Final[str] = "🟡 Comprando…"

#: Default snooze window. The orchestrator can override via
#: ``snooze_hours`` to wire ``config.yaml > snooze.default_hours``.
DEFAULT_SNOOZE_HOURS: Final[int] = 24


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BuyExecutor(Protocol):
    """Structural type for "something that can run a Phase 2 buy".

    The dispatcher depends on this Protocol, not on the concrete
    :class:`BuyOrchestrator`, so unit tests pass a recording fake and
    the orchestration layer keeps its one-way dependency shape.
    """

    async def execute_buy_from_callback(self, event: CallbackEvent) -> object: ...


class CallbackDispatcher:
    """Routes Phase 1 + Phase 2 callbacks to audit + state + keyboard edits.

    The dispatcher is stateless across callbacks; multiple in-flight
    taps are safe to interleave because every effect is serialized
    inside the :class:`Store` implementation's write lock. For
    ``buy`` taps the orchestrator runs as a background task — the
    dispatcher keeps a reference to each task in ``_buy_tasks`` so
    the asyncio garbage collector doesn't cancel them mid-flight, and
    drops them as they complete.
    """

    def __init__(
        self,
        *,
        store: Store,
        surface: TelegramSurface,
        buy_orchestrator: BuyExecutor | None = None,
        snooze_hours: int = DEFAULT_SNOOZE_HOURS,
        clock: Callable[[], datetime] = _utc_now,
        new_audit_id: Callable[[], UUID] = uuid_module.uuid4,
    ) -> None:
        self._store = store
        self._surface = surface
        self._buy_orchestrator = buy_orchestrator
        self._snooze_hours = snooze_hours
        self._clock = clock
        self._new_audit_id = new_audit_id
        self._log = get_logger("orchestration.callback_handler")
        self._buy_tasks: set[asyncio.Task[object]] = set()

    async def handle(self, event: CallbackEvent) -> None:
        """Process a single callback tap end-to-end.

        Ordering: audit row first, then any state mutation, then the
        keyboard edit. The keyboard edit is last so a delivery failure
        there doesn't lose the audit trail. For ``buy`` the orchestrator
        is fired *after* the keyboard edit so the operator's "tap
        registered" badge appears within 1 s of the tap regardless of
        downstream latency.
        """
        if event.verb not in HANDLED_VERBS:
            self._log.warning(
                "callback_unknown_verb",
                extra={"verb": event.verb, "callback_data": event.callback_data},
            )
            return

        try:
            alert_id = _alert_id_from_callback_data(event.callback_data)
        except ValueError:
            self._log.warning(
                "callback_malformed_callback_data",
                extra={"callback_data": event.callback_data},
            )
            return

        now = self._clock()
        await self._store.record_callback(
            CallbackAudit(
                audit_id=self._new_audit_id(),
                alert_id=alert_id,
                telegram_message_id=event.message_id,
                callback_data=event.callback_data,
                verb=event.verb,
                chat_id=event.chat_id,
                occurred_at=now,
            )
        )

        if event.verb == "buy":
            await self._handle_buy(event, alert_id)
            return

        if event.verb == "snooze":
            await self._apply_snooze(alert_id, now)

        await self._surface.edit_keyboard(
            event.message_id,
            _acknowledgment_keyboard(event.verb, alert_id),
        )
        # Observability: the previous code logged only failures (unknown
        # verb / malformed callback_data) and the Phase 2 buy branch.
        # view/skip/snooze landed silently — the only signal an operator
        # had that the daemon was actually processing taps was the
        # SQLite ``callbacks`` table. Surfaced after the first live
        # smoke test: green keyboard + green audit row + zero log lines
        # made the daemon feel broken even when it wasn't.
        self._log.info(
            "callback_handled",
            extra={
                "verb": event.verb,
                "alert_id": str(alert_id),
                "telegram_message_id": event.message_id,
            },
        )

    async def _handle_buy(self, event: CallbackEvent, alert_id: UUID) -> None:
        """Phase 2 buy verb: in-flight badge + fire-and-forget orchestrator."""
        self._log.info(
            "phase2_buy_callback_received",
            extra={"alert_id": str(alert_id), "callback_data": event.callback_data},
        )
        await self._surface.edit_keyboard(
            event.message_id,
            _in_flight_keyboard(alert_id),
        )
        if self._buy_orchestrator is None:
            # Defence-in-depth — the daemon should always wire an
            # orchestrator at v1.0, but a misconfigured deploy should
            # still leave the audit trail + badge in place.
            self._log.error(
                "buy_orchestrator_not_wired",
                extra={"alert_id": str(alert_id)},
            )
            return
        task = asyncio.create_task(self._buy_orchestrator.execute_buy_from_callback(event))
        self._buy_tasks.add(task)
        task.add_done_callback(self._buy_tasks.discard)

    async def _apply_snooze(self, alert_id: UUID, now: datetime) -> None:
        snapshot = await self._store.get_alert_snapshot_by_alert_id(alert_id)
        if snapshot is None:
            # The alert pre-dates the current DB (operator tapped an
            # old message after a wipe/restore). Audit still records
            # the tap; the visual ack still shows. Just no state.
            self._log.warning(
                "callback_snapshot_missing",
                extra={"alert_id": str(alert_id)},
            )
            return

        until = now + timedelta(hours=self._snooze_hours)
        await self._store.set_snooze(snapshot.entry_key, until)
        self._log.info(
            "entry_snoozed",
            extra={
                "entry_manufacturer": snapshot.entry_key[0],
                "entry_model": snapshot.entry_key[1],
                "entry_ref": snapshot.entry_key[2],
                "snooze_until": until.isoformat(),
                "snooze_hours": self._snooze_hours,
            },
        )


def _alert_id_from_callback_data(callback_data: str) -> UUID:
    """Extract the UUID from a ``<surface>:<verb>:<id>`` callback_data.

    Raises :class:`ValueError` when the shape is wrong or the id
    segment is not a valid UUID. The Telegram surface
    (``TelegramBotSurface.parse_callback``) already drops malformed
    data — this is defense in depth so a faulty caller can't crash
    the dispatcher.
    """
    parts = callback_data.split(":")
    if len(parts) != 3:
        raise ValueError(f"expected 3 segments, got {len(parts)}")
    return UUID(parts[2])


def _acknowledgment_keyboard(verb: str, alert_id: UUID) -> list[list[InlineButton]]:
    """Build the single-row acknowledgment keyboard (UX-DR12).

    ``callback_data`` uses the surface-locked ``listing:ack:<id>``
    form. ``ack`` is deliberately outside the
    :class:`TelegramBotSurface` known-verb set, so any stray future
    tap is dropped silently at the surface layer — the row is
    visually a status badge, not a button.
    """
    return [
        [
            InlineButton(
                text=ACK_LABELS[verb],
                callback_data=f"listing:ack:{alert_id}",
            )
        ]
    ]


def _in_flight_keyboard(alert_id: UUID) -> list[list[InlineButton]]:
    """Build the single-row in-flight keyboard shown while the buy
    orchestrator runs (Story 5.10, UX-DR11).

    Telegram requires a non-empty ``callback_data``; we use
    ``listing:noop:<alert_id>`` which fits the locked
    ``<surface>:<verb>:<id>`` format. ``noop`` is outside
    :data:`HANDLED_VERBS`, so a stray tap is dropped at this
    dispatcher with a structured-log warning.
    """
    return [
        [
            InlineButton(
                text=BUY_IN_FLIGHT_LABEL,
                callback_data=f"listing:noop:{alert_id}",
            )
        ]
    ]


__all__ = [
    "ACK_LABELS",
    "BUY_IN_FLIGHT_LABEL",
    "DEFAULT_SNOOZE_HOURS",
    "HANDLED_VERBS",
    "BuyExecutor",
    "CallbackDispatcher",
]
