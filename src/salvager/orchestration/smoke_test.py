"""Daily synthetic smoke test — Story 5.6 (FR33 / NFR-M3).

The Phase 2 buy path depends on the marketplace price parsers being
*correct*. Q9 (the canonical regression: a comma-vs-dot drift that
turns 53,00 € into 0,53 €) is exactly the silent failure the autonomous
checkout cannot survive. The smoke test runs once a day against a
growing set of recorded responses with independently-verified prices;
any drift trips a global Phase 2 lockout BEFORE a real listing has the
chance to be auto-bought.

Inputs
------
- A directory of fixtures laid out as pairs of files:

      <name>.<ext>            — the recorded marketplace response
      <name>.expected.json    — {"kind": "<parser-key>", "price_eur": "<Decimal>"}

  ``kind`` selects which parser in the registry handles this fixture.
- A registry ``Mapping[str, PriceParser]`` of ``kind → parser``. The
  parsers themselves are wired by the composer; the orchestrator only
  dispatches.

Outputs
-------
- One ``Phase2AuditWriter.record_smoke_test`` row per fixture (which
  also mirrors the latest result onto ``phase2_state``).
- On *any* failure: ``set_global_disable("smoke_test_failed")`` and a
  ``smoke_test_failed`` Telegram alert with the offending fixture's
  parsed/expected/delta values.
- On all-pass when the previous run failed: a ``smoke_test_recovered``
  info alert (the operator still has to ``phase2 enable`` to lift the
  lockout — NFR-R4).
- A structured ``smoke_test_started`` log at the top of each run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from salvager.adapters.sqlite_store.audit_writer import Phase2AuditWriter
from salvager.domain.alert import EventName
from salvager.domain.phase2_audit import SmokeTestRecord
from salvager.domain.reconciliation import (
    ReconciliationResult,
    compute_tolerance,
)
from salvager.interfaces.phase2_state_reader import Phase2StateReader
from salvager.observability.logging import get_logger
from salvager.orchestration.degradation_reporter import Reporter

#: Reason persisted in ``phase2_state.disabled_reason`` when the smoke
#: test trips the global lockout.
SMOKE_TEST_FAILED_REASON: Final[str] = "smoke_test_failed"

#: Canonical smoke-test fixtures, shipped as package data under
#: ``src/salvager/smoke_fixtures/`` so they travel with the runtime image
#: (``COPY src/``) and a built wheel — resolved relative to the package, NOT
#: the current working directory. ``__file__`` is
#: ``src/salvager/orchestration/smoke_test.py`` → ``parents[1]`` is the
#: ``salvager`` package root. Single source of truth for the CLI default and
#: the daemon-scheduled smoke job.
DEFAULT_SMOKE_FIXTURES_DIR: Final[Path] = (
    Path(__file__).resolve().parents[1] / "smoke_fixtures" / "price_parsers" / "active"
)

#: Suffix that pairs a fixture file with its independently-verified price.
EXPECTED_SUFFIX: Final[str] = ".expected.json"

PriceParser = Callable[[bytes], Decimal]


@dataclass(frozen=True)
class SmokeTestFixture:
    """One smoke-test case loaded from disk."""

    name: str
    kind: str
    response_path: Path
    expected_price_eur: Decimal


@dataclass(frozen=True)
class FixtureOutcome:
    """Per-fixture result inside one smoke-test run."""

    fixture: SmokeTestFixture
    parsed_price_eur: Decimal | None  # None when the parser raised
    parser_error_class: str | None  # set when the parser raised
    result: ReconciliationResult | None  # None when the parser raised
    passed: bool


@dataclass(frozen=True)
class SmokeTestSummary:
    """Aggregate of one smoke-test run."""

    outcomes: list[FixtureOutcome]
    any_failed: bool
    fired_lockout: bool
    fired_recovery: bool


def discover_fixtures(fixtures_dir: Path) -> list[SmokeTestFixture]:
    """Walk ``fixtures_dir`` and return its fixture pairs.

    Pairs every non-``.expected.json`` file with its sibling
    ``<name>.expected.json``. A missing sibling raises — a misnamed or
    half-installed fixture set is a programming error, not a soft fail.
    """
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(f"smoke-test fixtures dir not found: {fixtures_dir}")

    fixtures: list[SmokeTestFixture] = []
    for response_path in sorted(fixtures_dir.iterdir()):
        if response_path.is_dir():
            continue
        if response_path.name.endswith(EXPECTED_SUFFIX):
            continue
        expected_path = response_path.with_name(response_path.stem + EXPECTED_SUFFIX)
        if not expected_path.exists():
            raise FileNotFoundError(
                f"fixture {response_path.name!r} has no sibling {expected_path.name!r}"
            )
        meta = json.loads(expected_path.read_text(encoding="utf-8"))
        fixtures.append(
            SmokeTestFixture(
                name=response_path.stem,
                kind=str(meta["kind"]),
                response_path=response_path,
                expected_price_eur=Decimal(str(meta["price_eur"])),
            )
        )
    return fixtures


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def run_smoke_test(
    *,
    fixtures: list[SmokeTestFixture],
    parsers: Mapping[str, PriceParser],
    audit_writer: Phase2AuditWriter,
    state_reader: Phase2StateReader,
    reporter: Reporter,
    tolerance_eur: Decimal,
    tolerance_pct: Decimal,
    clock: Callable[[], datetime] = _utc_now,
) -> SmokeTestSummary:
    """Run the smoke test against every fixture and route the outcome."""
    log = get_logger("orchestration.smoke_test")
    now = clock()
    log.info(
        "smoke_test_started",
        extra={"fixture_count": len(fixtures), "run_at": now.isoformat()},
    )

    previous_state = await state_reader.read()
    previously_failed = previous_state.last_smoke_result == "fail"

    outcomes: list[FixtureOutcome] = []
    for fixture in fixtures:
        outcomes.append(
            await _run_one_fixture(
                fixture=fixture,
                parsers=parsers,
                audit_writer=audit_writer,
                tolerance_eur=tolerance_eur,
                tolerance_pct=tolerance_pct,
                now=now,
                log=log,
            )
        )

    any_failed = any(not o.passed for o in outcomes)
    fired_lockout = False
    fired_recovery = False

    if any_failed:
        await audit_writer.set_global_disable(SMOKE_TEST_FAILED_REASON)
        first_failure = next(o for o in outcomes if not o.passed)
        await reporter.report(
            "warn",
            EventName.smoke_test_failed,
            ctx={
                "fixture_name": first_failure.fixture.name,
                "parsed_price": (
                    str(first_failure.parsed_price_eur)
                    if first_failure.parsed_price_eur is not None
                    else "—"
                ),
                "expected_price": str(first_failure.fixture.expected_price_eur),
                "delta_eur": (
                    str(first_failure.result.delta_eur) if first_failure.result is not None else "—"
                ),
                "parser_error_class": first_failure.parser_error_class or "—",
            },
        )
        fired_lockout = True
    elif previously_failed:
        await reporter.report("info", EventName.smoke_test_recovered, ctx={})
        fired_recovery = True

    return SmokeTestSummary(
        outcomes=outcomes,
        any_failed=any_failed,
        fired_lockout=fired_lockout,
        fired_recovery=fired_recovery,
    )


async def _run_one_fixture(
    *,
    fixture: SmokeTestFixture,
    parsers: Mapping[str, PriceParser],
    audit_writer: Phase2AuditWriter,
    tolerance_eur: Decimal,
    tolerance_pct: Decimal,
    now: datetime,
    log: object,
) -> FixtureOutcome:
    parser = parsers.get(fixture.kind)
    response_bytes = fixture.response_path.read_bytes()
    parsed_price: Decimal | None = None
    parser_error: str | None = None
    result: ReconciliationResult | None = None
    passed = False

    if parser is None:
        parser_error = "ParserNotRegistered"
        log.error(  # type: ignore[attr-defined]
            "smoke_test_parser_missing",
            extra={"fixture_name": fixture.name, "kind": fixture.kind},
        )
    else:
        try:
            parsed_price = parser(response_bytes)
            result = compute_tolerance(
                fixture.expected_price_eur,
                parsed_price,
                tolerance_eur=tolerance_eur,
                tolerance_pct=tolerance_pct,
            )
            passed = result.passed
        except Exception as exc:
            parser_error = exc.__class__.__name__
            log.error(  # type: ignore[attr-defined]
                "smoke_test_parser_raised",
                extra={
                    "fixture_name": fixture.name,
                    "kind": fixture.kind,
                    "error_class": parser_error,
                },
            )

    # The audit row carries something even on parser failure so the
    # operator can see what happened in `audit show`.
    record = SmokeTestRecord(
        run_at=now,
        result="pass" if passed else "fail",
        parsed_price=parsed_price if parsed_price is not None else Decimal("0"),
        independent_price=fixture.expected_price_eur,
        delta_eur=(result.delta_eur if result is not None else fixture.expected_price_eur),
        delta_pct=result.delta_pct if result is not None else Decimal("0"),
    )
    await audit_writer.record_smoke_test(record)

    return FixtureOutcome(
        fixture=fixture,
        parsed_price_eur=parsed_price,
        parser_error_class=parser_error,
        result=result,
        passed=passed,
    )


__all__ = [
    "DEFAULT_SMOKE_FIXTURES_DIR",
    "EXPECTED_SUFFIX",
    "SMOKE_TEST_FAILED_REASON",
    "FixtureOutcome",
    "PriceParser",
    "SmokeTestFixture",
    "SmokeTestSummary",
    "discover_fixtures",
    "run_smoke_test",
]
