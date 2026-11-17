"""Tests for ``phase2 smoke-test`` + ``phase2 reconcile`` — Story 5.13."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from salvager.adapters.sqlite_store import (
    MigrationRunner,
    Phase2AuditWriter,
    open_connection,
)
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.cli.commands import phase2_cmd
from salvager.domain.alert import EventName
from salvager.domain.phase2_audit import TransactionRecord
from salvager.orchestration.degradation_reporter import Reporter
from salvager.orchestration.smoke_test import PriceParser

_T0 = datetime(2026, 5, 16, 6, 0, 0, tzinfo=UTC)
_ENTRY_KEY = ("Western Digital", "WD Red Plus 4TB", "WD40EFPX")


class _RecordingReporter(Reporter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, EventName, dict[str, Any]]] = []

    async def report(
        self,
        severity: str,
        event: EventName,
        ctx: Mapping[str, Any],
    ) -> bool:
        self.calls.append((severity, event, dict(ctx)))
        return True


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[Path, Path, Path, Path]]:
    """Provide (env_path, config_path, data_dir, fixtures_dir)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("phase2:\n  reconciliation_tolerance_eur: 1.00\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=fake\n"
        "TELEGRAM_CHAT_ID=1\n"
        "GEMINI_API_KEY=fake\n"
        "EBAY_APP_ID=fake\n"
        "EBAY_CERT_ID=fake\n"
        "EBAY_DEV_ID=fake\n"
        "TINYFISH_API_KEY=fake\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    connection = open_connection(db_path_under(data_dir))
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    yield env_path, config_path, data_dir, fixtures_dir


def _write_fixture(
    fixtures_dir: Path,
    name: str,
    *,
    body: str,
    kind: str,
    expected_price: str,
    ext: str = ".json",
) -> None:
    (fixtures_dir / f"{name}{ext}").write_text(body, encoding="utf-8")
    (fixtures_dir / f"{name}.expected.json").write_text(
        json.dumps({"kind": kind, "price_eur": expected_price}),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────────────────
# phase2 smoke-test
# ─────────────────────────────────────────────────────────────────────────


def test_smoke_test_all_pass_exits_zero(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_path, config_path, data_dir, fixtures_dir = workspace
    _write_fixture(
        fixtures_dir,
        "wallapop_api_typical",
        body="55.00",
        kind="wallapop_api",
        expected_price="55.00",
    )

    parsers: dict[str, PriceParser] = {"wallapop_api": lambda body: Decimal(body.decode("utf-8"))}
    reporter = _RecordingReporter()

    code = phase2_cmd.run_smoke_test(
        env_path=env_path,
        config_path=config_path,
        data_dir=data_dir,
        fixtures_dir=fixtures_dir,
        reporter_factory=lambda: reporter,
        parsers=parsers,
    )
    assert code == 0
    assert reporter.calls == []
    out = capsys.readouterr().out
    assert "wallapop_api_typical" in out
    assert "PASS" in out
    assert "Overall: pass" in out


def test_smoke_test_any_failure_exits_five_and_locks_phase2(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_path, config_path, data_dir, fixtures_dir = workspace
    _write_fixture(
        fixtures_dir,
        "wallapop_html_comma_vs_dot",
        body="0.53",
        kind="wallapop_html",
        expected_price="53.00",
        ext=".html",
    )

    parsers: dict[str, PriceParser] = {"wallapop_html": lambda body: Decimal(body.decode("utf-8"))}
    reporter = _RecordingReporter()

    code = phase2_cmd.run_smoke_test(
        env_path=env_path,
        config_path=config_path,
        data_dir=data_dir,
        fixtures_dir=fixtures_dir,
        reporter_factory=lambda: reporter,
        parsers=parsers,
    )
    assert code == 5
    assert any(event is EventName.smoke_test_failed for _, event, _ in reporter.calls)
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "Overall: fail" in out


def test_smoke_test_missing_fixtures_dir_is_usage_error(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_path, config_path, data_dir, _ = workspace
    missing_dir = data_dir / "nope"
    code = phase2_cmd.run_smoke_test(
        env_path=env_path,
        config_path=config_path,
        data_dir=data_dir,
        fixtures_dir=missing_dir,
        reporter_factory=lambda: _RecordingReporter(),
        parsers={},
    )
    assert code == 2
    assert "fixtures not found" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────
# phase2 reconcile
# ─────────────────────────────────────────────────────────────────────────


def _seed_alert_and_transaction(
    data_dir: Path,
    *,
    alert_price: str,
    receipt_price: str,
    receipt_id: str = "WP-2026-0001",
) -> str:
    """Write one alert_snapshots row + one transactions row to the DB.

    Returns the ``alert_id`` so the test can correlate downstream.
    """
    alert_id = str(uuid4())
    listing_json = json.dumps(
        {
            "listing_id": "abc123",
            "marketplace": "wallapop",
            "url": "https://es.wallapop.com/item/abc123",
            "title": "WD Red Plus 4TB",
            "description": "ok",
            "price_eur": alert_price,
            "location": "Madrid",
            "photo_urls": [],
            "fetched_at": _T0.isoformat(),
        }
    )
    evaluation_json = json.dumps(
        {
            "listing_id": "abc123",
            "entry_key": list(_ENTRY_KEY),
            "confidence": "high",
            "one_line_take": "Strong match.",
            "is_container": False,
            "evaluated_at": _T0.isoformat(),
        }
    )
    connection = open_connection(db_path_under(data_dir))
    try:
        connection.execute(
            """
            INSERT INTO alert_snapshots (
                alert_id, entry_manufacturer, entry_model, entry_ref,
                entry_display_name, listing_json, evaluation_json,
                phase, phase2_max_price_eur, rendered_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                *_ENTRY_KEY,
                "WD Red Plus 4TB",
                listing_json,
                evaluation_json,
                "phase2",
                "60.00",
                _T0.isoformat(),
            ),
        )
    finally:
        connection.close()

    import asyncio

    async def _write_tx() -> None:
        writer = Phase2AuditWriter(db_path_under(data_dir))
        try:
            from uuid import UUID

            await writer.record_transaction(
                TransactionRecord(
                    alert_id=UUID(alert_id),
                    price_paid_eur=Decimal(receipt_price),
                    payment_method="wallapop_pay",
                    receipt_id=receipt_id,
                    screenshot_path="/tmp/x.png",
                    total_seconds=42,
                    committed_at=_T0,
                )
            )
        finally:
            await writer.close()

    asyncio.run(_write_tx())
    return alert_id


def test_reconcile_match_exits_zero(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, config_path, data_dir, _ = workspace
    _seed_alert_and_transaction(
        data_dir, alert_price="55.00", receipt_price="55.50", receipt_id="WP-OK"
    )

    code = phase2_cmd.run_reconcile(
        receipt_or_audit_id="WP-OK",
        config_path=config_path,
        data_dir=data_dir,
        output_format="human",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Reconciliation PASSED" in out
    assert "WP-OK" in out


def test_reconcile_mismatch_exits_five(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, config_path, data_dir, _ = workspace
    _seed_alert_and_transaction(
        data_dir, alert_price="48.00", receipt_price="56.00", receipt_id="WP-BAD"
    )

    code = phase2_cmd.run_reconcile(
        receipt_or_audit_id="WP-BAD",
        config_path=config_path,
        data_dir=data_dir,
    )
    assert code == 5
    assert "Reconciliation FAILED" in capsys.readouterr().err + capsys.readouterr().out


def test_reconcile_json_format(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, config_path, data_dir, _ = workspace
    _seed_alert_and_transaction(
        data_dir, alert_price="55.00", receipt_price="55.00", receipt_id="WP-JSON"
    )
    code = phase2_cmd.run_reconcile(
        receipt_or_audit_id="WP-JSON",
        config_path=config_path,
        data_dir=data_dir,
        output_format="json",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["receipt_id"] == "WP-JSON"
    assert payload["passed"] is True
    assert payload["alert_price_eur"] == "55.00"
    assert payload["receipt_price_eur"] == "55.00"


def test_reconcile_unknown_receipt_exits_one(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, config_path, data_dir, _ = workspace
    code = phase2_cmd.run_reconcile(
        receipt_or_audit_id="WP-DOES-NOT-EXIST",
        config_path=config_path,
        data_dir=data_dir,
    )
    assert code == 1
    assert "not found in audit log" in capsys.readouterr().err


def test_reconcile_audit_id_fallback(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Numeric arg falls back to audit_id lookup."""
    _env, config_path, data_dir, _ = workspace
    _seed_alert_and_transaction(
        data_dir, alert_price="55.00", receipt_price="55.50", receipt_id="WP-NUM"
    )
    # First transaction → audit_id = 1.
    code = phase2_cmd.run_reconcile(
        receipt_or_audit_id="1",
        config_path=config_path,
        data_dir=data_dir,
    )
    assert code == 0


def test_reconcile_unknown_format_is_usage_error(
    workspace: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env, config_path, data_dir, _ = workspace
    code = phase2_cmd.run_reconcile(
        receipt_or_audit_id="anything",
        config_path=config_path,
        data_dir=data_dir,
        output_format="yaml",
    )
    assert code == 2
    assert "unknown --format" in capsys.readouterr().err
