"""``salvager offer enable/disable/status`` (wallapop-offer-flow, FR65).

The offer sibling of ``phase2_cmd``: per-entry opt-in lives in
``wishlist.yaml`` (AR12 — rewritten through the ruamel round-trip
loader so comments survive), the global offer lockout lives in SQLite
(``offer_state``), and ``enable`` is the only path that lifts it.

``disable`` (per-entry) never touches the lockout; ``disable --all``
is the kill-everything path with the UX-DR23 typed-number confirmation
and engages the global lockout with ``operator_disable_all``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter
from salvager.config.wishlist_yaml import load_wishlist, save_wishlist
from salvager.domain.offer_audit import OfferStateSnapshot
from salvager.domain.wishlist import OfferSettings, Wishlist, WishlistEntry
from salvager.observability.logging import get_logger
from salvager.observability.styling import (
    ColumnSpec,
    print_table,
    render_prose,
    render_table,
)

_USAGE_EXIT = 2
_USER_CANCELLED_EXIT = 1


def _resolve_entry(wishlist: Wishlist, query: str) -> WishlistEntry | None:
    """Same match rules as ``phase2_cmd``: exact ref wins, else a unique
    case-insensitive substring match on ref / model / display name."""
    needle = query.casefold()
    exact = [e for e in wishlist.entries if e.ref.casefold() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    candidates = [
        e
        for e in wishlist.entries
        if needle in e.ref.casefold()
        or needle in e.model.casefold()
        or needle in e.display_name.casefold()
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _entry_not_found(query: str) -> int:
    render_prose(
        f"entry {query!r} not found in wishlist.yaml",
        style="error",
        hint="salvager offer status to see valid entry IDs",
    )
    return _USAGE_EXIT


def _save(wishlist: Wishlist, new_wishlist: Wishlist, wishlist_path: Path) -> None:
    yaml_doc = getattr(wishlist, "__yaml_doc__", None)
    if yaml_doc is not None:
        object.__setattr__(new_wishlist, "__yaml_doc__", yaml_doc)
    save_wishlist(wishlist_path, new_wishlist)


# ─────────────────────────────────────────────────────────────────────────
# `offer enable`
# ─────────────────────────────────────────────────────────────────────────


def run_enable(
    *,
    query: str,
    wishlist_path: Path,
    data_dir: Path,
    target_total_eur: str | None = None,
) -> int:
    """Flip an entry's ``offer.enabled`` and lift the global offer lockout."""
    wishlist = load_wishlist(wishlist_path)
    entry = _resolve_entry(wishlist, query)
    if entry is None:
        return _entry_not_found(query)

    target: Decimal | None = entry.offer.target_total_eur
    if target_total_eur is not None:
        try:
            target = Decimal(target_total_eur.strip().replace(",", "."))
        except InvalidOperation:
            render_prose(f"not a number: {target_total_eur!r}", style="error")
            return _USAGE_EXIT
        if target <= Decimal("0"):
            render_prose("target must be > 0", style="error")
            return _USAGE_EXIT

    new_offer = OfferSettings(enabled=True, target_total_eur=target)
    updated_entry = entry.model_copy(update={"offer": new_offer})
    new_entries = [updated_entry if e is entry else e for e in wishlist.entries]
    _save(wishlist, wishlist.model_copy(update={"entries": new_entries}), wishlist_path)

    asyncio.run(_clear_lockout(data_dir, entry))
    target_label = (
        f"target: {target} €" if target is not None else "target: entry ceiling (max_price_solo)"
    )
    render_prose(
        f"Offers enabled for {entry.display_name} ({target_label}; lockout cleared)",
        style="success",
    )
    return 0


async def _clear_lockout(data_dir: Path, entry: WishlistEntry) -> None:
    writer = OfferAuditWriter(db_path_under(data_dir))
    try:
        await writer.clear_global_disable(entry.entry_key)
    finally:
        await writer.close()


# ─────────────────────────────────────────────────────────────────────────
# `offer disable` and `offer disable --all`
# ─────────────────────────────────────────────────────────────────────────


def run_disable(
    *,
    query: str | None,
    all_entries: bool,
    wishlist_path: Path,
    data_dir: Path,
    is_tty: Callable[[], bool] = sys.stdin.isatty,
    input_fn: Callable[[str], str] = input,
) -> int:
    if all_entries:
        return _disable_all(
            wishlist_path=wishlist_path, data_dir=data_dir, is_tty=is_tty, input_fn=input_fn
        )
    if query is None:
        render_prose(
            "offer disable requires <entry> or --all",
            style="error",
            hint="salvager offer disable <entry-ref>",
        )
        return _USAGE_EXIT

    wishlist = load_wishlist(wishlist_path)
    entry = _resolve_entry(wishlist, query)
    if entry is None:
        return _entry_not_found(query)

    new_offer = OfferSettings(enabled=False, target_total_eur=entry.offer.target_total_eur)
    updated_entry = entry.model_copy(update={"offer": new_offer})
    new_entries = [updated_entry if e is entry else e for e in wishlist.entries]
    _save(wishlist, wishlist.model_copy(update={"entries": new_entries}), wishlist_path)
    render_prose(f"Offers disabled for {entry.display_name}", style="success")
    return 0


def _disable_all(
    *,
    wishlist_path: Path,
    data_dir: Path,
    is_tty: Callable[[], bool],
    input_fn: Callable[[str], str],
) -> int:
    if not is_tty():
        render_prose(
            "--all requires an interactive terminal",
            style="error",
            hint="re-run in a TTY, or disable entries individually",
        )
        return _USER_CANCELLED_EXIT

    wishlist = load_wishlist(wishlist_path)
    enabled_entries = [e for e in wishlist.entries if e.offer.enabled]
    count = len(enabled_entries)
    if count == 0:
        render_prose("no entries currently have offers enabled", style="info")
        return 0

    try:
        confirmation = input_fn(f"Type the number {count} to confirm: ")
    except (EOFError, KeyboardInterrupt):
        confirmation = ""
    if confirmation.strip() != str(count):
        render_prose("aborted — no changes made", style="info")
        return _USER_CANCELLED_EXIT

    new_entries = [
        e.model_copy(
            update={
                "offer": OfferSettings(enabled=False, target_total_eur=e.offer.target_total_eur)
            }
        )
        if e.offer.enabled
        else e
        for e in wishlist.entries
    ]
    _save(wishlist, wishlist.model_copy(update={"entries": new_entries}), wishlist_path)

    asyncio.run(_set_lockout(data_dir, reason="operator_disable_all"))
    log = get_logger("cli.offer")
    log.warning(
        "offer_disabled",
        extra={
            "reason": "operator_disable_all",
            "entries_disabled": count,
            "last_affected_entry": enabled_entries[-1].display_name,
        },
    )
    render_prose(
        f"Offers disabled for {count} entries · global lockout activated "
        "(reason: operator_disable_all)",
        style="success",
    )
    return 0


async def _set_lockout(data_dir: Path, *, reason: str) -> None:
    writer = OfferAuditWriter(db_path_under(data_dir))
    try:
        await writer.set_global_disable(reason)
    finally:
        await writer.close()


# ─────────────────────────────────────────────────────────────────────────
# `offer status`
# ─────────────────────────────────────────────────────────────────────────


def run_status(
    *,
    wishlist_path: Path,
    data_dir: Path,
    output_format: str = "human",
    daily_limit: int = 5,
    width: int = 80,
) -> int:
    if output_format not in ("human", "json"):
        render_prose(
            f"unknown --format value: {output_format!r}",
            style="error",
            hint="use --format human or --format json",
        )
        return _USAGE_EXIT

    if not wishlist_path.exists():
        render_prose(
            f"wishlist not found at {wishlist_path}",
            style="error",
            hint="run `salvager init` to scaffold one",
        )
        return _USER_CANCELLED_EXIT
    wishlist = load_wishlist(wishlist_path)
    state, sent_24h = asyncio.run(_read_state_and_budget(data_dir))

    rows: list[dict[str, object]] = []
    json_entries: list[dict[str, object]] = []
    for entry in wishlist.entries:
        target = (
            f"{entry.offer.target_total_eur} €"
            if entry.offer.target_total_eur is not None
            else "ceiling"
        )
        rows.append(
            {
                "Entry": entry.display_name,
                "Offers Enabled?": "yes" if entry.offer.enabled else "no",
                "Target": target if entry.offer.enabled else None,
            }
        )
        json_entries.append(
            {
                "entry_key": list(entry.entry_key),
                "display_name": entry.display_name,
                "offer_enabled": entry.offer.enabled,
                "target_total_eur": (
                    str(entry.offer.target_total_eur)
                    if entry.offer.target_total_eur is not None
                    else None
                ),
            }
        )

    footer = (
        f"Globally disabled: {'yes' if state.globally_disabled else 'no'} · "
        f"Failures: {state.consecutive_failures} · "
        f"Sent last 24h: {sent_24h}/{daily_limit}"
    )

    if output_format == "json":
        payload = {
            "entries": json_entries,
            "globally_disabled": state.globally_disabled,
            "disabled_reason": state.disabled_reason,
            "consecutive_failures": state.consecutive_failures,
            "sent_last_24h": sent_24h,
            "daily_limit": daily_limit,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0

    columns: list[ColumnSpec] = [
        {"key": "Entry"},
        {"key": "Offers Enabled?"},
        {"key": "Target", "align": "right"},
    ]
    print_table(render_table(rows, columns, width=width), width=width)
    render_prose(footer, style="info")
    return 0


async def _read_state_and_budget(data_dir: Path) -> tuple[OfferStateSnapshot, int]:
    db_path = db_path_under(data_dir)
    if not db_path.exists():
        return OfferStateSnapshot(globally_disabled=False, consecutive_failures=0), 0
    writer = OfferAuditWriter(db_path)
    try:
        return await writer.read_state(), await writer.count_recent_successes()
    finally:
        await writer.close()


__all__ = ["run_disable", "run_enable", "run_status"]
