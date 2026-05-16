"""Tests for the Phase 1 callback dispatcher — Story 3.13.

The dispatcher composes a :class:`Store` and a :class:`TelegramSurface`;
both are exercised through in-memory fakes so the tests stay fast
and the orchestration logic is the only thing under test.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from typing import Any
from uuid import UUID, uuid4

from hardware_hunter.domain.alert import (
    AlertSnapshot,
    CallbackEvent,
    InlineButton,
    RenderedAlert,
)
from hardware_hunter.domain.audit import (
    CallbackAudit,
    TapEventAudit,
    TransactionAudit,
)
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing
from hardware_hunter.interfaces.store import EntryKey, Store
from hardware_hunter.interfaces.telegram_surface import TelegramSurface
from hardware_hunter.observability import logging as hh_logging
from hardware_hunter.orchestration.callback_handler import (
    ACK_LABELS,
    DEFAULT_SNOOZE_HOURS,
    HANDLED_VERBS,
    CallbackDispatcher,
)

# ─────────────────────────────────────────────────────────────────────────
# Fixtures + fakes
# ─────────────────────────────────────────────────────────────────────────


_FIXED_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def _make_snapshot(
    *,
    alert_id: UUID | None = None,
    entry_key: EntryKey = ("WD", "Red Plus 4TB", "WD40EFPX"),
) -> AlertSnapshot:
    return AlertSnapshot(
        alert_id=alert_id or uuid4(),
        entry_key=entry_key,
        entry_display_name="WD Red Plus 4TB (WD40EFPX)",
        listing=Listing(
            listing_id="lst-001",
            marketplace="wallapop",
            url="https://wallapop.com/item/lst-001",
            title="WD Red Plus 4TB",
            description="like new",
            price_eur=Decimal("55.00"),
            fetched_at=_FIXED_NOW,
        ),
        evaluation=ListingEvaluation(
            listing_id="lst-001",
            entry_key=entry_key,
            confidence="high",
            one_line_take="Strong match at €55.",
            is_container=False,
            evaluated_at=_FIXED_NOW,
        ),
        phase="phase1",
        rendered_at=_FIXED_NOW,
    )


class _FakeStore(Store):
    """In-memory :class:`Store` recording every write."""

    def __init__(self) -> None:
        self.callbacks: list[CallbackAudit] = []
        self.snoozes: list[tuple[EntryKey, datetime]] = []
        self.snapshots: dict[UUID, AlertSnapshot] = {}

    # Dedup state — not exercised here.
    async def is_seen(self, listing_id: str, entry_key: EntryKey) -> bool:
        return False

    async def record_seen(
        self,
        listing: Listing,
        entry_key: EntryKey,
        *,
        match_fired: bool = False,
    ) -> None:
        return None

    # Snooze state.
    async def get_snooze_until(self, entry_key: EntryKey) -> datetime | None:
        for stored_key, until in reversed(self.snoozes):
            if stored_key == entry_key:
                return until
        return None

    async def set_snooze(self, entry_key: EntryKey, until: datetime) -> None:
        self.snoozes.append((entry_key, until))

    # Alert snapshots.
    async def record_alert_snapshot(self, snapshot: AlertSnapshot) -> int:
        self.snapshots[snapshot.alert_id] = snapshot
        return len(self.snapshots)

    async def get_alert_snapshot(self, audit_id: int) -> AlertSnapshot | None:
        return None  # not used by the dispatcher

    async def get_alert_snapshot_by_alert_id(self, alert_id: UUID) -> AlertSnapshot | None:
        return self.snapshots.get(alert_id)

    async def record_callback(self, callback: CallbackAudit) -> None:
        self.callbacks.append(callback)

    # _meta — not exercised here.
    async def set_meta(self, key: str, value: str) -> None:
        return None

    async def get_meta(self, key: str) -> str | None:
        return None

    async def get_all_meta(self) -> dict[str, str]:
        return {}

    # Phase 2 — never invoked here.
    async def record_tap_event(self, tap: TapEventAudit) -> None:
        raise AssertionError("Phase 2 audit should not run in Story 3.13 tests")

    async def record_transaction(self, transaction: TransactionAudit) -> None:
        raise AssertionError("Phase 2 audit should not run in Story 3.13 tests")


class _FakeSurface(TelegramSurface):
    """In-memory :class:`TelegramSurface` recording edit_keyboard calls."""

    def __init__(self) -> None:
        self.edits: list[tuple[int, list[list[InlineButton]] | None]] = []
        self.send_calls: list[RenderedAlert] = []

    async def send(self, rendered: RenderedAlert) -> int:
        self.send_calls.append(rendered)
        return 999

    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:
        self.edits.append((message_id, keyboard))

    async def listen_callbacks(self, handler: Callable[[CallbackEvent], Awaitable[None]]) -> None:
        _ = handler
        raise AssertionError("listen_callbacks not exercised in Story 3.13")


def _frozen_clock() -> datetime:
    return _FIXED_NOW


_FIXED_AUDIT_ID = UUID("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa")


def _fixed_audit_id() -> UUID:
    return _FIXED_AUDIT_ID


class _FakeBuyOrchestrator:
    """Records every ``execute_buy_from_callback`` call.

    The constructor accepts a ``raises`` exception to simulate an
    orchestrator that explodes inside the background task — the
    dispatcher must not let that propagate.
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.received_events: list[CallbackEvent] = []
        self._raises = raises
        self._done = asyncio.Event()

    async def execute_buy_from_callback(self, event: CallbackEvent) -> object:
        try:
            self.received_events.append(event)
            if self._raises is not None:
                raise self._raises
            return "ok"
        finally:
            self._done.set()

    async def wait_for_completion(self) -> None:
        """Yield once so the dispatcher's create_task can advance.
        Tests call this right after ``await dispatcher.handle(event)``
        so the assertions see the orchestrator's recorded call."""
        await self._done.wait()


def _make_dispatcher(
    store: _FakeStore,
    surface: _FakeSurface,
    *,
    snooze_hours: int = DEFAULT_SNOOZE_HOURS,
    buy_orchestrator: _FakeBuyOrchestrator | None = None,
) -> CallbackDispatcher:
    return CallbackDispatcher(
        store=store,
        surface=surface,
        buy_orchestrator=buy_orchestrator,
        snooze_hours=snooze_hours,
        clock=_frozen_clock,
        new_audit_id=_fixed_audit_id,
    )


def _callback_event(
    *,
    verb: str,
    alert_id: UUID,
    chat_id: int = 12345,
    message_id: int = 42,
) -> CallbackEvent:
    return CallbackEvent(
        callback_query_id="cbq-1",
        chat_id=chat_id,
        message_id=message_id,
        callback_data=f"listing:{verb}:{alert_id}",
        verb=verb,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────────────────
# Happy paths — view / skip / snooze
# ─────────────────────────────────────────────────────────────────────────


async def test_view_records_audit_and_edits_to_visto_row() -> None:
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface)

    event = _callback_event(verb="view", alert_id=snapshot.alert_id)
    await dispatcher.handle(event)

    assert len(store.callbacks) == 1
    callback = store.callbacks[0]
    assert callback.verb == "view"
    assert callback.alert_id == snapshot.alert_id
    assert callback.telegram_message_id == event.message_id
    assert callback.chat_id == event.chat_id
    assert callback.occurred_at == _FIXED_NOW

    # No state mutation for view.
    assert store.snoozes == []

    # Keyboard replaced with a single-row [✓ visto].
    assert len(surface.edits) == 1
    edited_message_id, keyboard = surface.edits[0]
    assert edited_message_id == event.message_id
    assert keyboard is not None
    assert len(keyboard) == 1 and len(keyboard[0]) == 1
    assert keyboard[0][0].text == ACK_LABELS["view"]
    assert keyboard[0][0].text == "✓ visto"


async def test_skip_records_audit_and_edits_to_saltado_row() -> None:
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface)

    event = _callback_event(verb="skip", alert_id=snapshot.alert_id)
    await dispatcher.handle(event)

    assert store.callbacks[-1].verb == "skip"
    # No state mutation for skip — entry stays watched.
    assert store.snoozes == []
    assert surface.edits[-1][1] is not None
    assert surface.edits[-1][1][0][0].text == "✓ saltado"


async def test_snooze_writes_snooze_until_and_edits_to_pospuesto_row() -> None:
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface)

    event = _callback_event(verb="snooze", alert_id=snapshot.alert_id)
    await dispatcher.handle(event)

    # Audit row written.
    assert store.callbacks[-1].verb == "snooze"

    # snooze_until = now + 24h on the matching entry_key.
    assert len(store.snoozes) == 1
    entry_key, until = store.snoozes[0]
    assert entry_key == snapshot.entry_key
    assert until == _FIXED_NOW + timedelta(hours=DEFAULT_SNOOZE_HOURS)

    # Keyboard becomes [✓ pospuesto 24h].
    assert surface.edits[-1][1] is not None
    assert surface.edits[-1][1][0][0].text == "✓ pospuesto 24h"


async def test_snooze_uses_configured_snooze_hours() -> None:
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface, snooze_hours=6)

    event = _callback_event(verb="snooze", alert_id=snapshot.alert_id)
    await dispatcher.handle(event)

    _, until = store.snoozes[0]
    assert until == _FIXED_NOW + timedelta(hours=6)


# ─────────────────────────────────────────────────────────────────────────
# Ordering + reactiveness
# ─────────────────────────────────────────────────────────────────────────


async def test_audit_row_written_before_keyboard_edit() -> None:
    """Audit row must land first so a failed edit doesn't lose the tap."""
    snapshot = _make_snapshot()
    surface = _FakeSurface()

    class _OrderRecordingStore(_FakeStore):
        def __init__(self, surface_ref: _FakeSurface) -> None:
            super().__init__()
            self._surface_ref = surface_ref
            self.order: list[str] = []

        async def record_callback(self, callback: CallbackAudit) -> None:
            self.order.append(f"audit:{len(self._surface_ref.edits)}")
            await super().record_callback(callback)

    store = _OrderRecordingStore(surface)
    store.snapshots[snapshot.alert_id] = snapshot

    original_edit = surface.edit_keyboard

    async def _edit(message_id: int, keyboard: list[list[InlineButton]] | None) -> None:
        store.order.append(f"edit:{len(store.callbacks)}")
        await original_edit(message_id, keyboard)

    surface.edit_keyboard = _edit  # type: ignore[method-assign]
    dispatcher = _make_dispatcher(store, surface)

    await dispatcher.handle(_callback_event(verb="view", alert_id=snapshot.alert_id))

    # Audit recorded when 0 edits had happened; edit recorded when 1 audit row existed.
    assert store.order == ["audit:0", "edit:1"]


# ─────────────────────────────────────────────────────────────────────────
# Unknown verb / malformed data
# ─────────────────────────────────────────────────────────────────────────


async def test_buy_verb_is_in_handled_verbs() -> None:
    assert "buy" in HANDLED_VERBS
    assert frozenset({"view", "skip", "snooze", "buy"}) == HANDLED_VERBS


async def test_buy_verb_edits_keyboard_to_comprando_and_fires_orchestrator() -> None:
    """Story 5.10: a Buy tap immediately swaps the keyboard for the
    ``[🟡 Comprando…]`` badge and fires the orchestrator as a
    background task. The dispatcher returns without awaiting the
    orchestrator's completion."""
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    orchestrator = _FakeBuyOrchestrator()
    dispatcher = _make_dispatcher(store, surface, buy_orchestrator=orchestrator)

    event = _callback_event(verb="buy", alert_id=snapshot.alert_id)
    await dispatcher.handle(event)
    await orchestrator.wait_for_completion()

    # The audit row landed.
    assert len(store.callbacks) == 1
    assert store.callbacks[0].verb == "buy"
    # The keyboard was edited exactly once to the in-flight badge.
    assert len(surface.edits) == 1
    message_id, keyboard = surface.edits[0]
    assert message_id == event.message_id
    assert keyboard is not None
    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    badge = keyboard[0][0]
    assert badge.text == "🟡 Comprando…"
    assert badge.callback_data == f"listing:noop:{snapshot.alert_id}"
    # The orchestrator was fired with the original event.
    assert orchestrator.received_events == [event]


async def test_buy_in_flight_keyboard_callback_data_passes_validator() -> None:
    """The in-flight badge's callback_data must fit the locked
    ``<surface>:<verb>:<id>`` format + the 64-byte Telegram cap."""
    alert_id = uuid4()
    button = InlineButton(text="🟡 Comprando…", callback_data=f"listing:noop:{alert_id}")
    assert button.callback_data.encode("utf-8").__len__() <= 64


async def test_buy_without_orchestrator_writes_audit_and_badge_but_no_task() -> None:
    """If the daemon is misconfigured (no buy orchestrator wired), the
    dispatcher MUST still leave the audit + badge in place — silence
    is not an option, and the operator can debug from there."""
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface, buy_orchestrator=None)

    event = _callback_event(verb="buy", alert_id=snapshot.alert_id)
    await dispatcher.handle(event)

    assert len(store.callbacks) == 1
    assert len(surface.edits) == 1
    assert surface.edits[0][1] is not None  # badge present
    # No way to assert "no task started"; the absence of an orchestrator
    # means there is nothing to call. The structured-log message
    # ``buy_orchestrator_not_wired`` covers the operator-facing breadcrumb.


async def test_buy_emits_phase2_buy_callback_received_log() -> None:
    """Story 5.10 — the dispatcher logs ``phase2_buy_callback_received``
    on every Buy tap so operational dashboards can count taps even
    when the orchestrator is slow / absent."""
    out = _run_subprocess_logging_handle_buy()
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    received = [r for r in records if r["event"] == "phase2_buy_callback_received"]
    assert received, f"missing phase2_buy_callback_received in {records!r}"
    assert received[0]["level"] == "info"


async def test_buy_orchestrator_failure_does_not_break_dispatcher() -> None:
    """The orchestrator runs as a background task — a raise inside it
    must not propagate to the dispatcher's caller. The badge stays
    edited; the audit row stays written."""
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    orchestrator = _FakeBuyOrchestrator(raises=RuntimeError("orchestrator boom"))
    dispatcher = _make_dispatcher(store, surface, buy_orchestrator=orchestrator)

    event = _callback_event(verb="buy", alert_id=snapshot.alert_id)
    # The handle() call itself returns cleanly even though the task raises.
    await dispatcher.handle(event)
    await orchestrator.wait_for_completion()

    assert len(store.callbacks) == 1
    assert len(surface.edits) == 1


async def test_unhandled_verb_logs_callback_unknown_verb_event() -> None:
    """The structured log records the unknown verb at warn level."""
    out = _run_subprocess_logging_handle_unknown_verb()
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    unknown = [r for r in records if r["event"] == "callback_unknown_verb"]
    assert unknown, f"missing callback_unknown_verb in {records!r}"
    assert unknown[0]["level"] == "warn"
    assert unknown[0]["verb"] == "archive"


# ─────────────────────────────────────────────────────────────────────────
# Snooze edge: snapshot vanished (e.g. operator tapped old message)
# ─────────────────────────────────────────────────────────────────────────


async def test_snooze_with_missing_snapshot_records_audit_but_skips_state() -> None:
    store = _FakeStore()  # no snapshot inserted
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface)

    event = _callback_event(verb="snooze", alert_id=uuid4())
    await dispatcher.handle(event)

    # Audit still landed — the tap happened.
    assert len(store.callbacks) == 1
    assert store.callbacks[0].verb == "snooze"
    # State not mutated because we couldn't resolve the entry_key.
    assert store.snoozes == []
    # Keyboard still updated so the operator gets visual feedback.
    assert len(surface.edits) == 1


# ─────────────────────────────────────────────────────────────────────────
# Ack-row callback_data shape + non-tappability via unknown-verb drop
# ─────────────────────────────────────────────────────────────────────────


async def test_ack_keyboard_uses_listing_ack_callback_data() -> None:
    """The ack row carries ``listing:ack:<alert_id>``; the surface
    layer drops ``ack`` as an unknown verb so the row is effectively
    non-tappable even though Telegram requires a callback_data."""
    snapshot = _make_snapshot()
    store = _FakeStore()
    store.snapshots[snapshot.alert_id] = snapshot
    surface = _FakeSurface()
    dispatcher = _make_dispatcher(store, surface)

    await dispatcher.handle(_callback_event(verb="view", alert_id=snapshot.alert_id))

    keyboard = surface.edits[-1][1]
    assert keyboard is not None
    button = keyboard[0][0]
    assert button.callback_data == f"listing:ack:{snapshot.alert_id}"


async def test_ack_keyboard_button_passes_inline_button_validator() -> None:
    """Validates the locked <surface>:<verb>:<id> format + 64-byte cap."""
    alert_id = uuid4()
    # Constructing through InlineButton re-runs the validator on the
    # exact string the dispatcher emits.
    button = InlineButton(text="✓ visto", callback_data=f"listing:ack:{alert_id}")
    assert button.callback_data.encode("utf-8").__len__() <= 64


# ─────────────────────────────────────────────────────────────────────────
# Structured-log assertions for entry_snoozed
# ─────────────────────────────────────────────────────────────────────────


async def test_snooze_emits_entry_snoozed_operational_event() -> None:
    out = _run_subprocess_logging_handle_snooze()
    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    snoozed = [r for r in records if r["event"] == "entry_snoozed"]
    assert snoozed, f"missing entry_snoozed in {records!r}"
    assert snoozed[0]["entry_manufacturer"] == "WD"
    assert snoozed[0]["entry_model"] == "Red Plus 4TB"
    assert snoozed[0]["entry_ref"] == "WD40EFPX"
    assert snoozed[0]["snooze_hours"] == DEFAULT_SNOOZE_HOURS


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline: orchestration must stay pure
# ─────────────────────────────────────────────────────────────────────────


def test_callback_handler_imports_stay_within_orchestration_allowlist() -> None:
    """The orchestration layer never imports an adapter or external SDK.

    AST-walks the module and asserts every import resolves to stdlib,
    pydantic, or the four pure-tier packages (domain, interfaces,
    observability, orchestration).
    """
    import ast
    from pathlib import Path

    source_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "hardware_hunter"
        / "orchestration"
        / "callback_handler.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    allowed_first_segments = {
        # stdlib
        "__future__",
        "abc",
        "asyncio",  # Story 5.10 — fire-and-forget orchestrator task
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "logging",
        "typing",
        "uuid",
        # third-party we treat as pure-tier
        "pydantic",
        # project pure tiers
        "hardware_hunter",
    }
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in allowed_first_segments:
                    bad.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if top not in allowed_first_segments:
                bad.append(f"from {module} import ...")
            if module.startswith("hardware_hunter.adapters"):
                bad.append(f"orchestration must not import adapters: from {module} import ...")
    assert not bad, "orchestration import discipline violated:\n  " + "\n  ".join(bad)


# ─────────────────────────────────────────────────────────────────────────
# Helpers that capture structured-log output via subprocess
# ─────────────────────────────────────────────────────────────────────────


_SUBPROCESS_BOOTSTRAP = (
    "import asyncio, json, sys\n"
    "from datetime import UTC, datetime, timedelta\n"
    "from decimal import Decimal\n"
    "from uuid import UUID\n"
    "from hardware_hunter.domain.alert import CallbackEvent\n"
    "from hardware_hunter.domain.listing import Listing\n"
    "from hardware_hunter.domain.evaluation import ListingEvaluation\n"
    "from hardware_hunter.domain.alert import AlertSnapshot\n"
    "from hardware_hunter.orchestration.callback_handler import CallbackDispatcher\n"
)


def _run_subprocess(snippet: str) -> str:
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_BOOTSTRAP + snippet],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def _run_subprocess_logging_handle_unknown_verb() -> str:
    """Drive the defence-in-depth unknown-verb path. The :class:`CallbackVerb`
    Literal blocks ad-hoc verbs at validation; we use ``model_construct``
    to bypass — the same shape a misbehaving adapter could feed us."""
    snippet = (
        "from hardware_hunter.interfaces.store import Store\n"
        "from hardware_hunter.interfaces.telegram_surface import TelegramSurface\n"
        "class _S(Store):\n"
        "    async def is_seen(self, *a, **kw): return False\n"
        "    async def record_seen(self, *a, **kw): pass\n"
        "    async def get_snooze_until(self, *a, **kw): return None\n"
        "    async def set_snooze(self, *a, **kw): pass\n"
        "    async def record_alert_snapshot(self, *a, **kw): return 1\n"
        "    async def get_alert_snapshot(self, *a, **kw): return None\n"
        "    async def get_alert_snapshot_by_alert_id(self, *a, **kw): return None\n"
        "    async def record_callback(self, *a, **kw): pass\n"
        "    async def set_meta(self, *a, **kw): pass\n"
        "    async def get_meta(self, *a, **kw): return None\n"
        "    async def get_all_meta(self, *a, **kw): return {}\n"
        "    async def record_tap_event(self, *a, **kw): pass\n"
        "    async def record_transaction(self, *a, **kw): pass\n"
        "class _T(TelegramSurface):\n"
        "    async def send(self, *a, **kw): return 1\n"
        "    async def edit_keyboard(self, *a, **kw): pass\n"
        "    async def listen_callbacks(self, *a, **kw): pass\n"
        "async def main():\n"
        "    d = CallbackDispatcher(store=_S(), surface=_T())\n"
        "    e = CallbackEvent.model_construct(\n"
        "        callback_query_id='cbq-1', chat_id=1, message_id=1,\n"
        "        callback_data='listing:archive:" + str(uuid4()) + "', verb='archive')\n"
        "    await d.handle(e)\n"
        "asyncio.run(main())\n"
    )
    return _run_subprocess(snippet)


def _run_subprocess_logging_handle_buy() -> str:
    """Drive the Buy path through a structured-log subprocess so we can
    assert on the ``phase2_buy_callback_received`` JSON line. No
    orchestrator is wired — the dispatcher still emits the log and
    the badge, which is all this test cares about."""
    snippet = (
        "from hardware_hunter.interfaces.store import Store\n"
        "from hardware_hunter.interfaces.telegram_surface import TelegramSurface\n"
        "class _S(Store):\n"
        "    async def is_seen(self, *a, **kw): return False\n"
        "    async def record_seen(self, *a, **kw): pass\n"
        "    async def get_snooze_until(self, *a, **kw): return None\n"
        "    async def set_snooze(self, *a, **kw): pass\n"
        "    async def record_alert_snapshot(self, *a, **kw): return 1\n"
        "    async def get_alert_snapshot(self, *a, **kw): return None\n"
        "    async def get_alert_snapshot_by_alert_id(self, *a, **kw): return None\n"
        "    async def record_callback(self, *a, **kw): pass\n"
        "    async def set_meta(self, *a, **kw): pass\n"
        "    async def get_meta(self, *a, **kw): return None\n"
        "    async def get_all_meta(self, *a, **kw): return {}\n"
        "    async def record_tap_event(self, *a, **kw): pass\n"
        "    async def record_transaction(self, *a, **kw): pass\n"
        "class _T(TelegramSurface):\n"
        "    async def send(self, *a, **kw): return 1\n"
        "    async def edit_keyboard(self, *a, **kw): pass\n"
        "    async def listen_callbacks(self, *a, **kw): pass\n"
        "async def main():\n"
        "    d = CallbackDispatcher(store=_S(), surface=_T())\n"
        "    e = CallbackEvent(\n"
        "        callback_query_id='cbq-1', chat_id=1, message_id=1,\n"
        "        callback_data='listing:buy:" + str(uuid4()) + "', verb='buy')\n"
        "    await d.handle(e)\n"
        "asyncio.run(main())\n"
    )
    return _run_subprocess(snippet)


def _run_subprocess_logging_handle_snooze() -> str:
    alert_id = uuid4()
    snippet = (
        "from hardware_hunter.interfaces.store import Store\n"
        "from hardware_hunter.interfaces.telegram_surface import TelegramSurface\n"
        f"ALERT_ID = UUID('{alert_id}')\n"
        "SNAP = AlertSnapshot(\n"
        "    alert_id=ALERT_ID,\n"
        "    entry_key=('WD','Red Plus 4TB','WD40EFPX'),\n"
        "    entry_display_name='WD Red Plus 4TB',\n"
        "    listing=Listing(\n"
        "        listing_id='lst-001', marketplace='wallapop',\n"
        "        url='https://wallapop.com/i/x', title='WD',\n"
        "        description='ok', price_eur=Decimal('55.00'),\n"
        "        fetched_at=datetime(2026,5,13,12,0,0,tzinfo=UTC)),\n"
        "    evaluation=ListingEvaluation(\n"
        "        listing_id='lst-001',\n"
        "        entry_key=('WD','Red Plus 4TB','WD40EFPX'),\n"
        "        confidence='high', one_line_take='ok',\n"
        "        is_container=False,\n"
        "        evaluated_at=datetime(2026,5,13,12,0,0,tzinfo=UTC)),\n"
        "    phase='phase1',\n"
        "    rendered_at=datetime(2026,5,13,12,0,0,tzinfo=UTC),\n"
        ")\n"
        "class _S(Store):\n"
        "    async def is_seen(self, *a, **kw): return False\n"
        "    async def record_seen(self, *a, **kw): pass\n"
        "    async def get_snooze_until(self, *a, **kw): return None\n"
        "    async def set_snooze(self, *a, **kw): pass\n"
        "    async def record_alert_snapshot(self, *a, **kw): return 1\n"
        "    async def get_alert_snapshot(self, *a, **kw): return None\n"
        "    async def get_alert_snapshot_by_alert_id(self, *a, **kw): return SNAP\n"
        "    async def record_callback(self, *a, **kw): pass\n"
        "    async def set_meta(self, *a, **kw): pass\n"
        "    async def get_meta(self, *a, **kw): return None\n"
        "    async def get_all_meta(self, *a, **kw): return {}\n"
        "    async def record_tap_event(self, *a, **kw): pass\n"
        "    async def record_transaction(self, *a, **kw): pass\n"
        "class _T(TelegramSurface):\n"
        "    async def send(self, *a, **kw): return 1\n"
        "    async def edit_keyboard(self, *a, **kw): pass\n"
        "    async def listen_callbacks(self, *a, **kw): pass\n"
        "async def main():\n"
        "    d = CallbackDispatcher(store=_S(), surface=_T())\n"
        "    e = CallbackEvent(\n"
        "        callback_query_id='cbq-1', chat_id=1, message_id=1,\n"
        f"        callback_data=f'listing:snooze:{alert_id}', verb='snooze')\n"
        "    await d.handle(e)\n"
        "asyncio.run(main())\n"
    )
    return _run_subprocess(snippet)


# Silence unused-import warnings from the subprocess bootstrap helpers.
_ = (Any, StringIO, hh_logging)
