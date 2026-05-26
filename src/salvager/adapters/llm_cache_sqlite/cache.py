"""LLM evaluation cache — Story 3.10 (FR16 + NFR-C3).

Key design choices
------------------
The cache is keyed by ``(listing_url, prompt_version)``. ``prompt_version``
is the locked sentinel in :data:`salvager.domain.prompts.PROMPT_VERSION`
— bumping it invalidates every cached entry on the next read without
any explicit migration step. The TTL is split: low-confidence evals
expire faster (default 1h) because they're more likely to flip into a
match on a re-evaluation; medium/high are stickier (default 24h).

Schema
------
Single table, single index, no migration runner — the cache is by
definition disposable, so we use ``CREATE TABLE IF NOT EXISTS`` on
open. If the schema ever evolves, bump the on-disk filename
(`llm_eval_cache_v2.db`) and let TTL drain the old file.

Concurrency
-----------
The cache is read by the per-listing evaluation fan-out (Story 3.14
``asyncio.Semaphore(8)``); writes happen on cache misses. We wrap
every DB call in ``asyncio.to_thread`` and serialize writes with an
internal :class:`asyncio.Lock` — same pattern as ``SqliteStore``.

Decorator pattern
-----------------
:class:`CachingListingEvaluator` implements :class:`ListingEvaluator`
by wrapping an inner evaluator and consulting the cache first. The
orchestrator (Story 3.14) constructs
``CachingListingEvaluator(real_evaluator, cache, PROMPT_VERSION)`` and
the rest of the pipeline only sees a :class:`ListingEvaluator` —
caching is transparent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from salvager.adapters.sqlite_store.connection import open_connection
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing
from salvager.domain.wishlist import WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.observability.logging import get_logger

#: Default file name under ``data_dir/``.
DEFAULT_CACHE_FILENAME: Final[str] = "llm_eval_cache.db"

#: Default TTL when confidence is ``medium`` or ``high``.
DEFAULT_TTL_HOURS: Final[int] = 24

#: Default TTL when confidence is ``low`` — shorter because a low-confidence
#: verdict is the one most likely to flip on a re-evaluation once the
#: listing description gets updated by the seller.
DEFAULT_TTL_HOURS_LOW_CONFIDENCE: Final[int] = 1


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS llm_evaluation_cache (
    listing_url       TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    evaluation_json   TEXT NOT NULL,
    prompt_text       TEXT NOT NULL,
    confidence        TEXT NOT NULL,
    cached_at         TEXT NOT NULL,
    PRIMARY KEY (listing_url, prompt_version)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_llm_eval_cache_cached_at
    ON llm_evaluation_cache (cached_at);
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SqliteLlmEvalCache:
    """Per-listing LLM-evaluation cache backed by a dedicated SQLite file.

    The cache exposes ``get`` and ``set``; it does not implement any
    :class:`ListingEvaluator` interface itself — composition with the
    real evaluator happens in :class:`CachingListingEvaluator`.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        ttl_normal: timedelta = timedelta(hours=DEFAULT_TTL_HOURS),
        ttl_low_confidence: timedelta = timedelta(hours=DEFAULT_TTL_HOURS_LOW_CONFIDENCE),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._db_path = Path(db_path)
        self._ttl_normal = ttl_normal
        self._ttl_low_confidence = ttl_low_confidence
        self._clock = clock
        self._db_lock = asyncio.Lock()
        self._connection = open_connection(self._db_path)
        # CREATE TABLE IF NOT EXISTS is idempotent — safe to run on every
        # construction. No migration runner because the cache is
        # disposable.
        self._connection.executescript(_SCHEMA_DDL)
        self._log = get_logger("adapter.llm_cache_sqlite")

    async def close(self) -> None:
        async with self._db_lock:
            await asyncio.to_thread(self._connection.close)

    # ─────────────────────────────────────────────────────────────────
    # get / set
    # ─────────────────────────────────────────────────────────────────

    async def get(
        self,
        listing_url: str,
        prompt_version: str,
    ) -> ListingEvaluation | None:
        """Return the cached :class:`ListingEvaluation` for this key, or None.

        Returns None when:
          - the key isn't present at all;
          - the cached entry has expired against its confidence-tier TTL.

        On a hit, emits ``llm_cache_hit`` with the entry's age in
        seconds; on a TTL-driven miss, emits ``llm_cache_expired``.
        The caller does NOT need to evict the expired row — the next
        ``set`` against the same key replaces it. Old, never-revisited
        URLs accumulate; an operator can wipe the file at any point.
        """

        def _read() -> tuple[str, str, datetime] | None:
            cursor = self._connection.execute(
                """
                SELECT evaluation_json, confidence, cached_at
                FROM llm_evaluation_cache
                WHERE listing_url = ? AND prompt_version = ?
                """,
                (listing_url, prompt_version),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return (
                row["evaluation_json"],
                row["confidence"],
                datetime.fromisoformat(row["cached_at"]),
            )

        # Hold _db_lock around the read because the poll loop's
        # per-listing semaphore spawns up to 8 evaluations in parallel.
        # All 8 call cache.get first, and asyncio.to_thread dispatches
        # to different worker threads. Python's sqlite3 with
        # check_same_thread=False permits cross-thread sharing of a
        # connection BUT requires the caller to serialize access —
        # concurrent execute() on the same Connection raises
        # InterfaceError ("Recursive use of cursors not allowed" /
        # "Cannot operate on a closed database"). The set() path was
        # already serialized; without locking get() too, 8-way parallel
        # reads race against each other and against any in-flight set.
        async with self._db_lock:
            row = await asyncio.to_thread(_read)
        if row is None:
            return None

        evaluation_json, confidence, cached_at = row
        now = self._clock()
        age = now - cached_at
        ttl = self._ttl_for_confidence(confidence)
        if age > ttl:
            self._log.info(
                "llm_cache_expired",
                extra={
                    "listing_url": listing_url,
                    "confidence": confidence,
                    "age_seconds": int(age.total_seconds()),
                    "ttl_seconds": int(ttl.total_seconds()),
                },
            )
            return None

        evaluation = ListingEvaluation.model_validate_json(evaluation_json)
        self._log.info(
            "llm_cache_hit",
            extra={
                "listing_url": listing_url,
                "confidence": confidence,
                "age_seconds": int(age.total_seconds()),
            },
        )
        return evaluation

    async def set(
        self,
        listing_url: str,
        prompt_version: str,
        prompt_text: str,
        evaluation: ListingEvaluation,
    ) -> None:
        """Store ``evaluation`` under ``(listing_url, prompt_version)``.

        ``prompt_text`` is persisted alongside so ``salvager
        explain`` (FR44) can replay the exact prompt the LLM saw —
        useful when an evaluator returns surprising verdicts and the
        operator wants to understand what the model was asked.

        Re-running ``set`` against an existing key REPLACES the row;
        ``cached_at`` resets to the current clock value.
        """
        now = self._clock()
        evaluation_json = evaluation.model_dump_json()

        def _write() -> None:
            self._connection.execute(
                """
                INSERT INTO llm_evaluation_cache (
                    listing_url, prompt_version, evaluation_json,
                    prompt_text, confidence, cached_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (listing_url, prompt_version)
                DO UPDATE SET
                    evaluation_json = excluded.evaluation_json,
                    prompt_text     = excluded.prompt_text,
                    confidence      = excluded.confidence,
                    cached_at       = excluded.cached_at
                """,
                (
                    listing_url,
                    prompt_version,
                    evaluation_json,
                    prompt_text,
                    evaluation.confidence,
                    now.isoformat(),
                ),
            )

        async with self._db_lock:
            await asyncio.to_thread(_write)

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _ttl_for_confidence(self, confidence: str) -> timedelta:
        return self._ttl_low_confidence if confidence == "low" else self._ttl_normal


# ─────────────────────────────────────────────────────────────────────────
# Decorator — composes any ListingEvaluator with a cache
# ─────────────────────────────────────────────────────────────────────────


class CachingListingEvaluator(ListingEvaluator):
    """A :class:`ListingEvaluator` that consults a cache before its inner.

    The decorator is the seam the orchestrator composes:

    .. code-block:: python

        gemini = GeminiFlashEvaluator(...)
        cache = SqliteLlmEvalCache(data_dir / "llm_eval_cache.db")
        evaluator = CachingListingEvaluator(gemini, cache, PROMPT_VERSION)

    On a hit, the cached evaluation is returned with ``cache_hit=True``
    so the alert renderer can decorate the alert (or audit can record
    the hit). On a miss, the inner evaluator runs and the result is
    persisted alongside the originating prompt.
    """

    def __init__(
        self,
        inner: ListingEvaluator,
        cache: SqliteLlmEvalCache,
        prompt_version: str,
        *,
        prompt_builder: Callable[[Listing, WishlistEntry], str] | None = None,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._prompt_version = prompt_version
        # Lazy default so domain.prompts isn't imported at module load
        # if the caller injects a different prompt builder (test seam).
        self._prompt_builder = prompt_builder
        self._log = get_logger("adapter.llm_cache_sqlite")

    async def evaluate(self, listing: Listing, entry: WishlistEntry) -> ListingEvaluation:
        cached = await self._cache.get(listing.url, self._prompt_version)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True})

        result = await self._inner.evaluate(listing, entry)
        prompt = self._build_prompt(listing, entry)
        await self._cache.set(listing.url, self._prompt_version, prompt, result)
        return result

    def _build_prompt(self, listing: Listing, entry: WishlistEntry) -> str:
        if self._prompt_builder is not None:
            return self._prompt_builder(listing, entry)
        # Lazy import: avoids pulling domain.prompts into the module
        # graph at test time when the caller substitutes a fake builder.
        from salvager.domain.prompts import build_evaluation_prompt

        return build_evaluation_prompt(listing, entry)


__all__ = [
    "DEFAULT_CACHE_FILENAME",
    "DEFAULT_TTL_HOURS",
    "DEFAULT_TTL_HOURS_LOW_CONFIDENCE",
    "CachingListingEvaluator",
    "SqliteLlmEvalCache",
]
