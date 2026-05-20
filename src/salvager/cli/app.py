"""Typer CLI skeleton — Story 1.8 (FR39).

This module wires the ``salvager`` console script: a root typer app
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

import asyncio
import contextlib
import json
import os
import signal
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from salvager.observability.logging import get_logger
from salvager.observability.styling import render_prose

if TYPE_CHECKING:
    from salvager.orchestration.composer import ComposedDaemon

app = typer.Typer(
    name="salvager",
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

# `dev` subcommand group (Story 5.17): release-audit support — fires
# canonical alert variants against the configured Telegram chat so the
# operator can run the UX-DR32 client-variance audit without provoking
# the underlying daemon state each time.
from salvager.cli.commands import dev_cmd as _dev_cmd  # noqa: E402

_dev_cmd.register(app)


# Default paths — defined here (before the root callback) because typer
# evaluates the callback's default arguments at decoration time. Each
# is overridable via the matching ``--{name}-path`` CLI flag.
_DEFAULT_CONFIG_DIR = Path("config")
_DEFAULT_WISHLIST_PATH = _DEFAULT_CONFIG_DIR / "wishlist.yaml"
_DEFAULT_CONFIG_PATH = _DEFAULT_CONFIG_DIR / "config.yaml"
_DEFAULT_ENV_PATH = _DEFAULT_CONFIG_DIR / ".env"
_DEFAULT_DATA_DIR = Path("/app/data")
_EBAY_DEFAULT_SCOPE = "https://api.ebay.com/oauth/api_scope"


# ─────────────────────────────────────────────────────────────────────────
# Root callback — bare `salvager` runs the daemon (FR39)
# ─────────────────────────────────────────────────────────────────────────


@app.callback()
def _root(
    ctx: typer.Context,
    config_path: Annotated[
        Path,
        typer.Option(
            "--config-path",
            "-c",
            help="Path to config.yaml (default: ./config/config.yaml).",
        ),
    ] = _DEFAULT_CONFIG_PATH,
    wishlist_path: Annotated[
        Path,
        typer.Option(
            "--wishlist-path",
            "-w",
            help="Path to wishlist.yaml (default: ./config/wishlist.yaml).",
        ),
    ] = _DEFAULT_WISHLIST_PATH,
    env_path: Annotated[
        Path,
        typer.Option(
            "--env-path",
            "-e",
            help="Path to .env (default: ./config/.env).",
        ),
    ] = _DEFAULT_ENV_PATH,
) -> None:
    """Run the daemon when invoked without a subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    _run_daemon(config_path=config_path, wishlist_path=wishlist_path, env_path=env_path)


def _run_daemon(*, config_path: Path, wishlist_path: Path, env_path: Path) -> None:
    """Compose every adapter and run the daemon until SIGTERM/SIGINT.

    Exit-code semantics (FR48):
      - ``4`` — missing credentials (delegated to :func:`load_env_or_exit`).
      - ``5`` — no marketplaces have credentials on disk (no work to do).
      - ``0`` — clean shutdown after a signal.
    """
    from salvager.config.env import load_env_or_exit
    from salvager.orchestration.composer import (
        NoMarketplacesEnabledError,
        compose_daemon,
    )

    log = get_logger("daemon")
    env = load_env_or_exit(env_path)

    try:
        composed = compose_daemon(
            env,
            config_path=config_path,
            wishlist_path=wishlist_path,
        )
    except NoMarketplacesEnabledError as exc:
        log.error("daemon_no_marketplaces_enabled", extra={"reason": str(exc)})
        render_prose(
            "no marketplace credentials found",
            style="error",
            hint="run `salvager login wallapop` or `salvager login ebay` first",
        )
        raise typer.Exit(code=5) from exc

    log.info("daemon_starting", extra={"version": _resolve_version()})
    asyncio.run(_serve(composed))


async def _serve(composed: ComposedDaemon) -> None:
    """Start the daemon + the Telegram listener; block until shutdown.

    Two long-lived async tasks run concurrently:

    - ``daemon.serve_until_shutdown_signal()`` — the scheduler-driven
      poll loop.
    - ``telegram.listen_callbacks()`` — the Telegram long-poll loop
      that routes view/skip/snooze (and, eventually, buy) taps to the
      callback dispatcher.

    SIGTERM/SIGINT triggers a clean shutdown on the daemon AND cancels
    the listener task; the daemon's drain semantics (FR50, 30s
    in-flight cap) cover the poll side, and the listener exits on
    the first ``asyncio.CancelledError`` it sees on its ``await``.
    """
    from datetime import UTC, datetime

    daemon = composed.daemon

    # Persist daemon identity to `_meta` so `salvager health`
    # (Story 4.4) can report version / PID / uptime — and so a later
    # `health` run can tell a *running* daemon from a stale heartbeat.
    started_at = datetime.now(UTC).isoformat()
    await composed.store.set_meta("daemon_pid", str(os.getpid()))
    await composed.store.set_meta("daemon_started_at", started_at)
    await composed.store.set_meta("daemon_version", _resolve_version())

    await daemon.start()
    loop = asyncio.get_running_loop()
    log = get_logger("daemon")
    # Keep strong refs to the shutdown tasks so they aren't GC'd mid-drain.
    shutdown_tasks: set[asyncio.Task[None]] = set()

    async def _listener_supervisor() -> None:
        """Run the Telegram listener under a catch-all.

        A non-retryable Telegram failure (invalid bot token, bot
        kicked) would otherwise propagate through the task and
        either trip Python's "Task exception was never retrieved"
        warning at GC time OR — worse — re-raise during cleanup and
        skip ``daemon.shutdown()`` + the SQLite ``aclose()`` (data
        flush). The daemon keeps polling (outbound alerts still
        work and will surface the same auth failure with their own
        loud log), the operator sees the error in the log stream,
        and the listener stops cleanly without dragging the rest
        of the daemon down with it.
        """
        try:
            await composed.telegram.listen_callbacks(composed.dispatcher.handle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "telegram_listener_terminated",
                extra={"error_class": exc.__class__.__name__, "detail": str(exc)},
            )

    listener_task = asyncio.create_task(
        _listener_supervisor(),
        name="telegram_callback_listener",
    )

    def _on_signal(reason: str) -> None:
        task = asyncio.create_task(daemon.shutdown(reason=reason))
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)
        # The listener's long-poll is blocking on Telegram's server-
        # side timeout; cancelling unblocks it immediately so the
        # process can exit instead of waiting up to 30 s for the
        # next get_updates response.
        listener_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows event-loops don't support add_signal_handler; on
        # those, Ctrl-C still works via KeyboardInterrupt.
        reason = "sigterm" if sig == signal.SIGTERM else "sigint"
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal, reason)

    try:
        await daemon.serve_until_shutdown_signal()
    finally:
        listener_task.cancel()
        # The supervisor coroutine guarantees this only ever completes
        # via CancelledError or clean return (no other exception types
        # propagate). Suppressing CancelledError covers the typical
        # cancel-on-shutdown path; clean return needs no handling.
        with contextlib.suppress(asyncio.CancelledError):
            await listener_task
        await daemon.shutdown()
        await composed.aclose()


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

    render_prose(f"salvager {ver} ({commit})", style="info")


def _resolve_version() -> str:
    try:
        return version("salvager")
    except PackageNotFoundError:
        return "unknown"


def _resolve_commit() -> str:
    """Resolve the git commit short SHA.

    Order: ``SALVAGER_COMMIT`` env var (set by the Dockerfile at
    build time, lands in a follow-up) → ``git rev-parse`` when a working
    tree is present → ``unknown``.
    """
    env_commit = os.environ.get("SALVAGER_COMMIT")
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
def cmd_init(
    config_dir: Annotated[
        Path,
        typer.Option(
            "--config-dir",
            "-d",
            help="Where to scaffold the example files (default: ./config).",
        ),
    ] = _DEFAULT_CONFIG_DIR,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing files after typing OVERWRITE."),
    ] = False,
) -> None:
    """Scaffold .env, wishlist.yaml, and config.yaml from bundled examples."""
    from salvager.cli.commands.init_cmd import run

    exit_code = run(config_dir, force=force)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


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
    from salvager.cli.commands.validate_wishlist import run

    exit_code = run(path, output_format)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


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
    from salvager.cli.commands.validate_config import run

    exit_code = run(config_path, env_path, output_format)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("test-search")
def cmd_test_search(
    query: Annotated[
        str,
        typer.Argument(help="A wishlist entry ref, or an arbitrary free-text query."),
    ],
    marketplace: Annotated[
        str | None,
        typer.Option("--marketplace", help="Limit to one marketplace: wallapop or ebay."),
    ] = None,
    evaluate: Annotated[
        bool,
        typer.Option("--evaluate", help="Run the LLM evaluator on each result (uses cache)."),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
    config_path: Annotated[
        Path,
        typer.Option("--config-path", "-c", help="Path to config.yaml."),
    ] = _DEFAULT_CONFIG_PATH,
    wishlist_path: Annotated[
        Path,
        typer.Option("--wishlist-path", "-w", help="Path to wishlist.yaml."),
    ] = _DEFAULT_WISHLIST_PATH,
    env_path: Annotated[
        Path,
        typer.Option("--env-path", "-e", help="Path to .env."),
    ] = _DEFAULT_ENV_PATH,
) -> None:
    """Dry-run a marketplace search — no alerts, no state writes (Story 4.6)."""
    from salvager.cli.commands.test_search_cmd import run
    from salvager.config.config_yaml import load_config
    from salvager.config.env import load_env_or_exit
    from salvager.config.wishlist_yaml import load_wishlist

    env = load_env_or_exit(env_path)
    exit_code = run(
        query_or_entry=query,
        env=env,
        config=load_config(config_path),
        wishlist=load_wishlist(wishlist_path),
        data_dir=data_dir,
        marketplace=marketplace,
        evaluate=evaluate,
        output_format=output_format,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("explain")
def cmd_explain(
    url: Annotated[str, typer.Argument(help="The marketplace listing URL to evaluate.")],
    entry: Annotated[
        str | None,
        typer.Option("--entry", help="Evaluate only this wishlist entry ref (skips heuristic)."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
    config_path: Annotated[
        Path,
        typer.Option("--config-path", "-c", help="Path to config.yaml."),
    ] = _DEFAULT_CONFIG_PATH,
    wishlist_path: Annotated[
        Path,
        typer.Option("--wishlist-path", "-w", help="Path to wishlist.yaml."),
    ] = _DEFAULT_WISHLIST_PATH,
    env_path: Annotated[
        Path,
        typer.Option("--env-path", "-e", help="Path to .env."),
    ] = _DEFAULT_ENV_PATH,
) -> None:
    """Replay the full LLM evaluation for one listing URL (Story 4.7)."""
    from salvager.cli.commands.explain_cmd import run
    from salvager.config.config_yaml import load_config
    from salvager.config.env import load_env_or_exit
    from salvager.config.wishlist_yaml import load_wishlist

    env = load_env_or_exit(env_path)
    exit_code = run(
        url=url,
        env=env,
        config=load_config(config_path),
        wishlist=load_wishlist(wishlist_path),
        data_dir=data_dir,
        entry_ref=entry,
        output_format=output_format,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("health")
def cmd_health(
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
    config_path: Annotated[
        Path,
        typer.Option(
            "--config-path",
            "-c",
            help="Path to config.yaml (default: ./config/config.yaml).",
        ),
    ] = _DEFAULT_CONFIG_PATH,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
) -> None:
    """Print adapter status, daemon liveness, and Phase 1 activity (Story 4.4)."""
    from salvager.cli.commands.health_cmd import run

    exit_code = run(
        data_dir=data_dir,
        config_path=config_path,
        output_format=output_format,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command("logs")
def cmd_logs() -> None:
    """Tail recent structured-log lines from the daemon (Epic 4)."""
    _placeholder()


@login_app.command("wallapop")
def cmd_login_wallapop(
    data_dir: Annotated[
        Path,
        typer.Option(
            "--data-dir",
            "-d",
            help="Where to write the captured cookie file (default: /app/data).",
        ),
    ] = _DEFAULT_DATA_DIR,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            help="Max seconds to wait for the operator to complete login.",
            min=10,
        ),
    ] = 300,
) -> None:
    """Interactive Wallapop browser cookie capture (Story 2.9)."""
    from salvager.cli.commands.login_wallapop import run

    exit_code = run(data_dir, timeout_s=timeout)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@login_app.command("ebay")
def cmd_login_ebay(
    ru_name: Annotated[
        str,
        typer.Option(
            "--ru-name",
            help="Your eBay RuName (registered redirect-URL name).",
        ),
    ],
    data_dir: Annotated[
        Path,
        typer.Option(
            "--data-dir",
            "-d",
            help="Where to write the OAuth token file (default: /app/data).",
        ),
    ] = _DEFAULT_DATA_DIR,
    scope: Annotated[
        str,
        typer.Option("--scope", help="OAuth scope to request (default: Browse API scope)."),
    ] = _EBAY_DEFAULT_SCOPE,
    env_path: Annotated[
        Path,
        typer.Option("--env-path", "-e", help="Path to .env (default: ./config/.env)."),
    ] = _DEFAULT_ENV_PATH,
) -> None:
    """eBay OAuth authorization-code flow (Story 2.10)."""
    from salvager.cli.commands.login_ebay import run
    from salvager.config.env import load_env_or_exit

    env = load_env_or_exit(env_path)
    exit_code = run(
        data_dir,
        app_id=env.EBAY_APP_ID,
        cert_id=env.EBAY_CERT_ID,
        ru_name=ru_name,
        scope=scope,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@phase2_app.command("enable")
def cmd_phase2_enable(
    entry: Annotated[str, typer.Argument(help="Entry key from wishlist.")],
    wishlist_path: Annotated[
        Path,
        typer.Option("--wishlist-path", "-w", help="Path to wishlist.yaml."),
    ] = _DEFAULT_WISHLIST_PATH,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
) -> None:
    """Enable Phase 2 for an entry — Story 5.12 (FR45 / AR12)."""
    from salvager.cli.commands import phase2_cmd

    exit_code = phase2_cmd.run_enable(query=entry, wishlist_path=wishlist_path, data_dir=data_dir)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@phase2_app.command("disable")
def cmd_phase2_disable(
    entry: Annotated[str | None, typer.Argument(help="Entry key, or omit with --all.")] = None,
    all_entries: Annotated[bool, typer.Option("--all", help="Disable Phase 2 globally.")] = False,
    wishlist_path: Annotated[
        Path,
        typer.Option("--wishlist-path", "-w", help="Path to wishlist.yaml."),
    ] = _DEFAULT_WISHLIST_PATH,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
) -> None:
    """Disable Phase 2 — per entry or globally (Story 5.12 / UX-DR23)."""
    from salvager.cli.commands import phase2_cmd

    exit_code = phase2_cmd.run_disable(
        query=entry,
        all_entries=all_entries,
        wishlist_path=wishlist_path,
        data_dir=data_dir,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


_DEFAULT_FIXTURES_DIR = Path("tests/fixtures/price_parsers/active")


@phase2_app.command("smoke-test")
def cmd_phase2_smoke_test(
    env_path: Annotated[
        Path,
        typer.Option("--env-path", "-e", help="Path to .env."),
    ] = _DEFAULT_ENV_PATH,
    config_path: Annotated[
        Path,
        typer.Option("--config-path", "-c", help="Path to config.yaml."),
    ] = _DEFAULT_CONFIG_PATH,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
    fixtures_dir: Annotated[
        Path,
        typer.Option(
            "--fixtures-dir",
            help="Directory of price-parser fixtures.",
        ),
    ] = _DEFAULT_FIXTURES_DIR,
) -> None:
    """Manually run the Phase 2 synthetic smoke test (Story 5.13)."""
    from salvager.cli.commands import phase2_cmd

    exit_code = phase2_cmd.run_smoke_test(
        env_path=env_path,
        config_path=config_path,
        data_dir=data_dir,
        fixtures_dir=fixtures_dir,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@phase2_app.command("reconcile")
def cmd_phase2_reconcile(
    receipt_id: Annotated[
        str,
        typer.Argument(help="Receipt ID (or numeric audit_id) of a past transaction."),
    ],
    config_path: Annotated[
        Path,
        typer.Option("--config-path", "-c", help="Path to config.yaml."),
    ] = _DEFAULT_CONFIG_PATH,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: human | json."),
    ] = "human",
) -> None:
    """Re-run reconciliation on a past receipt (Story 5.13)."""
    from salvager.cli.commands import phase2_cmd

    exit_code = phase2_cmd.run_reconcile(
        receipt_or_audit_id=receipt_id,
        config_path=config_path,
        data_dir=data_dir,
        output_format=output_format,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@phase2_app.command("status")
def cmd_phase2_status(
    wishlist_path: Annotated[
        Path,
        typer.Option("--wishlist-path", "-w", help="Path to wishlist.yaml."),
    ] = _DEFAULT_WISHLIST_PATH,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: human | json."),
    ] = "human",
) -> None:
    """Print Phase 2 enablement table + global state (Story 5.12)."""
    from salvager.cli.commands import phase2_cmd

    exit_code = phase2_cmd.run_status(
        wishlist_path=wishlist_path,
        data_dir=data_dir,
        output_format=output_format,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@audit_app.command("show")
def cmd_audit_show(
    last: Annotated[
        int,
        typer.Option("--last", "-n", help="Show the N most recent records (default 10)."),
    ] = 10,
    record_id: Annotated[
        int | None,
        typer.Option("--id", help="Show a single record by audit_id, in full detail."),
    ] = None,
    type_filter: Annotated[
        str | None,
        typer.Option("--type", help="Filter by record type: alert, callback, or dropped."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only records at/after this ISO 8601 date or datetime."),
    ] = None,
    include_dropped: Annotated[
        bool,
        typer.Option("--include-dropped", help="Include dropped-below-threshold sightings."),
    ] = False,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: 'human' (default) or 'json'."),
    ] = "human",
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
) -> None:
    """Inspect the local append-only audit log (Story 4.5)."""
    from salvager.cli.commands.audit_cmd import run_show

    exit_code = run_show(
        data_dir=data_dir,
        last=last,
        record_id=record_id,
        type_filter=type_filter,
        since=since,
        include_dropped=include_dropped,
        output_format=output_format,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@audit_app.command("export")
def cmd_audit_export(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only records at/after this ISO 8601 date or datetime."),
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format (only 'json' / JSONL is supported)."),
    ] = "json",
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", "-d", help="Daemon state dir (default: /app/data)."),
    ] = _DEFAULT_DATA_DIR,
) -> None:
    """Stream the full audit log as JSON Lines (Story 4.5)."""
    from salvager.cli.commands.audit_cmd import run_export

    _ = output_format  # JSONL is the only supported shape; flag kept for symmetry
    exit_code = run_export(data_dir=data_dir, since=since)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@wishlist_app.command("list")
def cmd_wishlist_list() -> None:
    """List entries in the loaded wishlist (Epic 2)."""
    _placeholder()


def main() -> None:
    """Console-script entry point — referenced by ``[project.scripts]``."""
    app()
