"""``salvager audit show`` / ``audit export`` — Story 4.5 (FR37 / NFR-O3).

The audit log is a read-only union over three SQLite tables:

  - ``alert_snapshots`` → ``alert`` records (one per dispatched alert)
  - ``callbacks``       → ``callback`` records (one per inline-button tap)
  - ``seen_listings``   → ``dropped`` records (sightings that fell below
    the confidence threshold; ``match_fired = 0``) — opt-in via
    ``--include-dropped``

Like ``salvager health`` (Story 4.4), this command reads SQLite
directly and never needs the daemon running (AR14).

``--id`` resolves against the integer ``audit_id`` of ``alert_snapshots``
first, then ``callbacks`` — those two tables carry the autoincrement
audit ids. ``dropped`` records are listing sightings, not audit rows,
so they are not addressable by ``--id``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.domain.alert import AlertSnapshot, render_phase1_listing_alert
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.observability.styling import ColumnSpec, render_prose, render_table
from salvager.observability.styling import print_table as _print_table

#: The audit record types `--type` accepts.
_TYPE_ALERT = "alert"
_TYPE_CALLBACK = "callback"
_TYPE_DROPPED = "dropped"
_VALID_TYPES = frozenset({_TYPE_ALERT, _TYPE_CALLBACK, _TYPE_DROPPED})

_DEFAULT_SHOW_LIMIT = 10

#: ISO 8601 UTC offset that ``datetime.fromisoformat`` accepts in place of ``Z``.
_UTC_ISO_OFFSET = "+00:00"


# ─────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────


def run_show(
    *,
    data_dir: Path,
    last: int = _DEFAULT_SHOW_LIMIT,
    record_id: int | None = None,
    type_filter: str | None = None,
    since: str | None = None,
    include_dropped: bool = False,
    output_format: str = "human",
    width: int = 80,
) -> int:
    """``audit show`` — list recent audit records, or one in full detail."""
    if type_filter is not None and type_filter not in _VALID_TYPES:
        render_prose(
            f"unknown --type value: {type_filter!r}",
            style="error",
            hint=f"valid types: {', '.join(sorted(_VALID_TYPES))}",
        )
        return 2
    if output_format not in ("human", "json"):
        render_prose(
            f"unknown --format value: {output_format!r}",
            style="error",
            hint="use --format human or --format json",
        )
        return 2

    db_path = db_path_under(data_dir)

    # ── Single-record full detail ────────────────────────────────────
    if record_id is not None:
        record = _load_full_record(db_path, record_id)
        if record is None:
            render_prose(
                f"audit id {record_id} not found",
                style="error",
                hint="salvager audit show --last 5",
            )
            return 1
        print(json.dumps(record, indent=2))
        return 0

    # ── List view ────────────────────────────────────────────────────
    since_dt = _parse_since(since)
    if since is not None and since_dt is None:
        render_prose(
            f"invalid --since value: {since!r}",
            style="error",
            hint="use an ISO 8601 date or datetime, e.g. 2026-05-01",
        )
        return 2

    records = _load_records(db_path, include_dropped=include_dropped)
    records = _filter_records(records, type_filter=type_filter, since=since_dt)
    # Newest first, then apply the `--last N` window.
    records.sort(key=lambda r: r["timestamp"], reverse=True)
    if last <= 0:
        return 0  # `--last 0` is a deliberate "show nothing"
    records = records[:last]

    if output_format == "json":
        print(json.dumps(records))
        return 0

    _render_show_table(records, width=width)
    return 0


def run_export(
    *,
    data_dir: Path,
    since: str | None = None,
) -> int:
    """``audit export`` — stream every audit row as JSON Lines.

    One JSON object per line — ``jq``-friendly. ``dropped`` sightings
    are excluded: export is the audit trail (alerts + callbacks), not
    the dedup index.
    """
    since_dt = _parse_since(since)
    if since is not None and since_dt is None:
        render_prose(
            f"invalid --since value: {since!r}",
            style="error",
            hint="use an ISO 8601 date or datetime, e.g. 2026-04-01",
        )
        return 2

    db_path = db_path_under(data_dir)
    records = _load_records(db_path, include_dropped=False)
    records = _filter_records(records, type_filter=None, since=since_dt)
    records.sort(key=lambda r: r["timestamp"])  # chronological for export

    for record in records:
        print(json.dumps(record))
    return 0


# ─────────────────────────────────────────────────────────────────────────
# SQLite readers
# ─────────────────────────────────────────────────────────────────────────


def _load_records(db_path: Path, *, include_dropped: bool) -> list[dict[str, Any]]:
    """Build the union summary view. Missing DB → empty list (AR14)."""
    if not db_path.exists():
        return []
    connection = open_connection(db_path)
    try:
        records = _read_alert_summaries(connection) + _read_callback_summaries(connection)
        if include_dropped:
            records += _read_dropped_summaries(connection)
        return records
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _read_alert_summaries(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = connection.execute(
        """
        SELECT audit_id, alert_id, entry_display_name,
               listing_json, evaluation_json, rendered_at
        FROM alert_snapshots
        """
    )
    records: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        listing = json.loads(row["listing_json"])
        evaluation = json.loads(row["evaluation_json"])
        marketplace = listing.get("marketplace", "—")
        confidence = evaluation.get("confidence", "—")
        records.append(
            {
                "id": int(row["audit_id"]),
                "type": _TYPE_ALERT,
                "timestamp": _iso_z(row["rendered_at"]),
                "summary": f"{row['entry_display_name']} · {marketplace} · {confidence}",
            }
        )
    return records


def _read_callback_summaries(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = connection.execute("SELECT audit_id, alert_id, verb, received_at FROM callbacks")
    return [
        {
            "id": int(row["audit_id"]),
            "type": _TYPE_CALLBACK,
            "timestamp": _iso_z(row["received_at"]),
            "summary": f"{row['verb']} · alert {str(row['alert_id'])[:8]}",
        }
        for row in cursor.fetchall()
    ]


def _read_dropped_summaries(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = connection.execute(
        """
        SELECT rowid, listing_id, entry_manufacturer, entry_model, last_seen_at
        FROM seen_listings
        WHERE match_fired = 0
        """
    )
    return [
        {
            "id": int(row["rowid"]),
            "type": _TYPE_DROPPED,
            "timestamp": _iso_z(row["last_seen_at"]),
            "summary": (f"{row['listing_id']} · {row['entry_manufacturer']} {row['entry_model']}"),
        }
        for row in cursor.fetchall()
    ]


def _load_full_record(db_path: Path, audit_id: int) -> dict[str, Any] | None:
    """Resolve one ``--id`` to a full-detail record (alert, then callback)."""
    if not db_path.exists():
        return None
    connection = open_connection(db_path)
    try:
        alert = connection.execute(
            "SELECT * FROM alert_snapshots WHERE audit_id = ?", (audit_id,)
        ).fetchone()
        if alert is not None:
            return _full_alert_record(alert)
        callback = connection.execute(
            "SELECT * FROM callbacks WHERE audit_id = ?", (audit_id,)
        ).fetchone()
        if callback is not None:
            return _full_callback_record(callback)
        return None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _full_alert_record(row: sqlite3.Row) -> dict[str, Any]:
    listing = Listing.model_validate_json(row["listing_json"])
    evaluation = ListingEvaluation.model_validate_json(row["evaluation_json"])
    snapshot = AlertSnapshot(
        alert_id=row["alert_id"],
        entry_key=(row["entry_manufacturer"], row["entry_model"], row["entry_ref"]),
        entry_display_name=row["entry_display_name"],
        listing=listing,
        evaluation=evaluation,
        phase=row["phase"],
        phase2_max_price_eur=row["phase2_max_price_eur"],
        rendered_at=datetime.fromisoformat(row["rendered_at"]),
    )
    return {
        "id": int(row["audit_id"]),
        "type": _TYPE_ALERT,
        "timestamp": _iso_z(row["rendered_at"]),
        "alert_id": str(row["alert_id"]),
        "entry_key": [row["entry_manufacturer"], row["entry_model"], row["entry_ref"]],
        "entry_display_name": row["entry_display_name"],
        "phase": row["phase"],
        "listing": json.loads(row["listing_json"]),
        "evaluation": json.loads(row["evaluation_json"]),
        "rendered_telegram_text": render_phase1_listing_alert(snapshot).text,
    }


def _full_callback_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["audit_id"]),
        "type": _TYPE_CALLBACK,
        "timestamp": _iso_z(row["received_at"]),
        "alert_id": str(row["alert_id"]),
        "telegram_message_id": int(row["telegram_message_id"]),
        "chat_id": int(row["chat_id"]),
        "callback_data": row["callback_data"],
        "verb": row["verb"],
    }


# ─────────────────────────────────────────────────────────────────────────
# Filtering + rendering helpers
# ─────────────────────────────────────────────────────────────────────────


def _filter_records(
    records: list[dict[str, Any]],
    *,
    type_filter: str | None,
    since: datetime | None,
) -> list[dict[str, Any]]:
    out = records
    if type_filter is not None:
        out = [r for r in out if r["type"] == type_filter]
    if since is not None:
        out = [r for r in out if _record_dt(r) is not None and _record_dt(r) >= since]  # type: ignore[operator]
    return out


def _record_dt(record: dict[str, Any]) -> datetime | None:
    ts = record.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", _UTC_ISO_OFFSET))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_since(value: str | None) -> datetime | None:
    """Parse an ISO date/datetime to a UTC-aware datetime. None on bad input."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", _UTC_ISO_OFFSET))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso_z(value: str) -> str:
    """Normalize a UTC ISO 8601 string to the ``Z`` suffix form (UX-DR20)."""
    return value[:-6] + "Z" if value.endswith(_UTC_ISO_OFFSET) else value


def _render_show_table(records: list[dict[str, Any]], *, width: int) -> None:
    if not records:
        render_prose("no audit records found", style="info")
        return
    columns: list[ColumnSpec] = [
        {"key": "id", "header": "ID"},
        {"key": "type", "header": "Type"},
        {"key": "timestamp", "header": "Timestamp"},
        {"key": "summary", "header": "Summary"},
    ]
    table = render_table(records, columns, width=width)
    _print_table(table, width=width)


__all__ = ["run_export", "run_show"]
