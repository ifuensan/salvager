#!/usr/bin/env python3
"""Payment-Rail Enforcement Lint — FR25 / NFR-S5 launch-blocker.

Walks ``src/hardware_hunter/adapters/tinyfish_browser/`` and fails the
build if any file *name* or file *content* references a payment rail
other than Wallapop Pay / eBay.es checkout. The agent must have no
codepath — not even a dormant one — to use Bizum, transferencia
bancaria, PayPal, Revolut, a bank transfer, or the operator's own card.

Detection is two-pronged (per the Story 5.14 AC):

  - **AST**: denied module imports + class / function definition names.
  - **String match**: a case-insensitive per-line scan, which also
    catches comments, string literals, and the filename itself.

Escape hatch: an allowed flow (``wallapop_pay.py`` / ``ebay_checkout.py``)
may mention a denied term ONLY on a line that also carries the explicit
marker ``verified by payment_rail_lint`` — e.g.

    # NOT a Bizum flow — verified by payment_rail_lint

Without that marker, any occurrence fails the build. Zero external dep.
Exit: 0 clean, 1 violations, 2 invocation error.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LINT_ROOT = REPO_ROOT / "src" / "hardware_hunter" / "adapters" / "tinyfish_browser"

#: Payment rails the agent must never reach. Configurable — extend here.
DENY_TERMS: tuple[str, ...] = (
    "bizum",
    "transferencia",
    "paypal",
    "revolut",
    "bank_transfer",
    "tarjeta_propia",
)

#: A line carrying this marker is exempt — the explicit "this is NOT a
#: <rail> flow" annotation an allowed flow uses to name the rail it avoids.
ALLOW_MARKER = "verified by payment_rail_lint"


def _denied_terms_in(text: str) -> list[str]:
    """Return every deny-list term that appears (case-insensitive) in ``text``."""
    lowered = text.lower()
    return [term for term in DENY_TERMS if term in lowered]


def _ast_hits(path: Path, source: str) -> list[tuple[int, str]]:
    """AST pass: denied imports + class / function definition names."""
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [(0, f"<parse error: {exc.__class__.__name__}>")]

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for term in _denied_terms_in(alias.name):
                    hits.append((node.lineno, f"denied import {alias.name!r} ({term})"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for term in _denied_terms_in(module):
                hits.append((node.lineno, f"denied import from {module!r} ({term})"))
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            for term in _denied_terms_in(node.name):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                hits.append((node.lineno, f"denied {kind} name {node.name!r} ({term})"))
    return hits


def _string_hits(lines: list[str]) -> list[tuple[int, str]]:
    """String pass: per-line case-insensitive scan of comments / literals / code."""
    hits: list[tuple[int, str]] = []
    for line_no, line in enumerate(lines, start=1):
        for term in _denied_terms_in(line):
            hits.append((line_no, f"{term!r} referenced in source"))
    return hits


def find_violations(root: Path = DEFAULT_LINT_ROOT) -> list[tuple[Path, int, str]]:
    """Return ``[(path, line, reason), ...]`` for every payment-rail violation.

    A missing ``root`` is not an error — the ``tinyfish_browser`` adapter
    is introduced by Story 5.3; until then there is simply nothing to lint.
    """
    if not root.is_dir():
        return []

    violations: list[tuple[Path, int, str]] = []
    for py_file in sorted(root.rglob("*.py")):
        # Filename check — a `bizum_pay.py` file fails on its name alone.
        for term in _denied_terms_in(py_file.name):
            violations.append((py_file, 0, f"filename references denied rail ({term})"))

        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()

        raw_hits = _ast_hits(py_file, source) + _string_hits(lines)
        # Dedupe by (line, reason); honour the per-line escape-hatch marker.
        seen: set[tuple[int, str]] = set()
        for line_no, reason in raw_hits:
            if (line_no, reason) in seen:
                continue
            seen.add((line_no, reason))
            if 1 <= line_no <= len(lines) and ALLOW_MARKER in lines[line_no - 1].lower():
                continue  # explicitly annotated as a non-flow mention
            violations.append((py_file, line_no, reason))
    return violations


def main(root: Path = DEFAULT_LINT_ROOT) -> int:
    violations = find_violations(root)
    if not violations:
        print("OK payment-rail lint passed (FR25 / NFR-S5)")
        return 0

    print("FAIL payment-rail lint (FR25 / NFR-S5 violation)\n", file=sys.stderr)
    for path, line_no, reason in violations:
        try:
            rel: Path | str = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        location = f"{rel}:{line_no}" if line_no else f"{rel}"
        print(f"  {location}: {reason}", file=sys.stderr)
    print(
        f"\n{len(violations)} violation(s). Only Wallapop Pay / eBay.es checkout "
        f"are permitted payment rails. Annotate a deliberate non-flow mention with "
        f"'{ALLOW_MARKER}'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
