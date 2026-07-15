"""Tests for the adapter-port ABCs in ``interfaces/`` — Story 3.2.

These tests verify the *contract*, not behavior: that every ABC is
properly abstract (can't be instantiated naked), that the declared
signatures match the AC, and that the package stays pure (no SDK
imports outside stdlib + pydantic + domain).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from salvager.interfaces.listing_evaluator import (
    ListingEvaluator,
    ListingEvaluatorError,
)
from salvager.interfaces.page_fetcher import PageFetcher, PageFetcherError
from salvager.interfaces.scheduler import Scheduler, SchedulerError, SchedulerTask
from salvager.interfaces.store import EntryKey, Store, StoreError
from salvager.interfaces.telegram_surface import (
    CallbackHandler,
    TelegramSurface,
    TelegramSurfaceError,
)

# ─────────────────────────────────────────────────────────────────────────
# All ABCs reject naked instantiation
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "abc_cls",
    [PageFetcher, ListingEvaluator, Scheduler, TelegramSurface, Store],
)
def test_abc_cannot_be_instantiated_directly(abc_cls: type) -> None:
    """The ABCs declare abstract methods; direct instantiation is a
    programming error."""
    with pytest.raises(TypeError, match="abstract"):
        abc_cls()


# ─────────────────────────────────────────────────────────────────────────
# PageFetcher contract
# ─────────────────────────────────────────────────────────────────────────


def test_page_fetcher_declares_search_and_fetch() -> None:
    assert "search" in PageFetcher.__abstractmethods__
    assert "fetch" in PageFetcher.__abstractmethods__
    assert inspect.iscoroutinefunction(PageFetcher.search)
    assert inspect.iscoroutinefunction(PageFetcher.fetch)


def test_page_fetcher_error_is_runtime_error() -> None:
    assert issubclass(PageFetcherError, RuntimeError)


# ─────────────────────────────────────────────────────────────────────────
# ListingEvaluator contract
# ─────────────────────────────────────────────────────────────────────────


def test_listing_evaluator_declares_evaluate() -> None:
    assert "evaluate" in ListingEvaluator.__abstractmethods__
    assert inspect.iscoroutinefunction(ListingEvaluator.evaluate)


def test_listing_evaluator_error_is_runtime_error() -> None:
    assert issubclass(ListingEvaluatorError, RuntimeError)


# ─────────────────────────────────────────────────────────────────────────
# Scheduler contract
# ─────────────────────────────────────────────────────────────────────────


def test_scheduler_declares_register_and_shutdown() -> None:
    assert "register" in Scheduler.__abstractmethods__
    assert "shutdown" in Scheduler.__abstractmethods__
    assert inspect.iscoroutinefunction(Scheduler.register)
    assert inspect.iscoroutinefunction(Scheduler.shutdown)


def test_scheduler_task_alias_exposed() -> None:
    """``SchedulerTask`` is the public type alias for the registered callable."""
    assert SchedulerTask is not None


# ─────────────────────────────────────────────────────────────────────────
# TelegramSurface contract
# ─────────────────────────────────────────────────────────────────────────


def test_telegram_surface_declares_four_methods() -> None:
    methods = TelegramSurface.__abstractmethods__
    assert methods == {"send", "edit_alert", "edit_keyboard", "listen_callbacks"}
    assert inspect.iscoroutinefunction(TelegramSurface.send)
    assert inspect.iscoroutinefunction(TelegramSurface.edit_alert)
    assert inspect.iscoroutinefunction(TelegramSurface.edit_keyboard)
    assert inspect.iscoroutinefunction(TelegramSurface.listen_callbacks)


def test_callback_handler_alias_exposed() -> None:
    assert CallbackHandler is not None


# ─────────────────────────────────────────────────────────────────────────
# Store contract — Phase 1 methods + Phase 2 declared but no audit mutations
# ─────────────────────────────────────────────────────────────────────────


def test_store_phase1_methods_declared() -> None:
    abstract = Store.__abstractmethods__
    for method in (
        "is_seen",
        "record_seen",
        "get_snooze_until",
        "set_snooze",
        "record_alert_snapshot",
        "get_alert_snapshot",
        "get_alert_snapshot_by_alert_id",
        "record_callback",
    ):
        assert method in abstract, f"Store should declare {method!r}"


def test_store_phase2_methods_declared() -> None:
    """AR24: Phase 2 methods are declared so the ABC shape is complete;
    concrete v0.x implementations raise Phase2GuardrailTripped."""
    abstract = Store.__abstractmethods__
    assert "record_tap_event" in abstract
    assert "record_transaction" in abstract


def test_store_has_no_update_or_delete_methods_on_audit() -> None:
    """NFR-S4: append-only audit log is mechanical, not aspirational.
    Walking the ABC's attribute table is the enforcement mechanism."""
    forbidden_prefixes = ("update_", "delete_")
    audit_keywords = ("audit", "alert", "callback", "tap_event", "transaction")
    for name in dir(Store):
        if name.startswith("_"):
            continue
        if any(name.startswith(prefix) for prefix in forbidden_prefixes) and any(
            kw in name for kw in audit_keywords
        ):
            pytest.fail(f"Store declares forbidden mutator on audit data: {name}")


def test_entry_key_is_tuple_alias() -> None:
    """The alias normalises the (manufacturer, model, ref) tuple shape
    so Store implementations don't re-declare the type everywhere."""
    # EntryKey is a typing alias; comparing it directly to tuple[str,str,str]
    # depends on Python's typing-form equality which mypy distrusts. Use
    # the get_args/get_origin probe instead.
    from typing import get_args, get_origin

    assert get_origin(EntryKey) is tuple
    assert get_args(EntryKey) == (str, str, str)


def test_store_error_is_runtime_error() -> None:
    assert issubclass(StoreError, RuntimeError)
    assert issubclass(TelegramSurfaceError, RuntimeError)
    assert issubclass(SchedulerError, RuntimeError)


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline — interfaces/ stays pure (Story 3.2 AC)
# ─────────────────────────────────────────────────────────────────────────

_ALLOWED_INTERFACE_TOP_LEVELS = frozenset(
    {
        # stdlib
        "__future__",
        "abc",
        "collections",
        "datetime",
        "decimal",
        "typing",
        "uuid",
        # blessed third-party
        "pydantic",
        # in-package
        "salvager",
    }
)


@pytest.mark.parametrize(
    "module_path",
    [
        "src/salvager/interfaces/listing_evaluator.py",
        "src/salvager/interfaces/page_fetcher.py",
        "src/salvager/interfaces/scheduler.py",
        "src/salvager/interfaces/store.py",
        "src/salvager/interfaces/telegram_surface.py",
    ],
)
def test_interface_module_imports_only_whitelisted_packages(module_path: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    tree = ast.parse((repo_root / module_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in _ALLOWED_INTERFACE_TOP_LEVELS, (
                    f"{module_path}: forbidden import {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            top = (node.module or "").split(".")[0]
            assert top in _ALLOWED_INTERFACE_TOP_LEVELS, (
                f"{module_path}: forbidden 'from {node.module} import …'"
            )
