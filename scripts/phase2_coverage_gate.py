#!/usr/bin/env python3
"""Phase 2 critical-path coverage gate — Story 5.15 (NFR-M2).

Reads ``coverage.json`` (produced by ``pytest --cov-report=json``) and
asserts every Phase 2 critical-path module hits the locked line-coverage
threshold. Per-module enforcement matters: an overall average can hide a
poorly-tested buy path behind a thoroughly-tested adapter, and the buy
path is exactly where v1.0 cannot afford coverage drift.

Modules under the gate (Story 5.15 AC):

  - ``salvager.orchestration.buy_orchestrator``  — Story 5.7
  - ``salvager.orchestration.reconciler``        — Story 5.4
  - ``salvager.orchestration.circuit_breaker``   — Story 5.5
  - ``salvager.orchestration.smoke_test``        — Story 5.6
  - ``salvager.adapters.sqlite_store.audit_writer`` — Story 5.1

Modules that haven't landed yet (e.g. buy_orchestrator before Story 5.7)
are reported as ``PENDING`` and do not fail the gate. The gate trips
the moment a critical module exists but its coverage dips below the
threshold.

Zero external deps. Exit: 0 clean, 1 violations, 2 invocation error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final, TextIO

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The locked per-module floor. NFR-M2; only a PRD amendment changes it.
DEFAULT_THRESHOLD_PCT: Final[float] = 90.0

#: Critical-path modules as repo-relative file paths (the form coverage.json
#: uses as its ``files`` keys). Listed in AC order.
CRITICAL_MODULES: Final[tuple[str, ...]] = (
    "src/salvager/orchestration/buy_orchestrator.py",
    "src/salvager/orchestration/reconciler.py",
    "src/salvager/orchestration/circuit_breaker.py",
    "src/salvager/orchestration/smoke_test.py",
    "src/salvager/adapters/sqlite_store/audit_writer.py",
)


def _load_coverage(report_path: Path) -> dict[str, object]:
    # ``report_path`` comes from a CLI arg (--report). Canonicalise it and
    # confine it to the repo tree before reading, so a crafted argument can't
    # pull a file from elsewhere on disk (Sonar pythonsecurity:S8707). REPO_ROOT
    # derives from __file__, not from user input, so it is a trusted base.
    resolved = report_path.resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        raise SystemExit(
            f"ERROR: coverage report path {resolved} is outside the repo root {REPO_ROOT}."
        )
    if not resolved.is_file():
        raise SystemExit(
            f"ERROR: coverage report not found at {report_path}. "
            "Run pytest with --cov-report=json first."
        )
    return json.loads(resolved.read_text(encoding="utf-8"))


def _module_coverage_pct(file_entry: dict[str, object]) -> float | None:
    """Pull the line-coverage percentage out of one ``files[<path>]`` entry."""
    summary = file_entry.get("summary")
    if not isinstance(summary, dict):
        return None
    pct = summary.get("percent_covered")
    if pct is None:
        return None
    return float(pct)


def check_thresholds(
    coverage_data: dict[str, object],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    modules: tuple[str, ...] = CRITICAL_MODULES,
) -> tuple[list[tuple[str, float]], list[str], list[tuple[str, float]]]:
    """Walk the report and bucket each critical module into pass / pending / fail.

    Returns ``(passing, pending, failing)`` lists. ``pending`` entries
    are modules that don't exist in the report yet (e.g. before the
    landing story) — they DO NOT fail the gate but are surfaced so the
    CI log makes the gap visible.
    """
    files = coverage_data.get("files", {})
    if not isinstance(files, dict):
        raise SystemExit("ERROR: coverage report has no 'files' object.")

    passing: list[tuple[str, float]] = []
    pending: list[str] = []
    failing: list[tuple[str, float]] = []

    for module_path in modules:
        entry = files.get(module_path)
        if not isinstance(entry, dict):
            pending.append(module_path)
            continue
        pct = _module_coverage_pct(entry)
        if pct is None:
            pending.append(module_path)
            continue
        if pct >= threshold_pct:
            passing.append((module_path, pct))
        else:
            failing.append((module_path, pct))
    return passing, pending, failing


def render_report(
    passing: list[tuple[str, float]],
    pending: list[str],
    failing: list[tuple[str, float]],
    *,
    threshold_pct: float,
    stream: TextIO = sys.stdout,
) -> None:
    print(f"Phase 2 critical-path coverage gate (threshold: {threshold_pct:.0f}%)", file=stream)
    for module, pct in passing:
        print(f"  PASS  {module}  {pct:6.2f}%", file=stream)
    for module in pending:
        print(f"  PEND  {module}  (not yet in coverage report)", file=stream)
    for module, pct in failing:
        print(f"  FAIL  {module}  {pct:6.2f}%  (below {threshold_pct:.0f}%)", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "coverage.json",
        help="Path to coverage.json (default: ./coverage.json).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_PCT,
        help=f"Minimum per-module line coverage percentage (default: {DEFAULT_THRESHOLD_PCT}).",
    )
    args = parser.parse_args(argv)

    try:
        coverage_data = _load_coverage(args.report)
        passing, pending, failing = check_thresholds(coverage_data, threshold_pct=args.threshold)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: coverage gate aborted: {exc}", file=sys.stderr)
        return 2

    render_report(passing, pending, failing, threshold_pct=args.threshold, stream=sys.stdout)
    if failing:
        print(
            f"\nFAIL: {len(failing)} critical-path module(s) below {args.threshold:.0f}% (NFR-M2).",
            file=sys.stderr,
        )
        return 1
    print("\nOK Phase 2 coverage gate passed (NFR-M2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
