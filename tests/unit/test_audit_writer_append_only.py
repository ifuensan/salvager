"""Append-only enforcement for :class:`Phase2AuditWriter` — Story 5.1 (NFR-S4).

Two mechanical guards, both run in CI:

  1. *Introspection* — the class exposes only the seven documented
     ``record_*`` / ``phase2_state`` methods (plus ``close``). No method
     named ``update_*`` or ``delete_*`` exists, so a PR cannot add a
     mutate-an-audit-row codepath without this test naming it.
  2. *SQL static analysis* — no ``UPDATE`` or ``DELETE`` statement in the
     module's source targets one of the three append-only audit tables
     (``tap_events`` / ``transactions`` / ``phase2_smoke_tests``). The
     mutable ``phase2_state`` row is exempt by design.
"""

from __future__ import annotations

import inspect
import re

from salvager.adapters.sqlite_store import audit_writer, offer_writer
from salvager.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter

_APPEND_ONLY_TABLES = frozenset({"tap_events", "transactions", "phase2_smoke_tests"})

_EXPECTED_PUBLIC_METHODS = frozenset(
    {
        "record_tap_event",
        "record_transaction",
        "record_smoke_test",
        "set_global_disable",
        "clear_global_disable",
        "increment_failure_counter",
        "reset_failure_counter",
        # Lifecycle — not an audit-mutation surface.
        "close",
    }
)


def _public_methods() -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(Phase2AuditWriter, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_no_update_or_delete_methods_exist() -> None:
    offenders = sorted(
        name
        for name in _public_methods()
        if name.startswith("update_") or name.startswith("delete_")
    )
    assert offenders == [], (
        f"Phase2AuditWriter must stay append-only — forbidden methods found: {offenders}"
    )


def test_writer_exposes_only_the_documented_surface() -> None:
    assert _public_methods() == set(_EXPECTED_PUBLIC_METHODS)


def test_no_sql_mutates_an_append_only_audit_table() -> None:
    source = inspect.getsource(audit_writer)
    # Capture the target table of every UPDATE / DELETE FROM statement.
    mutated = re.findall(
        r"\b(?:UPDATE|DELETE\s+FROM)\s+([a-z_]+)",
        source,
        flags=re.IGNORECASE,
    )
    offenders = sorted(table for table in mutated if table in _APPEND_ONLY_TABLES)
    assert offenders == [], f"append-only audit tables must never be UPDATE/DELETE'd: {offenders}"


# ─────────────────────────────────────────────────────────────────────────
# OfferAuditWriter — same mechanical guards for the `offers` audit table
# (wallapop-offer-flow; the mutable `offer_state` row is exempt by design).
# ─────────────────────────────────────────────────────────────────────────

_OFFER_APPEND_ONLY_TABLES = frozenset({"offers"})

_EXPECTED_OFFER_PUBLIC_METHODS = frozenset(
    {
        "record_offer_attempt",
        "has_successful_offer",
        "count_recent_successes",
        "read_state",
        "set_global_disable",
        "clear_global_disable",
        "increment_failure_counter",
        "reset_failure_counter",
        # Lifecycle — not an audit-mutation surface.
        "close",
    }
)


def _offer_public_methods() -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(OfferAuditWriter, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_offer_writer_has_no_update_or_delete_methods() -> None:
    offenders = sorted(
        name
        for name in _offer_public_methods()
        if name.startswith("update_") or name.startswith("delete_")
    )
    assert offenders == [], (
        f"OfferAuditWriter must stay append-only — forbidden methods found: {offenders}"
    )


def test_offer_writer_exposes_only_the_documented_surface() -> None:
    assert _offer_public_methods() == set(_EXPECTED_OFFER_PUBLIC_METHODS)


def test_no_sql_mutates_the_offers_table() -> None:
    source = inspect.getsource(offer_writer)
    mutated = re.findall(
        r"\b(?:UPDATE|DELETE\s+FROM)\s+([a-z_]+)",
        source,
        flags=re.IGNORECASE,
    )
    offenders = sorted(table for table in mutated if table in _OFFER_APPEND_ONLY_TABLES)
    assert offenders == [], f"the offers audit table must never be UPDATE/DELETE'd: {offenders}"
