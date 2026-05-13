"""CLI exit-code map mechanical check — Story 3.15 (FR48).

FR48 locks the CLI exit-code set at ``{0, 1, 2, 3, 4, 5}``:

  - 0 → success
  - 1 → generic failure (precondition not met)
  - 2 → invalid CLI arguments
  - 3 → configuration / schema / scope error
  - 4 → authentication error
  - 5 → unrecoverable runtime error (Phase 2 fail-closed, etc.)

This test AST-walks ``src/hardware_hunter/cli/`` and refuses any
literal ``typer.Exit(code=N)`` or ``return N`` (inside a callable
named ``run`` — the convention for subcommand entrypoints) with
``N`` outside that set. The walker is intentionally conservative:
it only flags integer-literal exit codes, since dynamic ``exit_code``
values are resolved from the same constrained set at runtime and
the parametrized test below exercises those paths end-to-end.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hardware_hunter.cli.app import app

VALID_EXIT_CODES: frozenset[int] = frozenset({0, 1, 2, 3, 4, 5})

_CLI_ROOT = Path(__file__).resolve().parents[2] / "src" / "hardware_hunter" / "cli"


def _cli_python_files() -> list[Path]:
    return sorted(_CLI_ROOT.rglob("*.py"))


# ─────────────────────────────────────────────────────────────────────────
# Static AST walk: every literal exit code is in the set
# ─────────────────────────────────────────────────────────────────────────


def _literal_exit_codes(tree: ast.AST) -> list[tuple[int, int]]:
    """Return ``[(line_no, code), ...]`` for every literal exit code found.

    Picks up two shapes:
      * ``typer.Exit(code=N)``  — the typer-native interrupt
      * ``return N`` inside a function named ``run``  — the
        subcommand entry-point convention (see ``cli/commands/*.py``)
    """
    hits: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        # typer.Exit(code=N) and typer.Exit(N) forms.
        if isinstance(node, ast.Call):
            func = node.func
            is_typer_exit = (
                isinstance(func, ast.Attribute)
                and func.attr == "Exit"
                and isinstance(func.value, ast.Name)
                and func.value.id == "typer"
            )
            if is_typer_exit:
                for kw in node.keywords:
                    if (
                        kw.arg == "code"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, int)
                    ):
                        hits.append((node.lineno, kw.value.value))
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                        hits.append((node.lineno, arg.value))
        # `return N` inside `def run(...)`
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Return)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, int)
                ):
                    hits.append((stmt.lineno, stmt.value.value))
    return hits


@pytest.mark.parametrize(
    "path",
    _cli_python_files(),
    ids=lambda p: str(p.relative_to(_CLI_ROOT)),
)
def test_cli_module_uses_only_valid_exit_codes(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for line_no, code in _literal_exit_codes(tree):
        assert code in VALID_EXIT_CODES, (
            f"{path.relative_to(_CLI_ROOT)}:{line_no} uses exit code {code} "
            f"outside the FR48 set {sorted(VALID_EXIT_CODES)}"
        )


# ─────────────────────────────────────────────────────────────────────────
# End-to-end: every registered typer command exits with one of the codes
# ─────────────────────────────────────────────────────────────────────────


def _registered_command_paths() -> list[list[str]]:
    """Return every command path registered on the top-level app.

    The structure is one or two segments (``["version"]``,
    ``["login", "wallapop"]``); deeper nesting isn't used at v0.x.
    """
    paths: list[list[str]] = []
    for cmd in app.registered_commands:
        if cmd.name:
            paths.append([cmd.name])
    for group in app.registered_groups:
        sub = group.typer_instance
        if sub is None or group.name is None:
            continue
        for cmd in sub.registered_commands:
            if cmd.name:
                paths.append([group.name, cmd.name])
    return paths


_COMMAND_PATHS = _registered_command_paths()


@pytest.mark.parametrize(
    "command_path",
    _COMMAND_PATHS,
    ids=[" ".join(p) for p in _COMMAND_PATHS],
)
def test_command_exit_codes_stay_within_fr48_set(command_path: list[str]) -> None:
    """Invoke the subcommand with no extra arguments and assert the exit
    code is in the FR48 set.

    Most v0.x subcommands are placeholders that return 1, the implemented
    ones return 0/2/3 depending on default-path resolution. Any future
    subcommand introducing a 6/7/... gets caught here, not in production.
    """
    runner = CliRunner()
    result = runner.invoke(app, [*command_path], catch_exceptions=False)
    assert result.exit_code in VALID_EXIT_CODES, (
        f"`{' '.join(command_path)}` exited with {result.exit_code}; "
        f"FR48 only allows {sorted(VALID_EXIT_CODES)}.\n"
        f"stdout:\n{result.stdout}"
    )
