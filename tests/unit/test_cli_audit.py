"""Tests for ``salvager audit show`` / ``audit export`` — Story 4.5.

Fixtures seed a migrated DB with known ``alert_snapshots`` / ``callbacks``
/ ``seen_listings`` rows; the tests assert on the rendered table, the
JSON shapes, the filters, and the single-record full-detail view.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.adapters.sqlite_store.migrations import MigrationRunner, db_path_under
from salvager.cli.commands.audit_cmd import run_export, run_show
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing

_T0 = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


def _listing(listing_id: str) -> Listing:
    return Listing(
        listing_id=listing_id,
        marketplace="wallapop",
        url=f"https://es.wallapop.com/item/{listing_id}",
        title="WD Red Plus 4TB",
        description="Como nuevo",
        price_eur=Decimal("55.00"),
        location="Madrid",
        photo_urls=["https://cdn/photo.jpg"],
        fetched_at=_T0,
    )


def _evaluation(listing_id: str, *, confidence: str = "high") -> ListingEvaluation:
    return ListingEvaluation(
        listing_id=listing_id,
        entry_key=("Western Digital", "WD Red Plus 4TB", "WD40EFPX"),
        confidence=confidence,  # type: ignore[arg-type]
        one_line_take="Strong match.",
        is_container=False,
        evaluated_at=_T0,
    )


def _seed(
    tmp_path: Path,
    *,
    alerts: int = 0,
    callbacks: int = 0,
    dropped: int = 0,
    fired: int = 0,
) -> Path:
    """Build a migrated data_dir seeded with the requested audit rows.

    Timestamps step backwards from ``_T0`` so ordering is deterministic:
    row 0 is newest. ``fired`` adds seen_listings rows with
    ``match_fired = 1`` — those must NOT appear as dropped records.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = db_path_under(data_dir)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
        for n in range(alerts):
            ts = (_T0 - timedelta(minutes=n)).isoformat()
            connection.execute(
                """
                INSERT INTO alert_snapshots (
                    alert_id, entry_manufacturer, entry_model, entry_ref,
                    entry_display_name, listing_json, evaluation_json,
                    phase, rendered_at
                ) VALUES (?, 'Western Digital', 'WD Red Plus 4TB', 'WD40EFPX',
                          'WD Red Plus 4TB (WD40EFPX)', ?, ?, 'phase1', ?)
                """,
                (
                    f"00000000-0000-4000-8000-{n:012d}",
                    _listing(f"alert{n}").model_dump_json(),
                    _evaluation(f"alert{n}").model_dump_json(),
                    ts,
                ),
            )
        for n in range(callbacks):
            ts = (_T0 - timedelta(hours=1, minutes=n)).isoformat()
            connection.execute(
                """
                INSERT INTO callbacks (
                    alert_id, telegram_message_id, chat_id,
                    callback_data, verb, received_at
                ) VALUES (?, ?, 12345, ?, 'view', ?)
                """,
                (
                    f"11111111-1111-4111-8111-{n:012d}",
                    1000 + n,
                    f"listing:view:11111111-1111-4111-8111-{n:012d}",
                    ts,
                ),
            )
        for n in range(dropped):
            ts = (_T0 - timedelta(hours=2, minutes=n)).isoformat()
            connection.execute(
                """
                INSERT INTO seen_listings (
                    listing_id, entry_manufacturer, entry_model, entry_ref,
                    url, first_seen_at, last_seen_at, match_fired
                ) VALUES (?, 'Western Digital', 'WD Red Plus 4TB', 'WD40EFPX',
                          ?, ?, ?, 0)
                """,
                (f"drop{n}", f"https://es.wallapop.com/item/drop{n}", ts, ts),
            )
        for n in range(fired):
            ts = (_T0 - timedelta(hours=3, minutes=n)).isoformat()
            connection.execute(
                """
                INSERT INTO seen_listings (
                    listing_id, entry_manufacturer, entry_model, entry_ref,
                    url, first_seen_at, last_seen_at, match_fired
                ) VALUES (?, 'Western Digital', 'WD Red Plus 4TB', 'WD40EFPX',
                          ?, ?, ?, 1)
                """,
                (f"fired{n}", f"https://es.wallapop.com/item/fired{n}", ts, ts),
            )
    finally:
        connection.close()
    return data_dir


# ─────────────────────────────────────────────────────────────────────────
# audit show — list view
# ─────────────────────────────────────────────────────────────────────────


def test_show_default_returns_ten_most_recent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=15)
    code = run_show(data_dir=data_dir, output_format="json")
    assert code == 0
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 10  # default --last
    # Newest first: alert0 has the most recent timestamp.
    assert records[0]["summary"].startswith("WD Red Plus 4TB (WD40EFPX)")
    assert records[0]["type"] == "alert"


def test_show_last_n_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = _seed(tmp_path, alerts=8, callbacks=8)
    code = run_show(data_dir=data_dir, last=5, output_format="json")
    assert code == 0
    assert len(json.loads(capsys.readouterr().out)) == 5


def test_show_last_zero_produces_no_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=3)
    code = run_show(data_dir=data_dir, last=0, output_format="json")
    assert code == 0
    assert capsys.readouterr().out == ""


def test_show_default_excludes_dropped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=2, dropped=5)
    records = json.loads(
        _run_capture(capsys, lambda: run_show(data_dir=data_dir, output_format="json"))
    )
    assert {r["type"] for r in records} == {"alert"}


def test_show_include_dropped_adds_dropped_but_not_fired(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=1, dropped=3, fired=4)
    records = json.loads(
        _run_capture(
            capsys,
            lambda: run_show(
                data_dir=data_dir, include_dropped=True, last=50, output_format="json"
            ),
        )
    )
    types = [r["type"] for r in records]
    # 1 alert + 3 dropped — the 4 match_fired=1 sightings are NOT dropped.
    assert types.count("dropped") == 3
    assert types.count("alert") == 1


def test_show_type_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = _seed(tmp_path, alerts=4, callbacks=3)
    records = json.loads(
        _run_capture(
            capsys,
            lambda: run_show(
                data_dir=data_dir, type_filter="callback", last=50, output_format="json"
            ),
        )
    )
    assert records and {r["type"] for r in records} == {"callback"}


def test_show_since_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # alerts step back by minutes from _T0; callbacks start an hour earlier.
    data_dir = _seed(tmp_path, alerts=5, callbacks=5)
    since = (_T0 - timedelta(minutes=30)).isoformat()
    records = json.loads(
        _run_capture(
            capsys,
            lambda: run_show(data_dir=data_dir, since=since, last=50, output_format="json"),
        )
    )
    # Only the 5 recent alerts clear the cutoff; the hour-old callbacks don't.
    assert {r["type"] for r in records} == {"alert"}
    assert len(records) == 5


def test_show_unknown_type_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=1)
    code = run_show(data_dir=data_dir, type_filter="bogus")
    assert code == 2
    assert "unknown --type" in capsys.readouterr().err


def test_show_human_table_renders(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=2, callbacks=1)
    code = run_show(data_dir=data_dir, output_format="human", width=80)
    assert code == 0
    out = capsys.readouterr().out
    assert "ID" in out and "Type" in out and "Summary" in out
    assert "alert" in out and "callback" in out


def test_show_empty_log_reports_no_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path)  # migrated but empty
    code = run_show(data_dir=data_dir, output_format="human")
    assert code == 0
    assert "no audit records" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────
# audit show --id — single record full detail
# ─────────────────────────────────────────────────────────────────────────


def test_show_id_returns_full_alert_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=3)
    # audit_id autoincrements from 1; row 0 (newest) is audit_id 1.
    code = run_show(data_dir=data_dir, record_id=1)
    assert code == 0
    record = json.loads(capsys.readouterr().out)
    assert record["type"] == "alert"
    assert record["id"] == 1
    # Full detail: nested listing + evaluation + re-rendered Telegram text.
    assert record["listing"]["listing_id"] == "alert0"
    assert record["evaluation"]["confidence"] == "high"
    assert "rendered_telegram_text" in record
    assert "WD Red Plus 4TB" in record["rendered_telegram_text"]


def test_show_id_returns_full_callback_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, callbacks=2)
    code = run_show(data_dir=data_dir, record_id=1)
    assert code == 0
    record = json.loads(capsys.readouterr().out)
    assert record["type"] == "callback"
    assert record["verb"] == "view"
    assert record["chat_id"] == 12345


def test_show_missing_id_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=2)
    code = run_show(data_dir=data_dir, record_id=999)
    assert code == 1
    err = capsys.readouterr().err
    assert "audit id 999 not found" in err
    assert "audit show --last 5" in err  # hint


# ─────────────────────────────────────────────────────────────────────────
# audit export — JSONL
# ─────────────────────────────────────────────────────────────────────────


def test_export_emits_one_json_object_per_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=3, callbacks=2, dropped=4)
    code = run_export(data_dir=data_dir)
    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    # 3 alerts + 2 callbacks — dropped sightings are NOT part of the export.
    assert len(lines) == 5
    for line in lines:
        record = json.loads(line)  # each line parses on its own
        assert record["type"] in ("alert", "callback")
    # Chronological order (oldest first) for export.
    timestamps = [json.loads(line)["timestamp"] for line in lines]
    assert timestamps == sorted(timestamps)


def test_export_since_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = _seed(tmp_path, alerts=5, callbacks=5)
    since = (_T0 - timedelta(minutes=30)).isoformat()
    code = run_export(data_dir=data_dir, since=since)
    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 5  # only the recent alerts clear the cutoff


# ─────────────────────────────────────────────────────────────────────────
# Golden JSON — audit show --format json --last 3
# ─────────────────────────────────────────────────────────────────────────


def test_show_json_golden(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    data_dir = _seed(tmp_path, alerts=3, callbacks=3)
    code = run_show(data_dir=data_dir, last=3, output_format="json")
    assert code == 0
    # Pretty-print so the golden file is reviewable line-by-line.
    payload = json.loads(capsys.readouterr().out)
    assert json.dumps(payload, indent=2) == snapshot


def _run_capture(capsys: pytest.CaptureFixture[str], fn: object) -> str:
    """Run ``fn`` and return its captured stdout (test ergonomics helper)."""
    fn()  # type: ignore[operator]
    return capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────
# audit show --id — alert update history (edit-alerts-on-state-change)
# ─────────────────────────────────────────────────────────────────────────


def _seed_updates(data_dir: Path, alert_id: str, updates: list[tuple[str, str, str, int]]) -> None:
    """Append alert_updates rows: (change_kind, old, new, edit_ok)."""
    connection = open_connection(db_path_under(data_dir))
    try:
        for n, (kind, old, new, ok) in enumerate(updates):
            connection.execute(
                """
                INSERT INTO alert_updates (
                    alert_id, change_kind, old_value, new_value,
                    edited_at, edit_ok, rendered_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    kind,
                    old,
                    new,
                    (_T0 + timedelta(minutes=n)).isoformat(),
                    ok,
                    f"BANNER {kind}\nre-rendered body {n}",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_show_id_alert_without_updates_has_no_updates_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=1)
    assert run_show(data_dir=data_dir, record_id=1) == 0
    record = json.loads(capsys.readouterr().out)
    assert "updates" not in record


def test_show_id_alert_replays_update_history_in_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _seed(tmp_path, alerts=1)
    alert_id = "00000000-0000-4000-8000-000000000000"
    _seed_updates(
        data_dir,
        alert_id,
        [
            ("price_drop", "100.00", "80.00", 1),
            ("reserved", "False", "True", 0),
        ],
    )

    assert run_show(data_dir=data_dir, record_id=1) == 0
    record = json.loads(capsys.readouterr().out)
    updates = record["updates"]
    assert [u["change_kind"] for u in updates] == ["price_drop", "reserved"]
    assert updates[0]["edit_ok"] is True
    assert updates[1]["edit_ok"] is False
    # The operator-visible body is replayable for every attempt.
    assert updates[0]["rendered_telegram_text"].startswith("BANNER price_drop")
    assert updates[1]["rendered_telegram_text"].startswith("BANNER reserved")
