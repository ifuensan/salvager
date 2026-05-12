"""CLI rendering helpers — the single source of truth for operator-facing output.

Every CLI subcommand calls :func:`render_table` or :func:`render_prose`; no
command writes to stdout/stderr directly. Code review rejects bare ``print``
calls in ``cli/commands/``.

Why two helpers, not more
-------------------------
- :func:`render_table` for multi-record output (``audit show``, ``health``,
  ``phase2 status``, ``wishlist list``).
- :func:`render_prose` for single-record output: success markers, errors,
  validation results, single-fact reports.

The token-to-style map (:data:`THEME`) is locked at v1 per UX-DR16. Adding a
token is a PRD amendment, not a one-line change.

Forbidden v1 surfaces (UX-DR17)
-------------------------------
``rich.progress.Progress`` and ``rich.status.Status`` are NOT used at v1. The
daemon is a polling agent — there is no progress-bar-shaped work — and the
spinner adds nothing for an operator who reads logs over ssh. Reintroducing
either requires a PRD amendment.

Color independence (UX-DR22)
----------------------------
Text prefixes (``✓``, ``error:``, ``warn:``) carry semantics independently of
color. Honors ``--no-color`` flags, the ``NO_COLOR=1`` env var, and piped
stdout — in all three the prefixes are preserved and the ANSI escapes are
stripped.
"""

from __future__ import annotations

import os
import sys
from typing import IO, Literal, TypedDict

from rich.box import MINIMAL
from rich.console import Console
from rich.table import Table

# ─────────────────────────────────────────────────────────────────────────
# Theme — locked per UX-DR16. Seven tokens, no more, no fewer.
# ─────────────────────────────────────────────────────────────────────────

ThemeToken = Literal[
    "error",
    "warn",
    "success",
    "info",
    "emphasis",
    "secondary",
    "code",
]

THEME: dict[ThemeToken, str] = {
    "error": "bold red",
    "warn": "bold yellow",
    "success": "bold green",
    "info": "bold blue",
    "emphasis": "bold",
    "secondary": "dim",
    "code": "cyan",
}

# Plain-text prefixes are part of the public contract (color-independence).
_PROSE_PREFIX: dict[ThemeToken, str] = {
    "error": "error: ",
    "warn": "warn: ",
    "success": "✓ ",
    "info": "",
    "secondary": "",
    "emphasis": "",
    "code": "",
}

# stderr targets — errors and warnings go to fd 2 so JSON output on stdout
# stays parseable when both surfaces are active simultaneously.
_STDERR_TOKENS: frozenset[ThemeToken] = frozenset({"error", "warn"})

_DEFAULT_TABLE_WIDTH = 80


class ColumnSpec(TypedDict, total=False):
    """Spec for one column in :func:`render_table`.

    ``key`` (required) names the dict key on each row. ``header`` overrides
    the displayed header (defaults to ``key``). ``style`` is a theme token
    or raw rich style string. ``align`` is ``"left"`` (default), ``"right"``
    (use for numeric values per UX content rules), or ``"center"``.
    """

    key: str
    header: str
    style: str
    align: Literal["left", "right", "center"]


def _color_disabled() -> bool:
    """Return True if ANSI color must be suppressed.

    Honors three independent signals: ``NO_COLOR`` env (any non-empty value
    per https://no-color.org), ``HARDWARE_HUNTER_NO_COLOR=1``, and a
    non-TTY stdout (piped output, redirected to file, captured in CI)."""
    if os.environ.get("NO_COLOR"):
        return True
    if os.environ.get("HARDWARE_HUNTER_NO_COLOR") == "1":
        return True
    return not sys.stdout.isatty()


def _build_console(stream: IO[str], *, width: int | None = None) -> Console:
    no_color = _color_disabled()
    return Console(
        file=stream,
        width=width,
        force_terminal=not no_color,
        no_color=no_color,
        highlight=False,
        soft_wrap=False,
    )


def render_table(
    rows: list[dict[str, object]],
    columns: list[ColumnSpec],
    *,
    width: int = _DEFAULT_TABLE_WIDTH,
) -> Table:
    """Render multi-record output as a `rich.table.Table`.

    The returned table is configured per UX-DR16 (``box=MINIMAL``, bold
    header, no row separators, 80-col default). Empty ``rows`` returns an
    empty (header-only) table; callers are expected to fall back to
    :func:`render_prose` for "no results" messaging.
    """
    table = Table(
        box=MINIMAL,
        header_style="bold",
        show_lines=False,
        show_edge=False,
        pad_edge=False,
        width=width,
    )
    for column in columns:
        key = column["key"]
        header = column.get("header", key)
        style = column.get("style", "")
        align = column.get("align", "left")
        table.add_column(
            header,
            style=style,
            justify=align,
            no_wrap=False,
        )
    for row in rows:
        cells = [_cell(row.get(column["key"])) for column in columns]
        table.add_row(*cells)
    return table


def _cell(value: object) -> str:
    """Cell formatting rule: ``None`` → em dash (UX-DR16); else ``str()``."""
    if value is None:
        return "—"
    return str(value)


_PROSE_WIDTH = 4096


def render_prose(
    message: str,
    style: ThemeToken,
    hint: str | None = None,
) -> None:
    """Render single-record output. Errors/warns go to stderr; others stdout.

    With color enabled, the message body uses :data:`THEME`'s style. With
    color disabled (``NO_COLOR`` / piped stdout), output is plain text and
    the prefix glyph (``✓``, ``error:``, ``warn:``) is preserved so the
    semantics survive screen readers and grep-style consumers (UX-DR22).

    The console is built at a deliberately wide width so paths and other
    long error tokens (e.g. ``/tmp/.../wishlist.yaml:9: …``) never get
    soft-wrapped mid-line — that breaks grep, ``jq``-friendly parsing,
    and any test that asserts a substring on the rendered output.
    """
    stream: IO[str] = sys.stderr if style in _STDERR_TOKENS else sys.stdout
    console = _build_console(stream, width=_PROSE_WIDTH)

    prefix = _PROSE_PREFIX[style]
    body_style = THEME[style]

    # markup=False so user-supplied content containing square brackets
    # (e.g. an error message like "[phase2.x] out of range") doesn't get
    # silently consumed by rich's markup parser.
    if _color_disabled():
        console.print(f"{prefix}{message}", style=None, markup=False)
        if hint is not None:
            console.print(f"hint: {hint}", style=None, markup=False)
    else:
        console.print(f"{prefix}{message}", style=body_style, markup=False)
        if hint is not None:
            console.print(f"hint: {hint}", style="dim", markup=False)
