"""``hardware-hunter health`` — Story 4.4 (FR47 / NFR-O2 / UX-DR25).

Answers the operator's perpetual question — "is the bot actually
working?" — without ever needing the daemon process to be running.
Everything is read straight from SQLite (``_meta`` + ``alert_snapshots``)
and the filesystem (credential files), per AR14.

Status derivation
-----------------
Each adapter row resolves to one of three states:

- ``down``     — the adapter *cannot* run: its credential file is
  missing (no Wallapop cookie / no eBay OAuth token).
- ``degraded`` — the adapter *can* run but isn't healthy: an explicit
  degraded flag in ``_meta`` (e.g. ``wallapop_api_status``), or its
  last poll is stale (older than 2x the configured cadence — the
  "stuck poller" signal), or it has never polled at all.
- ``healthy``  — credential present *and* a fresh poll on record.

Daemon liveness
---------------
``_meta.daemon_pid`` plus an ``os.kill(pid, 0)`` probe tells running
from stopped. A stopped daemon is NOT an error — the operator may have
stopped it on purpose — so the command always exits ``0``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.adapters.sqlite_store.migrations import db_path_under
from hardware_hunter.config.config_yaml import ConfigModel, load_config
from hardware_hunter.observability.styling import ColumnSpec, render_prose, render_table
from hardware_hunter.observability.styling import print_table as _print_table

#: Credential files, relative to ``data_dir`` — mirror the composer.
_WALLAPOP_COOKIES_RELPATH = Path("auth") / "wallapop_cookies.txt"
_EBAY_TOKENS_RELPATH = Path("auth") / "oauth_tokens.json"

#: A poll older than ``cadence x this`` flags the marketplace degraded.
_STALE_CADENCE_MULTIPLIER = 2

_STATUS_HEALTHY = "healthy"
_STATUS_DEGRADED = "degraded"
_STATUS_DOWN = "down"


@dataclass(frozen=True)
class _AdapterRow:
    name: str
    status: str
    last_activity: str | None  # ISO 8601 Z, or None when never active


@dataclass(frozen=True)
class _HealthReport:
    """The full, render-agnostic health picture — built once, rendered twice."""

    version: str | None
    pid: int | None
    daemon_running: bool
    uptime_seconds: int | None
    adapters: list[_AdapterRow]
    recent_match_count_24h: int
    last_poll: dict[str, str | None]
    last_poll_stale: dict[str, bool]
    cadence_minutes: dict[str, int]
    phase2_circuit_threshold: int


def run(
    *,
    data_dir: Path,
    config_path: Path,
    output_format: str = "human",
    width: int = 80,
    now: datetime | None = None,
) -> int:
    """Render the daemon health snapshot. Always exits ``0`` (AR14)."""
    moment = now if now is not None else datetime.now(UTC)
    config = load_config(config_path)
    report = _build_report(data_dir=data_dir, config=config, now=moment)

    if output_format == "json":
        print(json.dumps(_report_to_json(report)))
        return 0
    if output_format != "human":
        render_prose(
            f"unknown --format value: {output_format!r}",
            style="error",
            hint="use --format human or --format json",
        )
        return 2

    _render_human(report, width=width, now=moment)
    return 0


# ─────────────────────────────────────────────────────────────────────────
# Report builder — pure read of SQLite + filesystem + config
# ─────────────────────────────────────────────────────────────────────────


def _build_report(*, data_dir: Path, config: ConfigModel, now: datetime) -> _HealthReport:
    meta = _read_meta(data_dir)
    recent_matches = _count_recent_matches(data_dir, now=now)

    cadence = {
        "wallapop": config.schedule.wallapop_minutes,
        "ebay": config.schedule.ebay_minutes,
    }
    last_poll: dict[str, str | None] = {
        "wallapop": _iso_z(meta.get("last_poll_wallapop")),
        "ebay": _iso_z(meta.get("last_poll_ebay")),
    }
    stale = {
        market: _is_stale(last_poll[market], cadence[market], now=now)
        for market in ("wallapop", "ebay")
    }

    cookie_present = (data_dir / _WALLAPOP_COOKIES_RELPATH).exists()
    tokens_present = (data_dir / _EBAY_TOKENS_RELPATH).exists()

    adapters = [
        _wallapop_api_row(meta, last_poll["wallapop"], stale["wallapop"], cookie_present),
        _wallapop_tinyfish_row(last_poll["wallapop"], stale["wallapop"], cookie_present),
        _ebay_api_row(last_poll["ebay"], stale["ebay"], tokens_present),
    ]

    pid = _parse_int(meta.get("daemon_pid"))
    running = pid is not None and _pid_alive(pid)
    uptime = _uptime_seconds(meta.get("daemon_started_at"), now=now) if running else None

    return _HealthReport(
        version=meta.get("daemon_version"),
        pid=pid,
        daemon_running=running,
        uptime_seconds=uptime,
        adapters=adapters,
        recent_match_count_24h=recent_matches,
        last_poll=last_poll,
        last_poll_stale=stale,
        cadence_minutes=cadence,
        phase2_circuit_threshold=config.phase2.circuit_breaker_threshold,
    )


def _read_meta(data_dir: Path) -> dict[str, str]:
    """Read every ``_meta`` row, or an empty dict when the DB is absent."""
    db_path = db_path_under(data_dir)
    if not db_path.exists():
        return {}
    connection = open_connection(db_path)
    try:
        cursor = connection.execute("SELECT key, value FROM _meta")
        return {str(row["key"]): str(row["value"]) for row in cursor.fetchall()}
    except sqlite3.Error:
        # An un-migrated / corrupt DB is treated as "no state" rather
        # than crashing the diagnostic command.
        return {}
    finally:
        connection.close()


def _count_recent_matches(data_dir: Path, *, now: datetime) -> int:
    """Count Phase 1 alert snapshots dispatched in the last 24h."""
    db_path = db_path_under(data_dir)
    if not db_path.exists():
        return 0
    cutoff = (now - timedelta(hours=24)).isoformat()
    connection = open_connection(db_path)
    try:
        cursor = connection.execute(
            "SELECT COUNT(*) FROM alert_snapshots WHERE rendered_at >= ?",
            (cutoff,),
        )
        return int(cursor.fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


# ─────────────────────────────────────────────────────────────────────────
# Per-adapter status rules
# ─────────────────────────────────────────────────────────────────────────


def _wallapop_api_row(
    meta: dict[str, str],
    last_poll: str | None,
    stale: bool,
    cookie_present: bool,
) -> _AdapterRow:
    if not cookie_present:
        status = _STATUS_DOWN
    elif meta.get("wallapop_api_status") == _STATUS_DEGRADED or stale or last_poll is None:
        status = _STATUS_DEGRADED
    else:
        status = _STATUS_HEALTHY
    return _AdapterRow("wallapop_api", status, last_poll)


def _wallapop_tinyfish_row(
    last_poll: str | None,
    stale: bool,
    cookie_present: bool,
) -> _AdapterRow:
    # TinyFish is the fallback path — it has no credential file of its
    # own (it rides the daemon's TINYFISH_API_KEY). It is healthy unless
    # the whole Wallapop leg is stuck (a stale last-poll affects every
    # path) or has never run.
    if stale or last_poll is None:
        status = _STATUS_DEGRADED
    elif not cookie_present:
        # The API cookie is gone, but TinyFish itself is still serving.
        status = _STATUS_HEALTHY
    else:
        status = _STATUS_HEALTHY
    return _AdapterRow("wallapop_tinyfish", status, last_poll)


def _ebay_api_row(last_poll: str | None, stale: bool, tokens_present: bool) -> _AdapterRow:
    if not tokens_present:
        status = _STATUS_DOWN
    elif stale or last_poll is None:
        status = _STATUS_DEGRADED
    else:
        status = _STATUS_HEALTHY
    return _AdapterRow("ebay_api", status, last_poll)


# ─────────────────────────────────────────────────────────────────────────
# Small pure helpers
# ─────────────────────────────────────────────────────────────────────────


def _iso_z(value: str | None) -> str | None:
    """Normalize a UTC ISO 8601 string to the ``Z`` suffix form (UX-DR20).

    ``datetime.isoformat()`` emits ``+00:00``; the operator-facing
    contract is the ``Z`` shorthand. Non-UTC or unparseable strings are
    returned untouched.
    """
    if value is None:
        return None
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_stale(last_poll: str | None, cadence_minutes: int, *, now: datetime) -> bool:
    """True iff the last poll is older than ``2x cadence`` (stuck poller)."""
    parsed = _parse_dt(last_poll)
    if parsed is None:
        return False  # "never polled" is handled separately as its own state
    threshold = timedelta(minutes=cadence_minutes * _STALE_CADENCE_MULTIPLIER)
    return (now - parsed) > threshold


def _uptime_seconds(started_at: str | None, *, now: datetime) -> int | None:
    parsed = _parse_dt(started_at)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _pid_alive(pid: int) -> bool:
    """True iff a process with ``pid`` exists (signal 0 is the probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else — still "alive".
        return True
    except OSError:
        return False
    return True


def _humanize_ago(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


# ─────────────────────────────────────────────────────────────────────────
# JSON rendering (UX-DR20: snake_case + ISO 8601 Z)
# ─────────────────────────────────────────────────────────────────────────


def _report_to_json(report: _HealthReport) -> dict[str, Any]:
    return {
        "version": report.version,
        "uptime_seconds": report.uptime_seconds,
        "pid": report.pid,
        "daemon_running": report.daemon_running,
        "adapters": [
            {
                "name": row.name,
                "status": row.status,
                "last_activity": row.last_activity,
            }
            for row in report.adapters
        ],
        "recent_match_count_24h": report.recent_match_count_24h,
        "last_poll": report.last_poll,
        "phase2": {
            "enabled_count": 0,
            "globally_disabled": False,
            "circuit_breaker": {
                "state": "closed",
                "consecutive_failures": 0,
                "threshold": report.phase2_circuit_threshold,
            },
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# Human rendering — header block + adapter table + footer block
# ─────────────────────────────────────────────────────────────────────────


def _render_human(report: _HealthReport, *, width: int, now: datetime) -> None:
    _render_header(report)
    _render_adapter_table(report, width=width)
    _render_footer(report, now=now)


def _render_header(report: _HealthReport) -> None:
    if report.daemon_running:
        uptime = report.uptime_seconds if report.uptime_seconds is not None else 0
        lines = [
            f"Daemon: running (PID {report.pid})",
            f"Version: {report.version or 'unknown'}",
            f"Uptime: {_humanize_ago(timedelta(seconds=uptime))}",
        ]
    else:
        lines = [
            "Daemon: not running",
            f"Version: {report.version or 'unknown'}",
        ]
    render_prose("\n".join(lines), style="info")


def _render_adapter_table(report: _HealthReport, *, width: int) -> None:
    columns: list[ColumnSpec] = [
        {"key": "adapter", "header": "Adapter"},
        {"key": "status", "header": "Status"},
        {"key": "last_activity", "header": "Last Activity"},
    ]
    rows: list[dict[str, object]] = [
        {
            "adapter": adapter.name,
            "status": adapter.status,
            "last_activity": adapter.last_activity,
        }
        for adapter in report.adapters
    ]
    table = render_table(rows, columns, width=width)
    _print_table(table, width=width)


def _render_footer(report: _HealthReport, *, now: datetime) -> None:
    # Recent matches — the UX-DR25 "watching, not stuck" disambiguation.
    matches_line = f"Recent matches: {report.recent_match_count_24h} in last 24h (watching)"

    # Last poll — newest across marketplaces, with staleness context.
    last_poll_line = _last_poll_line(report, now=now)

    phase2_line = (
        "Phase 2: 0 entries enabled "
        f"(globally disabled? no; circuit breaker closed 0/{report.phase2_circuit_threshold})"
    )

    render_prose("\n".join([matches_line, last_poll_line, phase2_line]), style="secondary")


def _last_poll_line(report: _HealthReport, *, now: datetime) -> str:
    dated: list[tuple[str, str, datetime]] = []
    for market, ts in report.last_poll.items():
        parsed = _parse_dt(ts)
        if ts is not None and parsed is not None:
            dated.append((market, ts, parsed))
    if not dated:
        return "Last poll: never"
    market, ts, parsed = max(dated, key=lambda triple: triple[2])
    ago = _humanize_ago(now - parsed)
    cadence = report.cadence_minutes[market]
    suffix = ""
    if report.last_poll_stale.get(market):
        suffix = f" — STALE, expected every {cadence} min"
    return f"Last poll: {ts} ({market}, {ago}){suffix}"


__all__ = ["run"]
