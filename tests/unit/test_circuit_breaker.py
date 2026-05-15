"""Tests for the persistence-backed :class:`CircuitBreaker` — Story 5.5.

The pure state-machine is exercised in ``test_circuit_domain.py``; this
module asserts the orchestrator wires it correctly:

  - every outcome persists the counter via :class:`Phase2AuditWriter`;
  - the threshold-crossing failure also fires ``set_global_disable``
    and dispatches the ``circuit_open`` operational alert ONCE;
  - subsequent outcomes against the latched-open state never re-fire
    the alert and never re-set the disable flag;
  - a success while latched-open still resets the counter (the lockout
    flag is independent — only ``clear_global_disable`` lifts it);
  - the breaker carries no in-memory state, so a "restart" (new
    instance) picks up the persisted state and behaves consistently.

A real :class:`Phase2AuditWriter` runs against an in-memory-ish temp DB
so the persistence round-trip is genuine; the reporter is a small fake.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from hardware_hunter.adapters.sqlite_store import (
    MigrationRunner,
    Phase2AuditWriter,
    open_connection,
)
from hardware_hunter.adapters.sqlite_store.migrations import db_path_under
from hardware_hunter.adapters.sqlite_store.phase2_state_reader import (
    SqlitePhase2StateReader,
)
from hardware_hunter.domain.alert import EventName
from hardware_hunter.orchestration.circuit_breaker import (
    CIRCUIT_OPEN_REASON,
    CircuitBreaker,
)
from hardware_hunter.orchestration.degradation_reporter import Reporter

_THRESHOLD = 3


class _RecordingReporter(Reporter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, EventName, dict[str, Any]]] = []

    async def report(
        self,
        severity: str,
        event: EventName,
        ctx: Mapping[str, Any],
    ) -> None:
        self.calls.append((severity, event, dict(ctx)))


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    return db_path


async def _make_breaker(
    db_path: Path, reporter: Reporter
) -> tuple[CircuitBreaker, Phase2AuditWriter, SqlitePhase2StateReader]:
    writer = Phase2AuditWriter(db_path)
    reader = SqlitePhase2StateReader(db_path)
    breaker = CircuitBreaker(
        audit_writer=writer,
        state_reader=reader,
        reporter=reporter,
        threshold=_THRESHOLD,
    )
    return breaker, writer, reader


# ─────────────────────────────────────────────────────────────────────────
# Closed-side transitions
# ─────────────────────────────────────────────────────────────────────────


async def test_failure_below_threshold_only_increments_counter(
    migrated_db: Path,
) -> None:
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        for _ in range(2):
            await breaker.record_outcome("failure")
        state = await reader.read()
        assert state.consecutive_failures == 2
        assert state.globally_disabled is False
        assert reporter.calls == []
    finally:
        await writer.close()
        await reader.close()


async def test_success_resets_counter(migrated_db: Path) -> None:
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        await breaker.record_outcome("failure")
        await breaker.record_outcome("failure")
        await breaker.record_outcome("success")
        state = await reader.read()
        assert state.consecutive_failures == 0
        assert state.globally_disabled is False
    finally:
        await writer.close()
        await reader.close()


# ─────────────────────────────────────────────────────────────────────────
# Threshold crossing → lockout + alert
# ─────────────────────────────────────────────────────────────────────────


async def test_third_failure_opens_circuit_and_fires_alert(migrated_db: Path) -> None:
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        decisions = []
        for _ in range(_THRESHOLD):
            decisions.append(
                await breaker.record_outcome(
                    "failure", last_affected_entry="WD Red Plus 4TB / WD40EFPX"
                )
            )

        # Only the third failure flips just_opened.
        assert [d.just_opened for d in decisions] == [False, False, True]
        assert [d.state for d in decisions] == ["closed", "closed", "open"]

        state = await reader.read()
        assert state.globally_disabled is True
        assert state.disabled_reason == CIRCUIT_OPEN_REASON
        assert state.consecutive_failures == _THRESHOLD

        # Exactly one operational alert, with the locked event + ctx.
        assert len(reporter.calls) == 1
        severity, event, ctx = reporter.calls[0]
        assert severity == "warn"
        assert event is EventName.circuit_open
        assert ctx["consecutive_failures"] == _THRESHOLD
        assert ctx["threshold"] == _THRESHOLD
        assert ctx["last_affected_entry"] == "WD Red Plus 4TB / WD40EFPX"
    finally:
        await writer.close()
        await reader.close()


# ─────────────────────────────────────────────────────────────────────────
# Latched-open behaviour
# ─────────────────────────────────────────────────────────────────────────


async def test_subsequent_failures_do_not_re_fire_the_alert(migrated_db: Path) -> None:
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        for _ in range(_THRESHOLD + 5):  # cross the line, then 5 more failures
            await breaker.record_outcome("failure")
        assert len(reporter.calls) == 1  # only the transition fires the alert
        state = await reader.read()
        assert state.globally_disabled is True
        # The disable reason is still the original one — never overwritten.
        assert state.disabled_reason == CIRCUIT_OPEN_REASON
    finally:
        await writer.close()
        await reader.close()


async def test_success_while_open_resets_counter_but_keeps_lockout(
    migrated_db: Path,
) -> None:
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        for _ in range(_THRESHOLD):
            await breaker.record_outcome("failure")
        decision = await breaker.record_outcome("success")
        assert decision.state == "open"  # latched
        assert decision.consecutive_failures == 0
        state = await reader.read()
        assert state.globally_disabled is True
        assert state.consecutive_failures == 0
        # Still only the original transition alert — no re-fire on success.
        assert len(reporter.calls) == 1
    finally:
        await writer.close()
        await reader.close()


async def test_phase2_enable_path_lifts_the_lockout(migrated_db: Path) -> None:
    """The operator's ``phase2 enable`` (Story 5.12) is the only legitimate
    clear, and it calls *both* ``clear_global_disable`` and
    ``reset_failure_counter`` — modelled here so the breaker's post-clear
    behaviour is exercised under realistic state."""
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        for _ in range(_THRESHOLD):
            await breaker.record_outcome("failure")

        # Simulate `phase2 enable WD40EFPX` — lift lockout AND zero counter.
        await writer.clear_global_disable(("Western Digital", "WD Red Plus 4TB", "WD40EFPX"))
        await writer.reset_failure_counter()
        state = await reader.read()
        assert state.globally_disabled is False
        assert state.consecutive_failures == 0

        # Next failure ticks the counter from a clean closed state.
        decision = await breaker.record_outcome("failure")
        assert decision.state == "closed"
        assert decision.consecutive_failures == 1
        assert decision.just_opened is False
    finally:
        await writer.close()
        await reader.close()


# ─────────────────────────────────────────────────────────────────────────
# Restart semantics — no in-memory state
# ─────────────────────────────────────────────────────────────────────────


async def test_state_survives_a_fresh_breaker_instance(migrated_db: Path) -> None:
    """A new CircuitBreaker (daemon restart) reads the persisted state."""
    reporter = _RecordingReporter()
    breaker, writer, reader = await _make_breaker(migrated_db, reporter)
    try:
        for _ in range(_THRESHOLD):
            await breaker.record_outcome("failure")
    finally:
        await writer.close()
        await reader.close()

    # Simulate a daemon restart: brand-new objects, same DB.
    reporter_after = _RecordingReporter()
    breaker_after, writer_after, reader_after = await _make_breaker(migrated_db, reporter_after)
    try:
        state = await reader_after.read()
        assert state.globally_disabled is True
        assert state.consecutive_failures == _THRESHOLD

        # Another failure post-restart must not re-fire the alert.
        await breaker_after.record_outcome("failure")
        assert reporter_after.calls == []
    finally:
        await writer_after.close()
        await reader_after.close()
