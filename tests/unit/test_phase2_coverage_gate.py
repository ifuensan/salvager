"""Tests for ``scripts/phase2_coverage_gate.py`` — Story 5.15.

The gate is exercised against synthetic ``coverage.json`` payloads so we
don't depend on the real coverage report (which floats with the suite)
and we can pin every branch behaviour deterministically.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO_ROOT / "scripts" / "phase2_coverage_gate.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase2_coverage_gate", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase2_coverage_gate"] = module
    spec.loader.exec_module(module)
    return module


_gate = _load_gate()


@pytest.fixture(autouse=True)
def _confine_repo_root_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the gate's repo-root confinement (Sonar S8707) at the test's tmp
    dir so synthetic ``coverage.json`` reports written under ``tmp_path`` pass
    the ``--report`` path validation."""
    # Resolve: the gate resolves the --report path before the containment
    # check, and on some platforms the temp dir sits behind a symlink
    # (e.g. macOS /var → /private/var).
    monkeypatch.setattr(_gate, "REPO_ROOT", tmp_path.resolve())


def _coverage(pct_by_module: dict[str, float]) -> dict[str, object]:
    return {
        "files": {
            module: {"summary": {"percent_covered": pct}} for module, pct in pct_by_module.items()
        }
    }


# ─────────────────────────────────────────────────────────────────────────
# Critical-module list itself
# ─────────────────────────────────────────────────────────────────────────


def test_critical_modules_match_the_ac() -> None:
    """The locked list mirrors Story 5.15's named modules verbatim."""
    expected = {
        "src/salvager/orchestration/buy_orchestrator.py",
        "src/salvager/orchestration/reconciler.py",
        "src/salvager/orchestration/circuit_breaker.py",
        "src/salvager/orchestration/smoke_test.py",
        "src/salvager/adapters/sqlite_store/audit_writer.py",
    }
    assert set(_gate.CRITICAL_MODULES) == expected


# ─────────────────────────────────────────────────────────────────────────
# check_thresholds — bucketing
# ─────────────────────────────────────────────────────────────────────────


def test_every_module_above_threshold_passes() -> None:
    data = _coverage(dict.fromkeys(_gate.CRITICAL_MODULES, 99.0))
    passing, pending, failing = _gate.check_thresholds(data)
    assert {m for m, _ in passing} == set(_gate.CRITICAL_MODULES)
    assert pending == []
    assert failing == []


def test_module_missing_from_report_is_pending() -> None:
    """A module the report doesn't carry (e.g. before its landing story)
    is surfaced as PEND but does not fail the gate."""
    data = _coverage(
        {
            m: 95.0
            for m in _gate.CRITICAL_MODULES
            if m != "src/salvager/orchestration/buy_orchestrator.py"
        }
    )
    passing, pending, failing = _gate.check_thresholds(data)
    assert pending == ["src/salvager/orchestration/buy_orchestrator.py"]
    assert failing == []
    assert len(passing) == len(_gate.CRITICAL_MODULES) - 1


def test_below_threshold_module_fails() -> None:
    data = _coverage(
        {
            "src/salvager/orchestration/reconciler.py": 89.99,
            "src/salvager/orchestration/circuit_breaker.py": 95.0,
            "src/salvager/orchestration/smoke_test.py": 95.0,
            "src/salvager/adapters/sqlite_store/audit_writer.py": 95.0,
        }
    )
    _passing, _pending, failing = _gate.check_thresholds(data)
    assert failing == [("src/salvager/orchestration/reconciler.py", 89.99)]


def test_exactly_ninety_passes() -> None:
    data = _coverage({_gate.CRITICAL_MODULES[1]: 90.0})
    passing, _pending, failing = _gate.check_thresholds(data)
    assert (_gate.CRITICAL_MODULES[1], 90.0) in passing
    assert failing == []


def test_custom_threshold_is_honoured() -> None:
    data = _coverage({_gate.CRITICAL_MODULES[1]: 92.0})
    _passing, _pending, failing = _gate.check_thresholds(data, threshold_pct=95.0)
    assert failing == [(_gate.CRITICAL_MODULES[1], 92.0)]


# ─────────────────────────────────────────────────────────────────────────
# main — end-to-end exit codes
# ─────────────────────────────────────────────────────────────────────────


def _write_report(tmp_path: Path, pct_by_module: dict[str, float]) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_coverage(pct_by_module)), encoding="utf-8")
    return path


def test_main_passes_when_every_present_module_is_above_threshold(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _write_report(
        tmp_path,
        {
            "src/salvager/orchestration/reconciler.py": 100.0,
            "src/salvager/orchestration/circuit_breaker.py": 94.5,
            "src/salvager/orchestration/smoke_test.py": 98.0,
            "src/salvager/adapters/sqlite_store/audit_writer.py": 100.0,
        },
    )
    rc = _gate.main(["--report", str(report)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK Phase 2 coverage gate passed" in out
    assert "PEND" in out  # buy_orchestrator missing


def test_main_fails_with_named_module_when_below_threshold(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _write_report(
        tmp_path,
        {
            "src/salvager/orchestration/reconciler.py": 87.5,
            "src/salvager/orchestration/circuit_breaker.py": 95.0,
            "src/salvager/orchestration/smoke_test.py": 95.0,
            "src/salvager/adapters/sqlite_store/audit_writer.py": 95.0,
        },
    )
    rc = _gate.main(["--report", str(report)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL  src/salvager/orchestration/reconciler.py" in captured.out
    assert " 87.50%" in captured.out
    assert "NFR-M2" in captured.err


def test_main_missing_report_exits_with_clear_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _gate.main(["--report", str(tmp_path / "does_not_exist.json")])
    assert "coverage report not found" in str(exc_info.value)
