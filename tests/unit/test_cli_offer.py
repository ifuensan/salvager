"""Tests for ``salvager offer enable/disable/status`` (wallapop-offer-flow).

Mirrors ``test_cli_phase2.py``: the ``run_*`` functions are exercised
directly with a real wishlist file (genuine ruamel round-trip) and a
migrated SQLite DB; TTY/``input()`` semantics use injected fakes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from salvager.adapters.sqlite_store import MigrationRunner, open_connection
from salvager.adapters.sqlite_store.migrations import db_path_under
from salvager.adapters.sqlite_store.offer_writer import OfferAuditWriter
from salvager.cli.commands import offer_cmd
from salvager.config.wishlist_yaml import load_wishlist
from salvager.domain.offer_audit import OfferStateSnapshot

_WISHLIST_YAML = """\
entries:
  - manufacturer: Corsair
    model: Vengeance LPX 16GB
    ref: CMK16GX4M2D3000C16
    type: ram
    keywords:
      - corsair vengeance lpx
    max_price_solo: 80.00
    confidence_threshold: medium
    offer:
      enabled: false
      target_total_eur: null

  - manufacturer: Western Digital
    model: WD Red Plus 4TB
    ref: WD40EFPX
    type: hdd
    keywords:
      - wd red plus 4tb
    max_price_solo: 70.00
    confidence_threshold: medium
    offer:
      enabled: true
      target_total_eur: 60.00
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    wishlist_path = tmp_path / "wishlist.yaml"
    wishlist_path.write_text(_WISHLIST_YAML, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    connection = open_connection(db_path_under(data_dir))
    try:
        MigrationRunner().run(connection)
    finally:
        connection.close()
    yield wishlist_path, data_dir


def _offer_enabled(wishlist_path: Path, ref: str) -> bool:
    wishlist = load_wishlist(wishlist_path)
    return next(e.offer.enabled for e in wishlist.entries if e.ref == ref)


def _engage_lockout(data_dir: Path) -> None:
    async def _do() -> None:
        writer = OfferAuditWriter(db_path_under(data_dir))
        try:
            await writer.increment_failure_counter()
            await writer.set_global_disable("offer_lockout_threshold")
        finally:
            await writer.close()

    asyncio.run(_do())


def _read_state(data_dir: Path) -> OfferStateSnapshot:
    async def _do() -> OfferStateSnapshot:
        writer = OfferAuditWriter(db_path_under(data_dir))
        try:
            return await writer.read_state()
        finally:
            await writer.close()

    return asyncio.run(_do())


# ─────────────────────────────────────────────────────────────────────────
# offer enable
# ─────────────────────────────────────────────────────────────────────────


def test_enable_flips_the_flag_and_clears_lockout(
    workspace: tuple[Path, Path],
) -> None:
    wishlist_path, data_dir = workspace
    _engage_lockout(data_dir)

    exit_code = offer_cmd.run_enable(
        query="CMK16GX4M2D3000C16", wishlist_path=wishlist_path, data_dir=data_dir
    )

    assert exit_code == 0
    assert _offer_enabled(wishlist_path, "CMK16GX4M2D3000C16") is True
    state = _read_state(data_dir)
    assert state.globally_disabled is False
    assert state.consecutive_failures == 0


def test_enable_with_target_persists_it(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace

    exit_code = offer_cmd.run_enable(
        query="CMK16GX4M2D3000C16",
        wishlist_path=wishlist_path,
        data_dir=data_dir,
        target_total_eur="70.00",
    )

    assert exit_code == 0
    wishlist = load_wishlist(wishlist_path)
    entry = next(e for e in wishlist.entries if e.ref == "CMK16GX4M2D3000C16")
    assert entry.offer.target_total_eur == Decimal("70.00")


def test_enable_with_garbage_target_is_usage_error(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_enable(
        query="CMK16GX4M2D3000C16",
        wishlist_path=wishlist_path,
        data_dir=data_dir,
        target_total_eur="mucho",
    )
    assert exit_code == 2
    assert _offer_enabled(wishlist_path, "CMK16GX4M2D3000C16") is False


def test_unknown_entry_exits_usage_error(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_enable(
        query="no-such-ref", wishlist_path=wishlist_path, data_dir=data_dir
    )
    assert exit_code == 2


# ─────────────────────────────────────────────────────────────────────────
# offer disable
# ─────────────────────────────────────────────────────────────────────────


def test_per_entry_disable_keeps_lockout_untouched(
    workspace: tuple[Path, Path],
) -> None:
    wishlist_path, data_dir = workspace
    _engage_lockout(data_dir)

    exit_code = offer_cmd.run_disable(
        query="WD40EFPX", all_entries=False, wishlist_path=wishlist_path, data_dir=data_dir
    )

    assert exit_code == 0
    assert _offer_enabled(wishlist_path, "WD40EFPX") is False
    # Per-entry disable never lifts (nor engages) the global lockout.
    assert _read_state(data_dir).globally_disabled is True


def test_disable_requires_entry_or_all(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_disable(
        query=None, all_entries=False, wishlist_path=wishlist_path, data_dir=data_dir
    )
    assert exit_code == 2


def test_disable_all_requires_tty(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_disable(
        query=None,
        all_entries=True,
        wishlist_path=wishlist_path,
        data_dir=data_dir,
        is_tty=lambda: False,
    )
    assert exit_code == 1
    assert _offer_enabled(wishlist_path, "WD40EFPX") is True


def test_disable_all_typing_count_disables_and_locks(
    workspace: tuple[Path, Path],
) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_disable(
        query=None,
        all_entries=True,
        wishlist_path=wishlist_path,
        data_dir=data_dir,
        is_tty=lambda: True,
        input_fn=lambda prompt: "1",  # one entry currently enabled
    )
    assert exit_code == 0
    assert _offer_enabled(wishlist_path, "WD40EFPX") is False
    state = _read_state(data_dir)
    assert state.globally_disabled is True
    assert state.disabled_reason == "operator_disable_all"


def test_disable_all_wrong_number_aborts(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_disable(
        query=None,
        all_entries=True,
        wishlist_path=wishlist_path,
        data_dir=data_dir,
        is_tty=lambda: True,
        input_fn=lambda prompt: "7",
    )
    assert exit_code == 1
    assert _offer_enabled(wishlist_path, "WD40EFPX") is True


# ─────────────────────────────────────────────────────────────────────────
# offer status
# ─────────────────────────────────────────────────────────────────────────


def test_status_json_emits_a_parseable_object(
    workspace: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_status(
        wishlist_path=wishlist_path, data_dir=data_dir, output_format="json"
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["globally_disabled"] is False
    assert payload["sent_last_24h"] == 0
    by_ref = {e["entry_key"][2]: e for e in payload["entries"]}
    assert by_ref["WD40EFPX"]["offer_enabled"] is True
    assert Decimal(by_ref["WD40EFPX"]["target_total_eur"]) == Decimal("60")
    assert by_ref["CMK16GX4M2D3000C16"]["offer_enabled"] is False


def test_status_human_shows_rows_and_footer(
    workspace: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_status(wishlist_path=wishlist_path, data_dir=data_dir)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "WD Red Plus 4TB" in out
    assert "Sent last 24h: 0/5" in out


def test_status_unknown_format_is_usage_error(workspace: tuple[Path, Path]) -> None:
    wishlist_path, data_dir = workspace
    exit_code = offer_cmd.run_status(
        wishlist_path=wishlist_path, data_dir=data_dir, output_format="xml"
    )
    assert exit_code == 2
