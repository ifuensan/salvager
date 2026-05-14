"""In-memory health-state cache — Story 4.2 (NFR-R3).

One of the three independent surfaces every degradation fans out to
(the other two: the structured logger and the Telegram surface). The
:class:`DegradationReporter` writes here on every ``report()`` call;
the ``hardware-hunter health`` CLI command (Story 4.4) reads the
snapshot to answer "is the daemon watching, or is it stuck?".

State lives in memory only — a daemon restart starts clean. That is
intentional: a restart re-attempts every adapter optimistically, and
the health snapshot reflects what's degraded *right now*, not what was
degraded before the last restart.

Adapter degradation
-------------------
When a degradation event's ``ctx`` carries an ``adapter`` key, the
named adapter is flagged degraded. Recovery events — those whose
:class:`EventName` ends in ``_renewed`` or ``_recovered`` — clear that
adapter's flag. An adapter with no degradation record is healthy by
omission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hardware_hunter.domain.alert import EventName, Severity

#: EventName suffixes that mark a recovery — these clear, rather than
#: set, the degraded flag on the adapter named in ``ctx``.
_RECOVERY_SUFFIXES: tuple[str, ...] = ("_renewed", "_recovered")


@dataclass(frozen=True)
class DegradationRecord:
    """One degradation (or recovery) event, as the health cache stores it."""

    event: EventName
    severity: Severity
    ctx: Mapping[str, Any]
    at: datetime


@dataclass
class HealthState:
    """Mutable in-memory cache of the daemon's degradation history.

    Not thread-safe by design — the daemon runs a single asyncio event
    loop, and ``record`` is only ever called from inside it.
    """

    #: The most recent record per event type. Overwritten on repeat.
    _last_by_event: dict[EventName, DegradationRecord] = field(default_factory=dict)
    #: Adapters currently flagged degraded → the record that flagged them.
    _degraded_adapters: dict[str, DegradationRecord] = field(default_factory=dict)

    def record(
        self,
        severity: Severity,
        event: EventName,
        ctx: Mapping[str, Any],
        at: datetime,
    ) -> None:
        """Record one degradation/recovery event into the cache.

        Always updates ``_last_by_event``. When ``ctx`` names an
        ``adapter``: a recovery event clears that adapter's degraded
        flag, any other event sets it.
        """
        record = DegradationRecord(event=event, severity=severity, ctx=dict(ctx), at=at)
        self._last_by_event[event] = record

        adapter = ctx.get("adapter")
        if adapter is None:
            return
        adapter_name = str(adapter)
        if event.value.endswith(_RECOVERY_SUFFIXES):
            self._degraded_adapters.pop(adapter_name, None)
        else:
            self._degraded_adapters[adapter_name] = record

    def degraded_adapters(self) -> dict[str, DegradationRecord]:
        """Snapshot of adapters currently flagged degraded (copy — safe to keep)."""
        return dict(self._degraded_adapters)

    def last_event(self, event: EventName) -> DegradationRecord | None:
        """The most recent record for ``event``, or None if never seen."""
        return self._last_by_event.get(event)

    def last_events(self) -> dict[EventName, DegradationRecord]:
        """Snapshot of the most-recent record per event type (copy)."""
        return dict(self._last_by_event)

    def is_degraded(self) -> bool:
        """True iff at least one adapter is currently flagged degraded."""
        return bool(self._degraded_adapters)


__all__ = [
    "DegradationRecord",
    "HealthState",
]
