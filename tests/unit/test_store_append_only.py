"""NFR-S4 mechanical enforcement — Story 3.15.

The audit log is append-only by contract. This test walks every
attribute of :class:`Store` and the concrete :class:`SqliteStore`
and refuses any method whose name pairs a mutator prefix
(``update_`` / ``delete_``) with an audit-table keyword
(``alert_snapshots``, ``tap_events``, ``transactions``,
``callbacks``).

There is an overlapping check in ``test_interfaces.py`` for the ABC.
This file is the dedicated NFR-S4 contract test the PRD requires;
it adds coverage for the SQLite implementation too, so the
mechanical rail isn't restricted to the port layer alone.
"""

from __future__ import annotations

import pytest

from hardware_hunter.adapters.sqlite_store import SqliteStore
from hardware_hunter.interfaces.store import Store

# Forbidden ``<prefix>_*<keyword>*`` combinations on a Store-shaped surface.
_FORBIDDEN_PREFIXES = ("update_", "delete_")
_AUDIT_KEYWORDS = (
    "audit",
    "alert",
    "callback",
    "tap_event",
    "transaction",
)


@pytest.mark.parametrize("klass", [Store, SqliteStore])
def test_no_update_or_delete_methods_on_audit_tables(klass: type) -> None:
    """Walks ``dir(klass)`` and fails if any public method name names
    an audit table after a mutator prefix."""
    offenders: list[str] = []
    for name in dir(klass):
        if name.startswith("_"):
            continue
        if any(name.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES) and any(
            kw in name for kw in _AUDIT_KEYWORDS
        ):
            offenders.append(name)
    assert not offenders, (
        f"{klass.__name__} declares forbidden audit mutators (NFR-S4 violation): {offenders}"
    )


def test_store_abc_declares_only_record_or_get_on_audit() -> None:
    """The verbs allowed on audit data are ``record_*`` (append) and
    ``get_*`` (read). Any other verb on an audit-keyword method is a
    smell, even if it's not literally ``update_`` or ``delete_``."""
    allowed_verbs = ("record_", "get_")
    suspicious: list[str] = []
    for name in dir(Store):
        if name.startswith("_"):
            continue
        if not any(kw in name for kw in _AUDIT_KEYWORDS):
            continue
        if not any(name.startswith(v) for v in allowed_verbs):
            suspicious.append(name)
    assert not suspicious, f"Store declares non-append, non-read method on audit data: {suspicious}"
