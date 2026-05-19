#!/usr/bin/env python3
"""Adapter Discipline Lint — NFR-M1 launch-blocker mechanism.

Walks ``src/salvager/``; fails on any deny-listed import outside
``adapters/``. Only ``adapters/`` may import marketplace SDKs / Hermes /
TinyFish / LLM SDKs / python-telegram-bot / httpx. Zero external dep.
Exit: 0 clean, 1 violations, 2 invocation error.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "salvager"
ADAPTERS_ROOT = SRC_ROOT / "adapters"

# Modules forbidden outside ``src/salvager/adapters/``.
# Suffix ``*`` means "any module starting with this prefix".
DENY_LIST: tuple[str, ...] = (
    "hermes_agent",  # NFR-I1: Hermes via adapter only
    "tinyfish",  # NFR-I2: TinyFish SDK only inside adapters/wallapop_tinyfish/
    "google.genai",  # NFR-I3: LLM via ListingEvaluator interface
    "openai",  # alternate LLM via adapter only
    "anthropic",  # alternate LLM via adapter only
    "telegram",  # python-telegram-bot via adapter only
    "httpx",  # AR23 + adapter discipline (external HTTP via adapters only)
    "playwright",  # browser sessions via TinyFish browser adapter only
    "curl_cffi",  # TLS-impersonating HTTP only inside adapters/wallapop_api/
)


def _is_denied(module: str) -> bool:
    """Return True if ``module`` matches the deny-list."""
    for entry in DENY_LIST:
        prefix = entry[:-1] if entry.endswith("*") else entry
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def _check_file(path: Path) -> list[tuple[int, str]]:
    """Return ``[(line, import_str), ...]`` for denied imports in ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [(0, f"<parse error: {exc.__class__.__name__}>")]

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_denied(alias.name):
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_denied(module):
                names = ", ".join(a.name for a in node.names)
                hits.append((node.lineno, f"from {module} import {names}"))
    return hits


def main() -> int:
    if not SRC_ROOT.is_dir():
        print(f"ERROR: source root not found at {SRC_ROOT}", file=sys.stderr)
        return 2

    failures: list[tuple[Path, int, str]] = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        try:
            py_file.relative_to(ADAPTERS_ROOT)
            continue  # adapters/** is allowed to import the deny-list
        except ValueError:
            pass
        for line_no, import_str in _check_file(py_file):
            failures.append((py_file, line_no, import_str))

    if not failures:
        print("OK adapter discipline lint passed (NFR-M1)")
        return 0

    print("FAIL adapter discipline lint (NFR-M1 violation)\n", file=sys.stderr)
    for path, line_no, import_str in failures:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{line_no}: {import_str}", file=sys.stderr)
    print(
        f"\n{len(failures)} violation(s). These imports are allowed only inside "
        f"src/salvager/adapters/.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
