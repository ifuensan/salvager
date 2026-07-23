"""Developer-only commands — Story 5.17 release-audit support.

The single command here, ``salvager dev emit-alert <variant>``,
fires any of the locked alert variants against the configured
Telegram chat. It is the prerequisite the release checklist names:

  - 6 listing variants (Phase 1 / Phase 2, direct / container / missing-photo)
  - 1 Phase 2 buy receipt
  - 8 Phase 2 buy-failure variants
  - 22 operational ``EventName`` variants

…rendered with the same fixture data the snapshot tests use, so what
the auditor sees on each Telegram client is *bit-for-bit* the string
under ``docs/release-audits/v1.0/reference-text/<variant>.txt``.

The command is intentionally limited to MarkdownV2-bearing variants —
``--dry-run`` prints the rendered text to stdout for shell-piping
inspection without touching the Telegram API. Production deploys
should still ship this command (it's the only knob the operator has
to re-run the audit on a re-built image).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console

from salvager.cli.dev_alert_fixtures import (
    VARIANT_REGISTRY,
    build_rendered_variant,
)


def register(app: typer.Typer) -> None:
    """Attach the ``dev`` subcommand group to the root app."""
    dev_app = typer.Typer(
        help=(
            "Developer-only commands for the v1.0 release audit. "
            "Not part of the daily-driver surface."
        )
    )
    dev_app.command("emit-alert")(emit_alert)
    dev_app.command("list-variants")(list_variants)
    app.add_typer(dev_app, name="dev")


_EMIT_HELP: Final[str] = (
    "Fire one rendered alert variant against the configured Telegram chat. "
    "Use --list-variants to discover names. Use --dry-run to print the "
    "rendered MarkdownV2 text to stdout without sending."
)


def emit_alert(
    variant: Annotated[str, typer.Argument(help="Variant name. `dev list-variants` shows all.")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the rendered text to stdout instead of sending to Telegram.",
        ),
    ] = False,
    env_path: Annotated[
        Path,
        typer.Option("--env-path", "-e", help="Path to .env (default: ./config/.env)."),
    ] = Path("config/.env"),
) -> None:
    """Fire one alert variant against the configured Telegram chat."""
    console = Console()

    if variant not in VARIANT_REGISTRY:
        console.print(f"[red]Unknown variant:[/red] {variant}")
        console.print("Run `salvager dev list-variants` for the catalog.")
        raise typer.Exit(code=2)

    rendered = build_rendered_variant(variant)

    if dry_run:
        console.print(f"[dim]# variant: {variant}[/dim]")
        console.print(f"[dim]# parse_mode: {rendered.parse_mode}[/dim]")
        if rendered.photo_url:
            console.print(f"[dim]# photo_url: {rendered.photo_url}[/dim]")
        if rendered.inline_keyboard:
            console.print(
                f"[dim]# keyboard: "
                f"{[[b.text for b in row] for row in rendered.inline_keyboard]}[/dim]"
            )
        console.print(rendered.text)
        return

    from salvager.adapters.telegram_bot.surface import TelegramBotSurface
    from salvager.config.env import load_env_or_exit

    env = load_env_or_exit(env_file=env_path)
    telegram = TelegramBotSurface(
        bot_token=env.TELEGRAM_BOT_TOKEN,
        recipient_chat_id=env.TELEGRAM_CHAT_ID,
    )
    message_id = asyncio.run(telegram.send(rendered))
    console.print(f"[green]OK[/green] sent {variant} as message_id={message_id}")


def list_variants() -> None:
    """List every variant name + a short label, grouped by surface."""
    console = Console()
    groups: dict[str, list[str]] = {}
    for name in VARIANT_REGISTRY:
        group = _group_of(name)
        groups.setdefault(group, []).append(name)
    for group, names in groups.items():
        console.print(f"[bold]{group}[/bold]  ({len(names)})")
        for name in names:
            console.print(f"  {name}")
        console.print()
    console.print(f"[dim]{len(VARIANT_REGISTRY)} variants total.[/dim]")


def _group_of(variant_name: str) -> str:
    if variant_name.startswith("phase1_listing"):
        return "phase1 listing"
    if variant_name.startswith("phase2_listing"):
        return "phase2 listing"
    if variant_name.startswith("negotiable_listing"):
        return "negotiable listing"
    if variant_name.startswith("buy_success"):
        return "phase2 buy success"
    if variant_name.startswith("buy_failure_"):
        return "phase2 buy failure"
    if variant_name == "offer_sent":
        return "offer sent"
    if variant_name.startswith("offer_failure_"):
        return "offer failure"
    return "operational"


__all__ = ["emit_alert", "list_variants", "register"]
