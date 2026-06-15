"""Smoke-test orchestrator behaviour — Story 5.6 (FR33 / NFR-M3).

The pure tolerance math is exercised in ``test_reconciliation.py``;
this module covers the orchestrator's wiring:

  - one ``record_smoke_test`` row written per fixture;
  - any failing fixture trips ``set_global_disable`` AND fires
    ``smoke_test_failed`` exactly once, with the first failure in ctx;
  - all-pass after a previously-failed run fires ``smoke_test_recovered``;
  - parser exceptions are recorded as failures (not swallowed);
  - the canonical v1.0 fixture set lives in ``tests/fixtures/price_parsers/active``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import salvager
from salvager.adapters.sqlite_store import (
    MigrationRunner,
    Phase2AuditWriter,
    open_connection,
)
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.adapters.sqlite_store.phase2_state_reader import (
    SqlitePhase2StateReader,
)
from salvager.domain.alert import EventName
from salvager.orchestration.degradation_reporter import Reporter
from salvager.orchestration.smoke_test import (
    SMOKE_TEST_FAILED_REASON,
    PriceParser,
    discover_fixtures,
    run_smoke_test,
)

_T0 = datetime(2026, 5, 15, 6, 0, 0, tzinfo=UTC)
_TOL_EUR = Decimal("1.00")
_TOL_PCT = Decimal("5")

# Smoke fixtures ship as package data under src/salvager/smoke_fixtures/.
SHIPPED_FIXTURES = (
    Path(salvager.__file__).resolve().parent / "smoke_fixtures" / "price_parsers" / "active"
)

_REQUIRED_FIXTURES = (
    "wallapop_api_typical",
    "wallapop_html_typical",
    "ebay_api_typical",
    "wallapop_html_comma_vs_dot",
)


class _RecordingReporter(Reporter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, EventName, dict[str, Any]]] = []

    async def report(
        self,
        severity: str,
        event: EventName,
        ctx: Mapping[str, Any],
    ) -> None:
        self.calls.append((severity, event, dict(ctx)))


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    db_path = db_path_under(tmp_path)
    connection = open_connection(db_path)
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    return db_path


def _write_fixture_pair(
    directory: Path,
    name: str,
    *,
    body: str,
    kind: str,
    expected_price: str,
    ext: str = ".json",
) -> None:
    (directory / f"{name}{ext}").write_text(body, encoding="utf-8")
    (directory / f"{name}.expected.json").write_text(
        json.dumps({"kind": kind, "price_eur": expected_price}),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────────────────
# Canonical v1.0 fixture set lives where the AC says it does
# ─────────────────────────────────────────────────────────────────────────


def test_canonical_fixture_set_is_present() -> None:
    """Story 5.6 names four fixtures that v1.0 must ship. They live here."""
    for name in _REQUIRED_FIXTURES:
        siblings = list(SHIPPED_FIXTURES.glob(f"{name}.*"))
        # One response file + one expected.json sibling.
        suffixes = sorted(p.name.removeprefix(name) for p in siblings)
        assert ".expected.json" in suffixes, f"{name} missing .expected.json"
        # The non-expected sibling defines the response — JSON or HTML.
        response_exts = [s for s in suffixes if s != ".expected.json"]
        assert response_exts, f"{name} has no response file"


def test_shipped_fixtures_discover_cleanly() -> None:
    fixtures = discover_fixtures(SHIPPED_FIXTURES)
    by_name = {f.name: f for f in fixtures}
    for name in _REQUIRED_FIXTURES:
        assert name in by_name
        assert by_name[name].kind in {"wallapop_api", "wallapop_html", "ebay_api"}
        assert by_name[name].expected_price_eur > Decimal("0")


def test_missing_expected_json_raises(tmp_path: Path) -> None:
    (tmp_path / "orphan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"orphan\.expected\.json"):
        discover_fixtures(tmp_path)


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        discover_fixtures(tmp_path / "does_not_exist")


# ─────────────────────────────────────────────────────────────────────────
# run_smoke_test — happy path
# ─────────────────────────────────────────────────────────────────────────


async def _make_writer_and_reader(
    db_path: Path,
) -> tuple[Phase2AuditWriter, SqlitePhase2StateReader]:
    writer = Phase2AuditWriter(db_path)
    reader = SqlitePhase2StateReader(db_path)
    return writer, reader


async def test_all_pass_records_rows_and_does_not_lock(migrated_db: Path, tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "active"
    fixtures_dir.mkdir()
    _write_fixture_pair(
        fixtures_dir,
        "wallapop_api_typical",
        body="55.00",
        kind="wallapop_api",
        expected_price="55.00",
    )
    _write_fixture_pair(
        fixtures_dir,
        "ebay_api_typical",
        body="62.50",
        kind="ebay_api",
        expected_price="62.50",
    )
    fixtures = discover_fixtures(fixtures_dir)

    # Stub parsers: read the response file's text as the price string.
    parsers: dict[str, PriceParser] = {
        "wallapop_api": lambda body: Decimal(body.decode("utf-8")),
        "ebay_api": lambda body: Decimal(body.decode("utf-8")),
    }
    reporter = _RecordingReporter()
    writer, reader = await _make_writer_and_reader(migrated_db)
    try:
        summary = await run_smoke_test(
            fixtures=fixtures,
            parsers=parsers,
            audit_writer=writer,
            state_reader=reader,
            reporter=reporter,
            tolerance_eur=_TOL_EUR,
            tolerance_pct=_TOL_PCT,
            clock=lambda: _T0,
        )

        assert summary.any_failed is False
        assert summary.fired_lockout is False
        assert summary.fired_recovery is False
        assert reporter.calls == []

        state = await reader.read()
        assert state.globally_disabled is False
        assert state.last_smoke_result == "pass"
        assert state.last_smoke_at == _T0
    finally:
        await writer.close()
        await reader.close()

    # Audit rows: one per fixture, all pass.
    connection = open_connection(migrated_db)
    try:
        rows = connection.execute(
            "SELECT result FROM phase2_smoke_tests ORDER BY audit_id"
        ).fetchall()
    finally:
        connection.close()
    assert [r["result"] for r in rows] == ["pass", "pass"]


# ─────────────────────────────────────────────────────────────────────────
# Failure paths
# ─────────────────────────────────────────────────────────────────────────


async def test_any_failure_locks_phase2_and_fires_smoke_test_failed(
    migrated_db: Path, tmp_path: Path
) -> None:
    fixtures_dir = tmp_path / "active"
    fixtures_dir.mkdir()
    _write_fixture_pair(
        fixtures_dir,
        "wallapop_html_comma_vs_dot",
        body="0.53",
        kind="wallapop_html",
        expected_price="53.00",
        ext=".html",
    )
    _write_fixture_pair(
        fixtures_dir,
        "wallapop_api_typical",
        body="55.00",
        kind="wallapop_api",
        expected_price="55.00",
    )
    fixtures = discover_fixtures(fixtures_dir)

    parsers: dict[str, PriceParser] = {
        "wallapop_html": lambda body: Decimal(body.decode("utf-8")),
        "wallapop_api": lambda body: Decimal(body.decode("utf-8")),
    }
    reporter = _RecordingReporter()
    writer, reader = await _make_writer_and_reader(migrated_db)
    try:
        summary = await run_smoke_test(
            fixtures=fixtures,
            parsers=parsers,
            audit_writer=writer,
            state_reader=reader,
            reporter=reporter,
            tolerance_eur=_TOL_EUR,
            tolerance_pct=_TOL_PCT,
            clock=lambda: _T0,
        )

        assert summary.any_failed is True
        assert summary.fired_lockout is True
        assert summary.fired_recovery is False

        state = await reader.read()
        assert state.globally_disabled is True
        assert state.disabled_reason == SMOKE_TEST_FAILED_REASON
        # ``last_smoke_result`` mirrors the freshest ``record_smoke_test``
        # row written this run — alphabetic order puts the failing
        # ``wallapop_html_comma_vs_dot`` last, so the mirror is "fail".
        assert state.last_smoke_result == "fail"

        # Exactly one Telegram alert; ctx names the failing fixture.
        assert len(reporter.calls) == 1
        severity, event, ctx = reporter.calls[0]
        assert severity == "warn"
        assert event is EventName.smoke_test_failed
        assert ctx["fixture_name"] == "wallapop_html_comma_vs_dot"
        assert ctx["expected_price"] == "53.00"
        assert ctx["parsed_price"] == "0.53"
    finally:
        await writer.close()
        await reader.close()


async def test_parser_exception_is_recorded_as_failure(migrated_db: Path, tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "active"
    fixtures_dir.mkdir()
    _write_fixture_pair(
        fixtures_dir,
        "broken",
        body="not a number",
        kind="wallapop_api",
        expected_price="55.00",
    )

    def _exploding_parser(_body: bytes) -> Decimal:
        raise ValueError("can't parse")

    reporter = _RecordingReporter()
    writer, reader = await _make_writer_and_reader(migrated_db)
    try:
        summary = await run_smoke_test(
            fixtures=discover_fixtures(fixtures_dir),
            parsers={"wallapop_api": _exploding_parser},
            audit_writer=writer,
            state_reader=reader,
            reporter=reporter,
            tolerance_eur=_TOL_EUR,
            tolerance_pct=_TOL_PCT,
            clock=lambda: _T0,
        )
        assert summary.any_failed is True
        outcome = summary.outcomes[0]
        assert outcome.parser_error_class == "ValueError"
        assert outcome.parsed_price_eur is None
        # Reporter fires with the parser-error class surfaced in ctx.
        _severity, _event, ctx = reporter.calls[0]
        assert ctx["parser_error_class"] == "ValueError"
    finally:
        await writer.close()
        await reader.close()


async def test_missing_parser_for_kind_is_recorded_as_failure(
    migrated_db: Path, tmp_path: Path
) -> None:
    fixtures_dir = tmp_path / "active"
    fixtures_dir.mkdir()
    _write_fixture_pair(
        fixtures_dir,
        "weirdo",
        body="anything",
        kind="future_marketplace",
        expected_price="10.00",
    )

    reporter = _RecordingReporter()
    writer, reader = await _make_writer_and_reader(migrated_db)
    try:
        summary = await run_smoke_test(
            fixtures=discover_fixtures(fixtures_dir),
            parsers={},  # nothing registered for "future_marketplace"
            audit_writer=writer,
            state_reader=reader,
            reporter=reporter,
            tolerance_eur=_TOL_EUR,
            tolerance_pct=_TOL_PCT,
            clock=lambda: _T0,
        )
        assert summary.any_failed is True
        assert summary.outcomes[0].parser_error_class == "ParserNotRegistered"
    finally:
        await writer.close()
        await reader.close()


# ─────────────────────────────────────────────────────────────────────────
# Recovery — all-pass after a previously failed run
# ─────────────────────────────────────────────────────────────────────────


async def test_all_pass_after_previous_fail_fires_recovered(
    migrated_db: Path, tmp_path: Path
) -> None:
    fixtures_dir = tmp_path / "active"
    fixtures_dir.mkdir()
    _write_fixture_pair(
        fixtures_dir,
        "wallapop_api_typical",
        body="55.00",
        kind="wallapop_api",
        expected_price="55.00",
    )

    parsers: dict[str, PriceParser] = {
        "wallapop_api": lambda body: Decimal(body.decode("utf-8")),
    }
    reporter = _RecordingReporter()
    writer, reader = await _make_writer_and_reader(migrated_db)
    try:
        # Seed a previous failed run — write a fail row + set the lockout
        # so the orchestrator sees the "previously failed" precondition.
        from salvager.domain.phase2_audit import SmokeTestRecord

        await writer.record_smoke_test(
            SmokeTestRecord(
                run_at=_T0,
                result="fail",
                parsed_price=Decimal("0.53"),
                independent_price=Decimal("53.00"),
                delta_eur=Decimal("52.47"),
                delta_pct=Decimal("99.0"),
            )
        )
        await writer.set_global_disable(SMOKE_TEST_FAILED_REASON)

        summary = await run_smoke_test(
            fixtures=discover_fixtures(fixtures_dir),
            parsers=parsers,
            audit_writer=writer,
            state_reader=reader,
            reporter=reporter,
            tolerance_eur=_TOL_EUR,
            tolerance_pct=_TOL_PCT,
            clock=lambda: _T0,
        )

        assert summary.any_failed is False
        assert summary.fired_recovery is True
        assert summary.fired_lockout is False

        # Recovery alert dispatched, no failure alert.
        assert [event for _, event, _ in reporter.calls] == [EventName.smoke_test_recovered]

        # NFR-R4: recovery alert does NOT clear the lockout — only
        # `phase2 enable` does. The lockout flag stays.
        state = await reader.read()
        assert state.globally_disabled is True
    finally:
        await writer.close()
        await reader.close()
