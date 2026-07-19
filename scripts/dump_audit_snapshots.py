#!/usr/bin/env python3
"""Dump syrupy snapshots to readable .txt reference files for Story 5.17.

Reads the .ambr snapshot files for the alert renderers and writes one
plain-text file per variant under ``docs/release-audits/v1.0/reference-text/``
so the auditor has the *source* MarkdownV2 string at hand when
comparing against what each Telegram client actually renders.

Why this helps:
  - The .ambr files mix many variants per file with syrupy-specific
    indent + ``# ---`` separators that are awkward to skim.
  - The reference files are flat: one variant per file, no header
    noise, named after the variant so a directory listing is the
    catalog.
  - Operators running the manual audit can ``diff`` what they screenshot
    against the reference text and instantly spot a rendering drift.

Exits 0 on success; 1 if any of the source .ambr files is missing.
Zero external dependencies; safe to run from CI or locally.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = REPO_ROOT / "tests" / "unit" / "__snapshots__"
OUTPUT_DIR = REPO_ROOT / "docs" / "release-audits" / "v1.0" / "reference-text"

#: Source files + the test-name prefix that identifies their parametrized
#: variants. Anything not matching the prefix is copied under its own
#: snapshot name (for non-parametrized snapshots).
SOURCES: Final[list[tuple[str, str, str]]] = [
    # (ambr filename, subdirectory, parametrize-id-prefix)
    (
        "test_operational_alert_renderer.ambr",
        "operational",
        "test_operational_alert_matches_snapshot",
    ),
    (
        "test_phase2_buy_renderers.ambr",
        "phase2-buy",
        "test_every_variant_has_a_snapshot",
    ),
    (
        "test_phase2_renderer_snapshots.ambr",
        "phase2-listing",
        "test_snapshot",
    ),
    (
        "test_alert_renderer_snapshots.ambr",
        "phase1-listing",
        "test_snapshot",
    ),
    (
        "test_alert_update_snapshots.ambr",
        "alert-updates",
        "test_snapshot",
    ),
]

_NAME_RE = re.compile(r"^# name:\s*(.+?)\s*$")
_SEP_RE = re.compile(r"^# ---\s*$")


def _parse_ambr(path: Path) -> dict[str, str]:
    """Return ``{snapshot_name: payload_text}`` for one .ambr file.

    syrupy's ambr format:
      ``# name: <name>``
      ``  '''<line>\\n  <line>\\n  ...'''``
      ``# ---``

    The payload is the triple-quoted block, indented by 2 spaces. We
    strip the outer ``'''`` markers and the 2-space indent.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        match = _NAME_RE.match(lines[i])
        if not match:
            i += 1
            continue
        name = match.group(1)
        i += 1
        # Expect: '''  ...content...  '''
        payload: list[str] = []
        if i < len(lines) and lines[i].strip() == "'''":
            i += 1  # skip opening '''
            while i < len(lines) and lines[i].strip() != "'''":
                # 2-space syrupy indent
                payload.append(lines[i][2:] if lines[i].startswith("  ") else lines[i])
                i += 1
            i += 1  # skip closing '''
        else:
            # Single-line snapshot: syrupy stores a Python string literal
            # (2-space indent, quoted, backslashes doubled). Decode it so
            # the reference file carries the actual MarkdownV2 text, not
            # the repr; unknown shapes pass through untouched.
            raw = lines[i].strip()
            try:
                decoded = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                decoded = raw
            payload.append(decoded if isinstance(decoded, str) else raw)
            i += 1
        # Skip the trailing # --- separator if present.
        if i < len(lines) and _SEP_RE.match(lines[i]):
            i += 1
        out[name] = "\n".join(payload).rstrip("\n") + "\n"
    return out


def _variant_filename(snapshot_name: str, prefix: str) -> str:
    """Map a syrupy snapshot name to a friendly variant filename.

    Examples:
      ``test_operational_alert_matches_snapshot[daemon_started]``
        → ``daemon_started.txt``
      ``test_every_variant_has_a_snapshot[circuit_open]``
        → ``circuit_open.txt``
      ``test_snapshot_direct`` (test_phase2_renderer_snapshots)
        → ``snapshot_direct.txt``
    """
    if snapshot_name.startswith(prefix + "["):
        inside = snapshot_name[len(prefix) + 1 : -1]  # drop "[" and "]"
        return f"{inside}.txt"
    # Strip a leading "test_" for non-parametrized snapshots.
    base = snapshot_name.removeprefix("test_")
    return f"{base}.txt"


def main() -> int:
    if not SNAPSHOTS_DIR.is_dir():
        print(f"ERROR: snapshots dir not found: {SNAPSHOTS_DIR}", file=sys.stderr)
        return 1

    missing: list[str] = []
    written = 0

    for filename, subdir, prefix in SOURCES:
        src = SNAPSHOTS_DIR / filename
        if not src.is_file():
            missing.append(filename)
            continue
        target_dir = OUTPUT_DIR / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in _parse_ambr(src).items():
            filename_out = _variant_filename(name, prefix)
            (target_dir / filename_out).write_text(payload, encoding="utf-8")
            written += 1

    if missing:
        print(f"ERROR: missing snapshot files: {missing}", file=sys.stderr)
        return 1

    print(f"OK wrote {written} reference-text files under {OUTPUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
