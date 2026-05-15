"""Phase 2 circuit breaker + auto-disable lockout — Story 5.5
(FR34 / FR35 / AR13 / NFR-R4).

The breaker wraps the pure :func:`compute_next_state` with the three
side-effects it needs:

  1. counter persistence via :class:`Phase2AuditWriter` (per-outcome);
  2. the durable global lockout (``set_global_disable``) on the failure
     that crosses the threshold;
  3. the ``circuit_open`` operational alert via :class:`Reporter`.

The state itself is *not* held in memory — every call reads the
``phase2_state`` row through :class:`Phase2StateReader`, so a daemon
restart automatically picks up the persisted state. The state machine
is implemented in :mod:`hardware_hunter.domain.circuit` and exercised
under a ``hypothesis`` property test (Story 5.5 AC).

Recovery model
--------------
There is no auto-recovery: once open, the breaker stays open until an
explicit ``hardware-hunter phase2 enable <entry>`` call lifts the
lockout via ``Phase2AuditWriter.clear_global_disable``. A Phase 2
*success* recorded while the circuit is open still resets the counter
— the lockout flag is independent and only the operator-action path
clears it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hardware_hunter.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from hardware_hunter.domain.alert import EventName
from hardware_hunter.domain.circuit import (
    CircuitDecision,
    CircuitState,
    Outcome,
    compute_next_state,
)
from hardware_hunter.interfaces.phase2_state_reader import Phase2StateReader
from hardware_hunter.orchestration.degradation_reporter import Reporter

#: The reason string persisted in ``phase2_state.disabled_reason`` when
#: the breaker locks Phase 2. Operators see it via ``hardware-hunter health``.
CIRCUIT_OPEN_REASON: Final[str] = "circuit_breaker_open"


@dataclass
class CircuitBreaker:
    """Stateless wrapper around the pure transition function."""

    audit_writer: Phase2AuditWriter
    state_reader: Phase2StateReader
    reporter: Reporter
    threshold: int

    async def record_outcome(
        self,
        outcome: Outcome,
        *,
        last_affected_entry: str | None = None,
    ) -> CircuitDecision:
        """Apply one Phase 2 outcome and return the new state.

        ``last_affected_entry`` flows into the ``circuit_open`` alert's
        ctx so the operator's Telegram message names the entry whose
        failure tripped the breaker. It is ignored for any outcome that
        doesn't transition closed → open.
        """
        snapshot = await self.state_reader.read()
        current_state: CircuitState = "open" if snapshot.globally_disabled else "closed"
        decision = compute_next_state(
            current_state, snapshot.consecutive_failures, outcome, self.threshold
        )

        # Persist the counter for every outcome. Persistence happens
        # before the lockout / alert so the audit trail is consistent
        # even if those subsequent calls fail.
        if outcome == "success":
            await self.audit_writer.reset_failure_counter()
        else:
            await self.audit_writer.increment_failure_counter()

        if decision.just_opened:
            await self.audit_writer.set_global_disable(CIRCUIT_OPEN_REASON)
            await self.reporter.report(
                "warn",
                EventName.circuit_open,
                ctx={
                    "consecutive_failures": decision.consecutive_failures,
                    "threshold": self.threshold,
                    "last_affected_entry": last_affected_entry or "—",
                },
            )

        return decision


async def record_success(breaker: CircuitBreaker) -> CircuitDecision:
    """Tiny ergonomic alias for the buy orchestrator's success path."""
    return await breaker.record_outcome("success")


async def record_failure(
    breaker: CircuitBreaker, *, last_affected_entry: str | None = None
) -> CircuitDecision:
    """Tiny ergonomic alias for the buy orchestrator's failure path."""
    return await breaker.record_outcome("failure", last_affected_entry=last_affected_entry)


__all__ = [
    "CIRCUIT_OPEN_REASON",
    "CircuitBreaker",
    "record_failure",
    "record_success",
]
