"""``hardware-hunter init`` — Story 2.8 (FR40).

Scaffolds the three operator-facing files into the configured
``config_dir``:

  config_dir/.env              ← .env.example
  config_dir/wishlist.yaml     ← wishlist.example.yaml
  config_dir/config.yaml       ← config.example.yaml

Without ``--force``: refuses to overwrite an existing file (the operator
gets a one-line error + a hint nudging them at ``--force``).

With ``--force``: requires a TTY and a typed-token confirmation
(``OVERWRITE``) per UX-DR23. Non-interactive contexts (e.g.
``docker-compose run``) refuse outright per NFR-S6 — there is no
non-interactive overwrite path.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from hardware_hunter.observability.styling import render_prose

# Locked confirmation token — typing-not-y/n per UX-DR23.
OVERWRITE_TOKEN = "OVERWRITE"

# Mapping from the on-disk filename inside ``config_dir`` to the bundled
# template name inside ``hardware_hunter.templates``.
_TEMPLATES: dict[str, str] = {
    ".env": "dot.env.example",
    "wishlist.yaml": "wishlist.example.yaml",
    "config.yaml": "config.example.yaml",
}


def run(
    config_dir: Path,
    *,
    force: bool,
    isatty: Callable[[], bool] = sys.stdin.isatty,
    prompt: Callable[[str], str] = input,
) -> int:
    """Scaffold the three config files into ``config_dir``.

    ``isatty`` and ``prompt`` are dependency-injected so tests don't
    need to mock builtins or fake a real terminal.
    """
    config_dir = config_dir.resolve()
    existing = _existing_targets(config_dir)

    if existing and not force:
        target = existing[0]
        render_prose(
            f"{target.name} already exists at {target}",
            style="error",
            hint="pass --force to overwrite (you'll be asked to confirm)",
        )
        return 1

    if force and existing:
        if not isatty():
            render_prose(
                "--force requires an interactive terminal",
                style="error",
                hint="re-run from a TTY shell — there is no non-interactive overwrite path",
            )
            return 1
        confirmation = prompt(f"Type '{OVERWRITE_TOKEN}' to confirm: ")
        if confirmation.strip() != OVERWRITE_TOKEN:
            render_prose(
                "init cancelled (confirmation token did not match)",
                style="info",
            )
            return 1

    config_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for target_name, template_name in _TEMPLATES.items():
        target = config_dir / target_name
        _copy_template(template_name, target)
        written.append(target)

    _render_summary_panel(written)
    return 0


def _existing_targets(config_dir: Path) -> list[Path]:
    """Files inside ``config_dir`` that match one of the scaffolded names."""
    return [config_dir / name for name in _TEMPLATES if (config_dir / name).exists()]


def _copy_template(template_name: str, target: Path) -> None:
    """Copy a bundled template to ``target``.

    Uses ``importlib.resources`` so the lookup works for both editable
    installs and wheel installs.
    """
    template = resources.files("hardware_hunter.templates").joinpath(template_name)
    with resources.as_file(template) as src:
        shutil.copyfile(src, target)


def _render_summary_panel(written: list[Path]) -> None:
    """The success surface — a rounded panel listing the created files
    so the operator can see at a glance where the scaffolding landed.

    The console is configured wide so absolute paths (e.g. those under
    ``/tmp/pytest-of-…/`` in tests, or ``/app/config/`` in Docker)
    don't get truncated mid-line by rich's default width.
    """
    body = Text()
    for path in written:
        body.append("✓ ", style="bold green")
        body.append(f"{path}\n")

    panel = Panel(
        body,
        title="hardware-hunter init",
        box=ROUNDED,
        border_style="green",
        padding=(0, 1),
        expand=False,
    )
    Console(file=sys.stdout, width=4096, soft_wrap=True).print(panel)
