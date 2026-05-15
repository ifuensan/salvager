"""Pure-domain tests for the circuit-breaker state machine — Story 5.5.

The transition function is total and IO-free, so every property the AC
calls out can be expressed as a hypothesis check. The headline test
generates 100+ random success/failure sequences and asserts the running
state matches the spec:

  - the counter never goes negative;
  - the counter resets to zero on every success;
  - the circuit opens exactly when failures-since-last-success reaches
    threshold while closed;
  - once open, no outcome closes it without an external clear.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hardware_hunter.domain.circuit import (
    CircuitDecision,
    compute_next_state,
)

_OUTCOMES = st.sampled_from(["success", "failure"])


# ─────────────────────────────────────────────────────────────────────────
# Tabular transitions — one explicit case per AC bullet
# ─────────────────────────────────────────────────────────────────────────


def test_closed_success_resets_counter() -> None:
    decision = compute_next_state("closed", 2, "success", threshold=3)
    assert decision == CircuitDecision("closed", 0, just_opened=False)


def test_closed_failure_below_threshold_stays_closed() -> None:
    decision = compute_next_state("closed", 1, "failure", threshold=3)
    assert decision == CircuitDecision("closed", 2, just_opened=False)


def test_closed_failure_reaching_threshold_opens_and_flags_just_opened() -> None:
    decision = compute_next_state("closed", 2, "failure", threshold=3)
    assert decision == CircuitDecision("open", 3, just_opened=True)


def test_open_failure_stays_open_without_just_opened() -> None:
    decision = compute_next_state("open", 5, "failure", threshold=3)
    assert decision == CircuitDecision("open", 6, just_opened=False)


def test_open_success_resets_counter_but_stays_open() -> None:
    """The lockout flag is independent of the counter — only an explicit
    operator action clears it (NFR-R4)."""
    decision = compute_next_state("open", 5, "success", threshold=3)
    assert decision == CircuitDecision("open", 0, just_opened=False)


# ─────────────────────────────────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────────────────────────────────


def test_threshold_below_one_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="threshold"):
        compute_next_state("closed", 0, "failure", threshold=0)


def test_negative_counter_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="consecutive_failures"):
        compute_next_state("closed", -1, "failure", threshold=3)


# ─────────────────────────────────────────────────────────────────────────
# Property: sequences match the spec
# ─────────────────────────────────────────────────────────────────────────


@settings(max_examples=200)
@given(
    threshold=st.integers(min_value=1, max_value=10),
    outcomes=st.lists(_OUTCOMES, min_size=0, max_size=50),
)
def test_random_sequence_obeys_spec(threshold: int, outcomes: list[str]) -> None:
    """Walk a random outcome sequence and verify the running state.

    Spec the test re-implements as the oracle:

      - the counter equals the run-length of consecutive failures since
        the last success (or since the start);
      - the circuit is open iff that count has EVER reached threshold;
      - ``just_opened`` flags the exact step that crossed the line.
    """
    state = "closed"
    counter = 0
    expected_open = False

    for outcome in outcomes:
        decision = compute_next_state(state, counter, outcome, threshold)  # type: ignore[arg-type]

        oracle_counter = 0 if outcome == "success" else counter + 1

        oracle_just_opened = (
            state == "closed" and outcome == "failure" and oracle_counter >= threshold
        )
        oracle_state = "open" if expected_open or oracle_just_opened else "closed"

        assert decision.consecutive_failures == oracle_counter, (
            f"counter drift at outcome={outcome!r} from ({state}, {counter}, t={threshold})"
        )
        assert decision.state == oracle_state, (
            f"state drift at outcome={outcome!r} from ({state}, {counter}, t={threshold})"
        )
        assert decision.just_opened == oracle_just_opened

        state = decision.state
        counter = decision.consecutive_failures
        if oracle_just_opened:
            expected_open = True


@given(
    threshold=st.integers(min_value=1, max_value=10),
    outcomes=st.lists(_OUTCOMES, min_size=1, max_size=30),
)
def test_open_circuit_never_returns_to_closed(threshold: int, outcomes: list[str]) -> None:
    """Latched-open is durable: no sequence of outcomes can close it."""
    state = "open"
    counter = threshold
    for outcome in outcomes:
        decision = compute_next_state(state, counter, outcome, threshold)  # type: ignore[arg-type]
        assert decision.state == "open"
        assert decision.just_opened is False  # never re-fires
        state = decision.state
        counter = decision.consecutive_failures
