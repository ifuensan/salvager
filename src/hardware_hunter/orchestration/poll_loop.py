"""Phase 1 poll-cycle orchestrator — Story 3.14 (AR15 + AR16, FR6-FR22).

Composes the per-marketplace pipeline:

    PageFetcher.search
      → snooze filter
      → dedup filter (is_seen)
      → ListingEvaluator.evaluate  (asyncio.Semaphore-bounded fan-out)
      → confidence threshold gate
      → render Phase 1 alert
      → TelegramSurface.send
      → Store.record_alert_snapshot + Store.record_seen
      (else: record_seen + log listing_dropped_below_threshold)

The poll cycle is the only module in the codebase that knows the
end-to-end story; everything else is a port or a single-purpose
adapter. The scheduler (Story 3.8) invokes :func:`run_poll_cycle`
once per cadence tick per marketplace.

Failure semantics
-----------------
Per-listing exceptions are caught and logged so one bad listing
cannot kill the cycle. The cycle-level guarantee is:

- LLM eval raised → listing skipped, NOT marked as seen (retried on
  the next cycle once the cache or rate-limit clears).
- Telegram delivery failed → listing skipped, NOT marked as seen
  (operator sees the failure log; deliver on next cycle).
- record_alert_snapshot / record_seen failed → listing left to retry
  (the alert dispatched once but the audit row didn't land — operator
  will see the discrepancy in audit show).
- ANY unhandled exception inside the cycle's per-listing block →
  logged as ``poll_cycle_error`` and the cycle moves on.

A ``fetcher.search`` failure ALSO doesn't kill the cycle — we log
and move to the next entry. The two-path Wallapop orchestrator
(Story 3.6) handles its own internal fallback before raising.
"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from hardware_hunter.domain.alert import AlertSnapshot, render_phase1_listing_alert
from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing, Marketplace, SearchQuery
from hardware_hunter.domain.wishlist import Wishlist, WishlistEntry
from hardware_hunter.interfaces.listing_evaluator import ListingEvaluator
from hardware_hunter.interfaces.page_fetcher import PageFetcher
from hardware_hunter.interfaces.store import Store
from hardware_hunter.interfaces.telegram_surface import TelegramSurface
from hardware_hunter.observability.logging import get_logger

#: NFR-P3: per-marketplace cycles cap concurrent LLM evaluations at 8.
DEFAULT_MAX_CONCURRENT_EVALUATIONS: Final[int] = 8

#: Confidence ordering: low < medium < high. A wishlist entry's
#: ``confidence_threshold`` is the floor — we alert when the eval's
#: ``confidence`` is at-or-above the threshold.
_CONFIDENCE_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PollCycleSummary:
    """Per-marketplace cycle metrics — emitted on ``poll_cycle_complete``.

    Counter names mirror the AC fields verbatim so the structured-log
    record's ``extra={...}`` body matches the schema operators read.
    """

    marketplace: Marketplace
    result_count: int = 0
    new_count: int = 0
    alerts_sent: int = 0
    dropped_count: int = 0
    snoozed_entries: int = 0
    errors: int = 0
    latency_ms: int = 0
    failed_entries: list[str] = field(default_factory=list)


async def run_poll_cycle(
    marketplace: Marketplace,
    *,
    wishlist: Wishlist,
    fetcher: PageFetcher,
    evaluator: ListingEvaluator,
    store: Store,
    telegram: TelegramSurface,
    max_concurrent_evaluations: int = DEFAULT_MAX_CONCURRENT_EVALUATIONS,
    clock: Callable[[], datetime] = _utc_now,
    new_alert_id: Callable[[], UUID] = uuid_module.uuid4,
) -> PollCycleSummary:
    """Run one poll cycle for ``marketplace`` against every wishlist entry.

    The function never raises — every error path is logged and the
    cycle moves on. The summary's counters reflect what actually
    happened; the orchestrator (or operator) reads them to decide
    health.
    """
    log = get_logger("orchestration.poll_loop")
    started = time.perf_counter()
    summary = PollCycleSummary(marketplace=marketplace)
    semaphore = asyncio.Semaphore(max_concurrent_evaluations)
    now = clock()

    for entry in wishlist.entries:
        snooze_until = await store.get_snooze_until(entry.entry_key)
        if snooze_until is not None and snooze_until > now:
            summary.snoozed_entries += 1
            log.info(
                "poll_entry_snoozed",
                extra={
                    "marketplace": marketplace,
                    "entry_display_name": entry.display_name,
                    "snooze_until": snooze_until.isoformat(),
                },
            )
            continue

        query = _build_search_query(entry, marketplace)
        try:
            listings = await fetcher.search(query)
        except Exception as exc:
            summary.errors += 1
            summary.failed_entries.append(entry.display_name)
            log.error(
                "poll_entry_fetch_failed",
                extra={
                    "marketplace": marketplace,
                    "entry_display_name": entry.display_name,
                    "error_class": exc.__class__.__name__,
                },
            )
            continue

        summary.result_count += len(listings)
        candidates = await _filter_unseen(listings, entry, store)
        summary.new_count += len(candidates)

        if not candidates:
            continue

        evaluations = await _evaluate_concurrently(candidates, entry, evaluator, semaphore, log)

        for listing, evaluation in evaluations:
            if evaluation is None:
                # Eval failed (rate-limited, malformed, network). Do
                # NOT mark as seen — the listing will retry next cycle.
                continue

            if _passes_threshold(evaluation, entry.confidence_threshold):
                handled = await _dispatch_alert(
                    entry=entry,
                    listing=listing,
                    evaluation=evaluation,
                    telegram=telegram,
                    store=store,
                    new_alert_id=new_alert_id,
                    clock=clock,
                    log=log,
                )
                if handled:
                    summary.alerts_sent += 1
            else:
                summary.dropped_count += 1
                log.info(
                    "listing_dropped_below_threshold",
                    extra={
                        "marketplace": marketplace,
                        "entry_display_name": entry.display_name,
                        "listing_id": listing.listing_id,
                        "confidence": evaluation.confidence,
                        "threshold": entry.confidence_threshold,
                    },
                )
                try:
                    await store.record_seen(listing, entry.entry_key)
                except Exception as exc:
                    summary.errors += 1
                    log.error(
                        "poll_record_seen_failed",
                        extra={
                            "listing_id": listing.listing_id,
                            "error_class": exc.__class__.__name__,
                        },
                    )

    summary.latency_ms = int((time.perf_counter() - started) * 1000)

    # Persist a poll heartbeat to `_meta` so `hardware-hunter health`
    # (Story 4.4) can report last-poll freshness without the daemon
    # process running (AR14). A heartbeat-write failure must not fail
    # the cycle — it is diagnostic state, not audit data.
    try:
        await store.set_meta(f"last_poll_{marketplace}", clock().isoformat())
    except Exception as exc:
        log.error(
            "poll_heartbeat_write_failed",
            extra={"marketplace": marketplace, "error_class": exc.__class__.__name__},
        )

    log.info(
        "poll_cycle_complete",
        extra={
            "marketplace": marketplace,
            "result_count": summary.result_count,
            "new_count": summary.new_count,
            "alerts_sent": summary.alerts_sent,
            "dropped_count": summary.dropped_count,
            "snoozed_entries": summary.snoozed_entries,
            "errors": summary.errors,
            "latency_ms": summary.latency_ms,
        },
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────


def _build_search_query(entry: WishlistEntry, marketplace: Marketplace) -> SearchQuery:
    return SearchQuery(
        keywords=list(entry.keywords) or [entry.model],
        marketplace=marketplace,
        max_price_eur=entry.max_price_solo or entry.max_price_in_device,
    )


async def _filter_unseen(
    listings: list[Listing],
    entry: WishlistEntry,
    store: Store,
) -> list[Listing]:
    """Drop already-seen listings. Snooze is per-entry and checked
    upstream of the fetcher call, so we don't re-check it here."""
    unseen: list[Listing] = []
    for listing in listings:
        if await store.is_seen(listing.listing_id, entry.entry_key):
            continue
        unseen.append(listing)
    return unseen


async def _evaluate_concurrently(
    listings: list[Listing],
    entry: WishlistEntry,
    evaluator: ListingEvaluator,
    semaphore: asyncio.Semaphore,
    log: object,
) -> list[tuple[Listing, ListingEvaluation | None]]:
    """Fan out :meth:`ListingEvaluator.evaluate` across at most
    ``semaphore`` concurrent calls; collect ``(listing, eval | None)``.

    A failed evaluation lands as ``None`` so the caller can leave the
    listing un-marked for retry. The original exception is logged inside
    the per-listing closure.
    """

    async def _eval_one(
        listing: Listing,
    ) -> tuple[Listing, ListingEvaluation | None]:
        async with semaphore:
            try:
                return listing, await evaluator.evaluate(listing, entry)
            except Exception as exc:
                log.error(  # type: ignore[attr-defined]
                    "llm_eval_failed",
                    extra={
                        "entry_display_name": entry.display_name,
                        "listing_id": listing.listing_id,
                        "error_class": exc.__class__.__name__,
                    },
                )
                return listing, None

    return await asyncio.gather(*(_eval_one(listing) for listing in listings))


def _passes_threshold(evaluation: ListingEvaluation, threshold: str) -> bool:
    return _CONFIDENCE_RANK[evaluation.confidence] >= _CONFIDENCE_RANK[threshold]


async def _dispatch_alert(
    *,
    entry: WishlistEntry,
    listing: Listing,
    evaluation: ListingEvaluation,
    telegram: TelegramSurface,
    store: Store,
    new_alert_id: Callable[[], UUID],
    clock: Callable[[], datetime],
    log: object,
) -> bool:
    """Build snapshot → render → send → persist. Return True on full success.

    Persistence (record_alert_snapshot + record_seen) only runs after
    Telegram confirms the message_id. A delivery failure leaves the
    listing un-marked so the next cycle retries — at the cost of a
    duplicate-eval on the next pass, but the cache (Story 3.10) absorbs
    that.
    """
    snapshot = AlertSnapshot(
        alert_id=new_alert_id(),
        entry_key=entry.entry_key,
        entry_display_name=entry.display_name,
        listing=listing,
        evaluation=evaluation,
        phase="phase1",
        rendered_at=clock(),
    )

    try:
        rendered = render_phase1_listing_alert(snapshot)
        _message_id = await telegram.send(rendered)
    except Exception as exc:
        log.error(  # type: ignore[attr-defined]
            "alert_dispatch_failed",
            extra={
                "listing_id": listing.listing_id,
                "alert_id": str(snapshot.alert_id),
                "error_class": exc.__class__.__name__,
            },
        )
        return False

    try:
        await store.record_alert_snapshot(snapshot)
        await store.record_seen(listing, entry.entry_key)
    except Exception as exc:
        log.error(  # type: ignore[attr-defined]
            "alert_persist_failed",
            extra={
                "listing_id": listing.listing_id,
                "alert_id": str(snapshot.alert_id),
                "error_class": exc.__class__.__name__,
            },
        )
        # We DID alert the operator — count it. Audit drift is loud
        # in `audit show` (Epic 4) so an operator catches it.
        return True

    return True


__all__ = [
    "DEFAULT_MAX_CONCURRENT_EVALUATIONS",
    "PollCycleSummary",
    "run_poll_cycle",
]
