"""Phase 2 circuit-breaker state machine — Story 5.5 (pure domain).

This module is intentionally IO-free. The orchestrator
(:mod:`hardware_hunter.orchestration.circuit_breaker`) wraps the pure
function below with persistence + alert dispatch; the math itself is
total and deterministic so it can be exhaustively property-tested with
``hypothesis``.

State transitions
-----------------
- ``(closed, success)``  → ``closed`` with the counter reset to 0.
- ``(closed, failure)``  → ``closed`` (counter +1) OR ``open`` when the
  new counter reaches ``threshold``.
- ``(open, success)``    → ``open`` (latched) with the counter reset.
- ``(open, failure)``    → ``open`` (latched) with the counter +1.

Once open, the circuit stays open until an external action clears it
(``phase2 enable <entry>`` → ``clear_global_disable``) — there is no
auto-recovery. This is NFR-R4 by design.

The ``just_opened`` flag fires exactly once: on the failure that
crosses the threshold. The orchestrator listens for it to call
``set_global_disable`` and dispatch the ``circuit_open`` alert without
re-firing on every subsequent failure while latched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

CircuitState = Literal["closed", "open"]
Outcome = Literal["success", "failure"]

#: Sane lower bound — a threshold of 0 would open on the first failure
#: AND interpret an empty closed state as already-open. ``compute_next_state``
#: raises on values below this so a config misconfiguration fails loud.
MIN_THRESHOLD: Final[int] = 1


@dataclass(frozen=True)
class CircuitDecision:
    """Outcome of one transition.

    Carries the resulting state, the resulting counter value, and
    ``just_opened`` — True for exactly the transition that crossed the
    threshold from closed → open. Use the flag to fire the lockout
    alert once; the counter to mirror onto ``phase2_state``.
    """

    state: CircuitState
    consecutive_failures: int
    just_opened: bool


def compute_next_state(
    state: CircuitState,
    consecutive_failures: int,
    outcome: Outcome,
    threshold: int,
) -> CircuitDecision:
    """Pure transition function. Raises on invalid input.

    The orchestrator passes ``state`` derived from
    ``phase2_state.globally_disabled`` (``open`` iff disabled) and the
    persisted counter; we return the new state to persist back.
    """
    if threshold < MIN_THRESHOLD:
        raise ValueError(f"threshold must be >= {MIN_THRESHOLD}; got {threshold}")
    if consecutive_failures < 0:
        raise ValueError(f"consecutive_failures must be >= 0; got {consecutive_failures}")

    if outcome == "success":
        # Success always clears the counter; the lockout state itself is
        # cleared only by an explicit operator action (NFR-R4).
        return CircuitDecision(state=state, consecutive_failures=0, just_opened=False)

    # outcome == "failure"
    new_failures = consecutive_failures + 1
    if state == "open":
        # Already latched — keep counting for transparency in the audit
        # trail, but never re-fire just_opened.
        return CircuitDecision(state="open", consecutive_failures=new_failures, just_opened=False)
    if new_failures >= threshold:
        return CircuitDecision(state="open", consecutive_failures=new_failures, just_opened=True)
    return CircuitDecision(state="closed", consecutive_failures=new_failures, just_opened=False)


__all__ = [
    "MIN_THRESHOLD",
    "CircuitDecision",
    "CircuitState",
    "Outcome",
    "compute_next_state",
]
