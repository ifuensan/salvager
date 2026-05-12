"""Typer CLI skeleton — Story 1.8 (FR39).

This module wires the ``hardware-hunter`` console script: a root typer app
that mounts placeholder subcommands and groups for every CLI surface named
in the PRD. Each placeholder exits with code 1 + a ``not yet implemented``
message; later stories swap real implementations in without touching the
mount points.

Exit codes (FR48): ``0`` success, ``1`` generic error, ``2`` usage,
``3`` validation failure, ``4`` transient/retry-friendly, ``5`` fatal infra.
typer's default for unknown subcommands is ``2``, which matches FR48.

# TODO(Epic 5 — exit-code CI gate): as subcommands land, enforce the
# locked exit-code set {0,1,2,3,4,5} via a CI lint that greps for any
# `raise typer.Exit(<n>)` with n outside the set.
"""

from __future__ import annotations

import json
import os
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer

from hardware_hunter.observability.logging import get_logger
from hardware_hunter.observability.styling import render_prose

app = typer.Typer(
    name="hardware-hunter",
    help=(
        "Self-hosted personal agent for monitoring second-hand homelab parts "
        "on Wallapop and eBay.es. Watches your wishlist, sends Telegram "
        "alerts, and (Phase 2) executes purchases behind a non-bypassable tap."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

# Subcommand groups — empty shells that later stories fill in.
login_app = typer.Typer(
    help="Authenticate with a marketplace (Wallapop cookie capture, eBay OAuth).",
)
phase2_app = typer.Typer(help="Control Phase 2 autonomous-purchase enablement per entry.")
audit_app = typer.Typer(help="Inspect the local append-only audit log.")
wishlist_app = typer.Typer(help="Read-only inspection of the loaded wishlist.")

app.add_typer(login_app, name="login")
app.add_typer(phase2_app, name="phase2")
app.add_typer(audit_app, name="audit")
app.add_typer(wishlist_app, name="wishlist")


# ─────────────────────────────────────────────────────────────────────────
# Root callback — bare `hardware-hunter` runs the daemon (FR39)
# ─────────────────────────────────────────────────────────────────────────


@app.callback()
def _root(ctx: typer.Context) -> None:
    """Dispatch to the daemon stub when invoked without a subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    _run_daemon_stub()


def _run_daemon_stub() -> None:
    """Epic 1 placeholder for the daemon entrypoint.

    The real poll loop lands in Epic 3. At v0.1 we emit the lifecycle
    events through the structured logger so an operator running the
    Docker image sees the container start cleanly and exit cleanly —
    which is enough to verify the image, the logging substrate, and the
    console-script wiring all work end-to-end.
    """
    log = get_logger("daemon")
    log.info("daemon_started", extra={"phase": "stub", "version": _resolve_version()})
    log.info("daemon_stopped", extra={"reason": "stub_no_poll_loop"})


# ─────────────────────────────────────────────────────────────────────────
# `version` subcommand — fully implemented at v0.1
# ─────────────────────────────────────────────────────────────────────────


@app.command("version")
def cmd_version(
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
) -> None:
    """Print the package version and the git commit short SHA."""
    ver = _resolve_version()
    commit = _resolve_commit()

    if output_format == "json":
        typer.echo(json.dumps({"version": ver, "commit": commit}))
        return
    if output_format != "human":
        render_prose(
            f"unknown --format value: {output_format!r}",
            style="error",
            hint="use --format human or --format json",
        )
        raise typer.Exit(code=2)

    render_prose(f"hardware-hunter {ver} ({commit})", style="info")


def _resolve_version() -> str:
    try:
        return version("hardware-hunter")
    except PackageNotFoundError:
        return "unknown"


def _resolve_commit() -> str:
    """Resolve the git commit short SHA.

    Order: ``HARDWARE_HUNTER_COMMIT`` env var (set by the Dockerfile at
    build time, lands in a follow-up) → ``git rev-parse`` when a working
    tree is present → ``unknown``.
    """
    env_commit = os.environ.get("HARDWARE_HUNTER_COMMIT")
    if env_commit:
        return env_commit
    repo_root = Path(__file__).resolve().parents[3]
    if not (repo_root / ".git").exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


# ─────────────────────────────────────────────────────────────────────────
# Placeholder commands — every later story swaps its real impl here
# ─────────────────────────────────────────────────────────────────────────


_NOT_YET = "not yet implemented in this build"
_ROADMAP_HINT = "see ROADMAP.md"


def _placeholder() -> None:
    render_prose(_NOT_YET, style="error", hint=_ROADMAP_HINT)
    raise typer.Exit(code=1)


@app.command("init")
def cmd_init() -> None:
    """Scaffold config files (Epic 2 Story 2.8)."""
    _placeholder()


_DEFAULT_WISHLIST_PATH = Path("config") / "wishlist.yaml"


@app.command("validate-wishlist")
def cmd_validate_wishlist(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="Path to wishlist.yaml (default: ./config/wishlist.yaml).",
        ),
    ] = _DEFAULT_WISHLIST_PATH,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
) -> None:
    """Validate ``wishlist.yaml`` against the schema + (c3) scope contract."""
    from hardware_hunter.cli.commands.validate_wishlist import run

    exit_code = run(path, output_format)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


_DEFAULT_CONFIG_PATH = Path("config") / "config.yaml"
_DEFAULT_ENV_PATH = Path("config") / ".env"


@app.command("validate-config")
def cmd_validate_config(
    config_path: Annotated[
        Path,
        typer.Option(
            "--config-path",
            "-c",
            help="Path to config.yaml (default: ./config/config.yaml).",
        ),
    ] = _DEFAULT_CONFIG_PATH,
    env_path: Annotated[
        Path,
        typer.Option(
            "--env-path",
            "-e",
            help="Path to .env (default: ./config/.env).",
        ),
    ] = _DEFAULT_ENV_PATH,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
) -> None:
    """Validate ``config.yaml`` schema + ``.env`` credential set."""
    from hardware_hunter.cli.commands.validate_config import run

    exit_code = run(config_path, env_path, output_format)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("test-search")
def cmd_test_search() -> None:
    """Run a one-shot search against a marketplace adapter (Epic 3)."""
    _placeholder()


@app.command("explain")
def cmd_explain() -> None:
    """Replay an LLM evaluation for a specific listing (Epic 3)."""
    _placeholder()


@app.command("health")
def cmd_health() -> None:
    """Print adapter, scheduler, and Phase 2 health snapshot (Epic 4)."""
    _placeholder()


@app.command("logs")
def cmd_logs() -> None:
    """Tail recent structured-log lines from the daemon (Epic 4)."""
    _placeholder()


@app.command("smoke-test")
def cmd_smoke_test() -> None:
    """Manually run the Phase 2 synthetic smoke test (Epic 5)."""
    _placeholder()


@login_app.command("wallapop")
def cmd_login_wallapop() -> None:
    """Interactive Wallapop browser cookie capture (Epic 2 Story 2.9)."""
    _placeholder()


@login_app.command("ebay")
def cmd_login_ebay() -> None:
    """eBay OAuth flow (Epic 2)."""
    _placeholder()


@phase2_app.command("enable")
def cmd_phase2_enable(
    entry: Annotated[str, typer.Argument(help="Entry key from wishlist.")],
) -> None:
    """Enable Phase 2 for an entry (Epic 5)."""
    _ = entry
    _placeholder()


@phase2_app.command("disable")
def cmd_phase2_disable(
    entry: Annotated[str | None, typer.Argument(help="Entry key, or omit with --all.")] = None,
    all_entries: Annotated[bool, typer.Option("--all", help="Disable Phase 2 globally.")] = False,
) -> None:
    """Disable Phase 2 (Epic 5)."""
    _ = (entry, all_entries)
    _placeholder()


@phase2_app.command("status")
def cmd_phase2_status() -> None:
    """Show Phase 2 enable scope (Epic 5)."""
    _placeholder()


@audit_app.command("show")
def cmd_audit_show() -> None:
    """Show recent audit-log entries (Epic 4)."""
    _placeholder()


@wishlist_app.command("list")
def cmd_wishlist_list() -> None:
    """List entries in the loaded wishlist (Epic 2)."""
    _placeholder()


def main() -> None:
    """Console-script entry point — referenced by ``[project.scripts]``."""
    app()
