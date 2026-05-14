"""Tests for ``hardware-hunter health`` — Story 4.4 (FR47 / NFR-O2 / UX-DR25).

The command reads SQLite ``_meta`` + ``alert_snapshots`` + the
filesystem directly — no running daemon required (AR14). Fixtures
build a representative ``data_dir`` and the tests assert on rendered
output (golden snapshots at four terminal widths) + the JSON shape +
the status-derivation edge cases.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion

from hardware_hunter.adapters.sqlite_store.connection import open_connection
from hardware_hunter.adapters.sqlite_store.migrations import MigrationRunner, db_path_under
from hardware_hunter.cli.commands import health_cmd
from hardware_hunter.cli.commands.health_cmd import run

# A fixed "now" so humanized "ago" strings + 24h cutoffs are deterministic.
_NOW = datetime(2026, 5, 14, 18, 0, 0, tzinfo=UTC)

_CONFIG_YAML = textwrap.dedent(
    """\
    schedule:
      wallapop_minutes: 15
      ebay_minutes: 30
    """
)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG_YAML, encoding="utf-8")
    return path


def _data_dir(tmp_path: Path, *, meta: dict[str, str], match_count: int = 0) -> Path:
    """Build a migrated data_dir with the given _meta rows + N alert snapshots."""
    data_dir = tmp_path / "data"
    (data_dir / "auth").mkdir(parents=True)
    db_path = db_path_under(data_dir)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
        for key, value in meta.items():
            connection.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                (key, value),
            )
        for n in range(match_count):
            connection.execute(
                """
                INSERT INTO alert_snapshots (
                    alert_id, entry_manufacturer, entry_model, entry_ref,
                    entry_display_name, listing_json, evaluation_json,
                    phase, rendered_at
                ) VALUES (?, 'WD', 'Red Plus 4TB', 'WD40EFPX', 'WD Red Plus 4TB',
                          '{}', '{}', 'phase1', ?)
                """,
                (f"00000000-0000-4000-8000-{n:012d}", (_NOW - timedelta(hours=1)).isoformat()),
            )
    finally:
        connection.close()
    return data_dir


def _with_credentials(data_dir: Path, *, cookie: bool = True, tokens: bool = True) -> None:
    if cookie:
        (data_dir / "auth" / "wallapop_cookies.txt").write_text("# cookies", encoding="utf-8")
    if tokens:
        (data_dir / "auth" / "oauth_tokens.json").write_text("{}", encoding="utf-8")


def _healthy_meta() -> dict[str, str]:
    """A running daemon that polled both marketplaces minutes ago."""
    return {
        "daemon_pid": "99999",
        "daemon_started_at": (_NOW - timedelta(hours=3)).isoformat(),
        "daemon_version": "0.1.0",
        "last_poll_wallapop": (_NOW - timedelta(minutes=4)).isoformat(),
        "last_poll_ebay": (_NOW - timedelta(minutes=12)).isoformat(),
        "wallapop_api_status": "healthy",
    }


@pytest.fixture
def _pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the PID-liveness probe True so 'running' snapshots are stable."""
    monkeypatch.setattr(health_cmd, "_pid_alive", lambda pid: True)


# ─────────────────────────────────────────────────────────────────────────
# Golden snapshots — rendering at four terminal widths (UX-DR31)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", [60, 80, 100, 120])
@pytest.mark.usefixtures("_pid_alive")
def test_health_human_output_snapshot(
    tmp_path: Path,
    width: int,
    snapshot: SnapshotAssertion,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _data_dir(tmp_path, meta=_healthy_meta(), match_count=2)
    _with_credentials(data_dir)

    code = run(
        data_dir=data_dir,
        config_path=_config(tmp_path),
        output_format="human",
        width=width,
        now=_NOW,
    )
    assert code == 0
    assert capsys.readouterr().out == snapshot


# ─────────────────────────────────────────────────────────────────────────
# JSON output shape (UX-DR20)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_pid_alive")
def test_health_json_output_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _data_dir(tmp_path, meta=_healthy_meta(), match_count=3)
    _with_credentials(data_dir)

    code = run(
        data_dir=data_dir,
        config_path=_config(tmp_path),
        output_format="json",
        now=_NOW,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["version"] == "0.1.0"
    assert payload["pid"] == 99999
    assert payload["daemon_running"] is True
    assert payload["uptime_seconds"] == 3 * 3600
    assert payload["recent_match_count_24h"] == 3
    # Timestamps are normalized to the UX-DR20 `Z` suffix on output.
    expected_wallapop = _healthy_meta()["last_poll_wallapop"].replace("+00:00", "Z")
    assert payload["last_poll"]["wallapop"] == expected_wallapop
    # Every adapter row carries name + status + last_activity.
    names = {a["name"] for a in payload["adapters"]}
    assert names == {"wallapop_api", "wallapop_tinyfish", "ebay_api"}
    assert all(a["status"] == "healthy" for a in payload["adapters"])
    # Phase 2 is stubbed (Epic 5) but the shape is present.
    assert payload["phase2"]["circuit_breaker"]["state"] == "closed"


# ─────────────────────────────────────────────────────────────────────────
# Daemon-down case — exit 0, reads state without a running process (AR14)
# ─────────────────────────────────────────────────────────────────────────


def test_health_with_dead_daemon_pid_reports_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(health_cmd, "_pid_alive", lambda pid: False)
    data_dir = _data_dir(tmp_path, meta=_healthy_meta())
    _with_credentials(data_dir)

    code = run(data_dir=data_dir, config_path=_config(tmp_path), now=_NOW)

    # A stopped daemon is not an error condition.
    assert code == 0
    assert "Daemon: not running" in capsys.readouterr().out


def test_health_with_no_database_reports_not_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh install — no DB, no daemon ever — must not crash."""
    data_dir = tmp_path / "data"
    (data_dir / "auth").mkdir(parents=True)

    code = run(data_dir=data_dir, config_path=_config(tmp_path), now=_NOW)
    assert code == 0
    out = capsys.readouterr().out
    assert "Daemon: not running" in out
    assert "Last poll: never" in out


# ─────────────────────────────────────────────────────────────────────────
# Status derivation — stuck poller + degraded API path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("_pid_alive")
def test_stuck_poller_marks_marketplace_degraded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    meta = _healthy_meta()
    # Last Wallapop poll 5 hours ago — way past 2x the 15-min cadence.
    meta["last_poll_wallapop"] = (_NOW - timedelta(hours=5)).isoformat()
    data_dir = _data_dir(tmp_path, meta=meta)
    _with_credentials(data_dir)

    code = run(
        data_dir=data_dir,
        config_path=_config(tmp_path),
        output_format="json",
        now=_NOW,
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    status = {a["name"]: a["status"] for a in payload["adapters"]}
    # The stale poll degrades both Wallapop paths...
    assert status["wallapop_api"] == "degraded"
    assert status["wallapop_tinyfish"] == "degraded"
    # ...but eBay polled recently and stays healthy (NFR-R1 independence).
    assert status["ebay_api"] == "healthy"


@pytest.mark.usefixtures("_pid_alive")
def test_wallapop_api_degraded_while_tinyfish_healthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Session expired: the API path is flagged degraded in _meta, but
    TinyFish is still serving fresh polls — the table must show both."""
    meta = _healthy_meta()
    meta["wallapop_api_status"] = "degraded"  # written by the fallback fetcher
    data_dir = _data_dir(tmp_path, meta=meta)
    _with_credentials(data_dir)

    code = run(
        data_dir=data_dir,
        config_path=_config(tmp_path),
        output_format="json",
        now=_NOW,
    )
    assert code == 0
    status = {a["name"]: a["status"] for a in json.loads(capsys.readouterr().out)["adapters"]}
    assert status["wallapop_api"] == "degraded"
    assert status["wallapop_tinyfish"] == "healthy"


@pytest.mark.usefixtures("_pid_alive")
def test_missing_credential_files_mark_adapters_down(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _data_dir(tmp_path, meta=_healthy_meta())
    # No cookie, no tokens written.

    code = run(
        data_dir=data_dir,
        config_path=_config(tmp_path),
        output_format="json",
        now=_NOW,
    )
    assert code == 0
    status = {a["name"]: a["status"] for a in json.loads(capsys.readouterr().out)["adapters"]}
    assert status["wallapop_api"] == "down"
    assert status["ebay_api"] == "down"


# ─────────────────────────────────────────────────────────────────────────
# Bad --format value
# ─────────────────────────────────────────────────────────────────────────


def test_unknown_format_returns_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = _data_dir(tmp_path, meta=_healthy_meta())
    code = run(
        data_dir=data_dir,
        config_path=_config(tmp_path),
        output_format="yaml",
        now=_NOW,
    )
    assert code == 2
    assert "error" in capsys.readouterr().err.lower()
