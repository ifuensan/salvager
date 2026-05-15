"""Tests for ``scripts/payment_rail_lint.py`` — FR25 / NFR-S5 enforcement.

Verifies the lint:

  - passes on the current tree (the ``tinyfish_browser`` adapter does
    not exist yet — nothing to lint is not a failure);
  - fails on a synthetic ``bizum_pay.py`` file (by filename AND content);
  - catches denied terms in comments and imports;
  - honours the explicit ``verified by payment_rail_lint`` escape hatch;
  - leaves a genuinely clean flow file alone.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT_SCRIPT = REPO_ROOT / "scripts" / "payment_rail_lint.py"


def _load_lint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("payment_rail_lint", LINT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["payment_rail_lint"] = module
    spec.loader.exec_module(module)
    return module


_lint = _load_lint()


def test_passes_on_current_tree() -> None:
    # tinyfish_browser/ is introduced by Story 5.3; until then the lint
    # has nothing to scan and must exit clean.
    assert _lint.main() == 0


def test_missing_root_is_not_a_violation(tmp_path: Path) -> None:
    assert _lint.find_violations(tmp_path / "does_not_exist") == []


def test_synthetic_bizum_file_fails(tmp_path: Path) -> None:
    (tmp_path / "bizum_pay.py").write_text(
        "async def pay_with_bizum() -> None:\n    ...\n",
        encoding="utf-8",
    )
    violations = _lint.find_violations(tmp_path)
    assert violations, "a bizum_pay.py file must be flagged"
    # Flagged both on its filename and on the function name.
    reasons = " ".join(reason for _, _, reason in violations)
    assert "filename" in reasons
    assert _lint.main(tmp_path) == 1


def test_filename_alone_triggers(tmp_path: Path) -> None:
    (tmp_path / "paypal_checkout.py").write_text("X = 1\n", encoding="utf-8")
    violations = _lint.find_violations(tmp_path)
    assert any(line == 0 and "filename" in reason for _, line, reason in violations)


def test_denied_term_in_comment_fails(tmp_path: Path) -> None:
    (tmp_path / "wallapop_pay.py").write_text(
        "# fall back to transferencia if the card is declined\nVALUE = 1\n",
        encoding="utf-8",
    )
    violations = _lint.find_violations(tmp_path)
    assert any("transferencia" in reason for _, _, reason in violations)


def test_denied_import_is_caught(tmp_path: Path) -> None:
    (tmp_path / "wallapop_pay.py").write_text("import revolut_sdk\n", encoding="utf-8")
    violations = _lint.find_violations(tmp_path)
    assert any("revolut" in reason for _, _, reason in violations)


def test_marker_escape_hatch_is_tolerated(tmp_path: Path) -> None:
    (tmp_path / "wallapop_pay.py").write_text(
        "# NOT a Bizum flow — verified by payment_rail_lint\n"
        "async def pay_with_wallapop_pay() -> None:\n    ...\n",
        encoding="utf-8",
    )
    assert _lint.find_violations(tmp_path) == []


def test_marker_only_exempts_its_own_line(tmp_path: Path) -> None:
    """The escape hatch is per-line — a bare mention elsewhere still fails."""
    (tmp_path / "wallapop_pay.py").write_text(
        "# NOT a Bizum flow — verified by payment_rail_lint\n# but here we actually call paypal\n",
        encoding="utf-8",
    )
    violations = _lint.find_violations(tmp_path)
    assert any("paypal" in reason for _, _, reason in violations)
    assert not any("bizum" in reason.lower() for _, _, reason in violations)


def test_clean_flow_file_passes(tmp_path: Path) -> None:
    (tmp_path / "wallapop_pay.py").write_text(
        '"""Wallapop Pay checkout flow."""\nasync def execute_buy() -> None:\n    ...\n',
        encoding="utf-8",
    )
    assert _lint.find_violations(tmp_path) == []
