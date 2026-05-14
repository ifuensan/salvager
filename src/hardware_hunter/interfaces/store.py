"""``Store`` ABC — Story 3.2 (AR8 / AR9 / NFR-S4 / AR24).

The port through which orchestration reads + writes persistence state.
The v1 implementation is ``adapters/sqlite_store``; alternative backends
are not anticipated but the boundary keeps that door open.

Append-only contract (NFR-S4)
-----------------------------
There are NO ``update_*`` or ``delete_*`` methods on any audit row in
this ABC. Once an alert is dispatched, the audit record is permanent.
Phase 1 stores can mutate ``seen_listings``-class state (dedup ↔
snooze) freely — that's not audit data.

Phase 2 method declarations (AR24)
----------------------------------
``record_tap_event`` and ``record_transaction`` are declared so the
:class:`Store` shape is complete and the schema migration runner
(Story 3.3) knows the full row set. Concrete v0.x implementations
raise :class:`hardware_hunter.domain.audit.Phase2GuardrailTripped` if
called — Phase 2 is not enabled at v0.x.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from hardware_hunter.domain.alert import AlertSnapshot
from hardware_hunter.domain.audit import (
    CallbackAudit,
    TapEventAudit,
    TransactionAudit,
)
from hardware_hunter.domain.listing import Listing

#: An entry's (manufacturer, model, ref) tuple per FR4.
EntryKey = tuple[str, str, str]


class Store(ABC):
    """Port for persistence: dedup, snooze, alert snapshots, audit log."""

    # ─────────────────────────────────────────────────────────────────
    # Phase 1: dedup state
    # ─────────────────────────────────────────────────────────────────

    @abstractmethod
    async def is_seen(self, listing_id: str, entry_key: EntryKey) -> bool:
        """``True`` iff the (listing, entry) pair already triggered an alert."""

    @abstractmethod
    async def record_seen(self, listing: Listing, entry_key: EntryKey) -> None:
        """Mark the (listing, entry) pair as seen — no-op if already present."""

    # ─────────────────────────────────────────────────────────────────
    # Phase 1: snooze state
    # ─────────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_snooze_until(self, entry_key: EntryKey) -> datetime | None:
        """The UTC datetime until which alerts for this entry are suppressed,
        or None if no snooze is active."""

    @abstractmethod
    async def set_snooze(self, entry_key: EntryKey, until: datetime) -> None:
        """Suppress alerts for this entry until the given UTC datetime."""

    # ─────────────────────────────────────────────────────────────────
    # Phase 1: alert snapshots + audit (append-only)
    # ─────────────────────────────────────────────────────────────────

    @abstractmethod
    async def record_alert_snapshot(self, snapshot: AlertSnapshot) -> int:
        """Persist an alert snapshot and return its monotonic ``audit_id``.

        ``alert_snapshots`` IS the audit table for dispatched alerts —
        there is no separate "audit" write for alerts. Replay queries
        and callback lookups read from this table; the slim audit view
        is :class:`AlertSnapshotAudit` (read-side projection).
        """

    @abstractmethod
    async def get_alert_snapshot(self, audit_id: int) -> AlertSnapshot | None:
        """Look up an alert snapshot by audit_id (used by callback handler)."""

    @abstractmethod
    async def get_alert_snapshot_by_alert_id(self, alert_id: UUID) -> AlertSnapshot | None:
        """Look up an alert snapshot by its UUID ``alert_id``.

        The callback handler (Story 3.13) uses this to resolve the
        originating ``entry_key`` when an operator taps a Phase 1
        button — ``callback_data`` carries the UUID, not the internal
        autoincrement ``audit_id``, because eBay listing IDs contain
        characters that aren't valid callback_data and we deliberately
        chose ``alert_id`` as the stable handle in the locked
        ``<surface>:<verb>:<id>`` format.
        """

    @abstractmethod
    async def record_callback(self, callback: CallbackAudit) -> None:
        """Append a callback-tap row to the audit log (NFR-S4)."""

    # ─────────────────────────────────────────────────────────────────
    # Phase 1: _meta key-value store
    # ─────────────────────────────────────────────────────────────────
    #
    # The ``_meta`` table is the daemon's persistent scratch space:
    # poll heartbeats, daemon PID/version, adapter-status flags. It is
    # NOT audit data — keys are freely overwritten. The ``health`` CLI
    # command (Story 4.4) reads ``_meta`` directly so it can report
    # daemon state without the daemon process running (AR14).

    @abstractmethod
    async def set_meta(self, key: str, value: str) -> None:
        """Upsert a ``_meta`` key. Overwrites any prior value."""

    @abstractmethod
    async def get_meta(self, key: str) -> str | None:
        """Return the ``_meta`` value for ``key``, or None if unset."""

    @abstractmethod
    async def get_all_meta(self) -> dict[str, str]:
        """Return every ``_meta`` key-value pair as a plain dict."""

    # ─────────────────────────────────────────────────────────────────
    # Phase 2 — declared, but concrete v0.x implementations must raise
    # Phase2GuardrailTripped per AR24. No code path produces these
    # objects until Phase 2 is enabled (the domain constructors
    # themselves trip the guardrail at v0.x — see domain/audit.py).
    # ─────────────────────────────────────────────────────────────────

    @abstractmethod
    async def record_tap_event(self, tap: TapEventAudit) -> None:
        """Phase 2 audit — operator's buy-confirmation tap."""

    @abstractmethod
    async def record_transaction(self, transaction: TransactionAudit) -> None:
        """Phase 2 audit — completed autonomous purchase."""


class StoreError(RuntimeError):
    """Persistence operation failed; cause lives in ``__cause__``."""
