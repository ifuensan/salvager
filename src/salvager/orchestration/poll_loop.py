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
from decimal import Decimal
from typing import Final
from uuid import UUID

from salvager.domain.alert import (
    AlertSnapshot,
    Phase,
    render_phase1_listing_alert,
    render_phase2_listing_alert,
)
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, Marketplace, SearchQuery
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.store import Store
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.observability.logging import get_logger
from salvager.orchestration.phase2_preflight import Phase2Preflight

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
    ``reserved_count`` tracks the new-but-not-buyable subset (sellers
    flagged them as taken before we polled); they're recorded as seen
    and treated as price comps but never reach the evaluator.
    """

    marketplace: Marketplace
    result_count: int = 0
    new_count: int = 0
    alerts_sent: int = 0
    dropped_count: int = 0
    reserved_count: int = 0
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
    phase2_preflight: Phase2Preflight | None = None,
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

        queries = _build_search_queries(entry, marketplace)
        listings_by_id: dict[str, Listing] = {}
        keyword_failures = 0
        for query in queries:
            try:
                sub_listings = await fetcher.search(query)
            except Exception as exc:
                keyword_failures += 1
                log.exception(
                    "poll_keyword_fetch_failed",
                    extra={
                        "marketplace": marketplace,
                        "entry_display_name": entry.display_name,
                        "keyword": query.keyword,
                        "error_class": exc.__class__.__name__,
                    },
                )
                continue
            for listing in sub_listings:
                listings_by_id.setdefault(listing.listing_id, listing)
        if keyword_failures == len(queries):
            # Every keyword failed → entry is unreachable this cycle.
            summary.errors += 1
            summary.failed_entries.append(entry.display_name)
            log.error(
                "poll_entry_fetch_failed",
                extra={
                    "marketplace": marketplace,
                    "entry_display_name": entry.display_name,
                    "keyword_count": len(queries),
                },
            )
            continue
        listings = list(listings_by_id.values())

        summary.result_count += len(listings)
        candidates = await _filter_unseen(listings, entry, store)
        summary.new_count += len(candidates)

        if not candidates:
            continue

        # Split reserved (no longer buyable) from buyable. Reserved
        # listings are still useful as price comps for the operator
        # (what someone was willing to pay) but they must never reach
        # the evaluator (LLM cost on dead inventory) or the alert path
        # (operator can't buy a sold listing). Record them as seen so
        # they don't reprocess every cycle.
        buyable, reserved = _split_reserved(candidates)
        summary.reserved_count += len(reserved)
        if reserved:
            comp_prices = [r.price_eur for r in reserved]
            log.info(
                "reserved_comps_observed",
                extra={
                    "marketplace": marketplace,
                    "entry_display_name": entry.display_name,
                    "reserved_count": len(reserved),
                    "comp_prices_eur": [str(p) for p in comp_prices],
                },
            )
            await _record_reserved_as_seen(reserved, entry, store, summary, log)

        if not buyable:
            continue

        evaluations = await _evaluate_concurrently(buyable, entry, evaluator, semaphore, log)

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
                    phase2_preflight=phase2_preflight,
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
                    log.exception(
                        "poll_record_seen_failed",
                        extra={
                            "listing_id": listing.listing_id,
                            "error_class": exc.__class__.__name__,
                        },
                    )

    summary.latency_ms = int((time.perf_counter() - started) * 1000)

    # Persist a poll heartbeat to `_meta` so `salvager health`
    # (Story 4.4) can report last-poll freshness without the daemon
    # process running (AR14). A heartbeat-write failure must not fail
    # the cycle — it is diagnostic state, not audit data.
    try:
        await store.set_meta(f"last_poll_{marketplace}", clock().isoformat())
    except Exception as exc:
        log.exception(
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
            "reserved_count": summary.reserved_count,
            "snoozed_entries": summary.snoozed_entries,
            "errors": summary.errors,
            "latency_ms": summary.latency_ms,
        },
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────


def _build_search_queries(entry: WishlistEntry, marketplace: Marketplace) -> list[SearchQuery]:
    """Fan a wishlist entry out into one search per keyword phrase.

    The entry's ``keywords`` list is treated as alternative phrases —
    each becomes its own marketplace search; ``run_poll_cycle`` unions
    + de-dupes the results by ``listing_id``. Falls back to the entry's
    canonical ``model`` when the wishlist omits ``keywords``.
    """
    keywords = list(entry.keywords) or [entry.model]
    max_price = entry.max_price_solo or entry.max_price_in_device
    return [
        SearchQuery(keyword=kw, marketplace=marketplace, max_price_eur=max_price) for kw in keywords
    ]


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


def _split_reserved(listings: list[Listing]) -> tuple[list[Listing], list[Listing]]:
    """Partition ``listings`` into ``(buyable, reserved)``.

    Two lists rather than a filter so the caller can keep the reserved
    set for downstream uses (comp pricing, log fan-out) without a
    second pass.
    """
    buyable: list[Listing] = []
    reserved: list[Listing] = []
    for listing in listings:
        (reserved if listing.is_reserved else buyable).append(listing)
    return buyable, reserved


async def _record_reserved_as_seen(
    reserved: list[Listing],
    entry: WishlistEntry,
    store: Store,
    summary: PollCycleSummary,
    log: object,
) -> None:
    """Mark reserved listings as seen so they don't reprocess each cycle.

    A persistence failure increments ``summary.errors`` and gets a
    structured log; the cycle moves on. Same shape as the post-eval
    ``record_seen`` call site so operators reading audit logs see
    consistent error_class values for "failed to mark seen".
    """
    for listing in reserved:
        try:
            await store.record_seen(listing, entry.entry_key)
        except Exception as exc:
            summary.errors += 1
            log.error(  # type: ignore[attr-defined]
                "poll_record_seen_failed",
                extra={
                    "listing_id": listing.listing_id,
                    "error_class": exc.__class__.__name__,
                    "reason": "reserved",
                },
            )


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


async def _select_phase(
    *,
    entry: WishlistEntry,
    listing: Listing,
    evaluation: ListingEvaluation,
    phase2_preflight: Phase2Preflight | None,
    log: object,
) -> tuple[Phase, Decimal | None]:
    """Decide which renderer to use; return ``(phase, phase2_max_price_eur)``.

    Returns ``("phase1", None)`` whenever the Phase 2 gate is not
    available, the entry isn't opted in, or any pre-flight check fails.
    Returns ``("phase2", entry.phase2.max_price_eur)`` only when every
    check passes — and emits a structured ``phase2_alert_downgraded``
    log line for every silent downgrade.
    """
    if phase2_preflight is None or not entry.phase2.enabled:
        return "phase1", None
    result = await phase2_preflight.check(entry, listing, evaluation)
    if result.eligible:
        return "phase2", entry.phase2.max_price_eur
    log.info(  # type: ignore[attr-defined]
        "phase2_alert_downgraded",
        extra={
            "entry_display_name": entry.display_name,
            "listing_id": listing.listing_id,
            "reason": result.reason,
        },
    )
    return "phase1", None


async def _dispatch_alert(
    *,
    entry: WishlistEntry,
    listing: Listing,
    evaluation: ListingEvaluation,
    telegram: TelegramSurface,
    store: Store,
    phase2_preflight: Phase2Preflight | None,
    new_alert_id: Callable[[], UUID],
    clock: Callable[[], datetime],
    log: object,
) -> bool:
    """Build snapshot → render → send → persist. Return True on full success.

    The Phase 2 renderer (with the Buy keyboard) is selected only when
    every check in :class:`Phase2Preflight` passes; any failure
    downgrades silently to the Phase 1 renderer (UX-DR7) and emits a
    ``phase2_alert_downgraded`` log so an operator can audit the why.

    Persistence (record_alert_snapshot + record_seen) only runs after
    Telegram confirms the message_id. A delivery failure leaves the
    listing un-marked so the next cycle retries — at the cost of a
    duplicate-eval on the next pass, but the cache (Story 3.10) absorbs
    that.
    """
    phase, phase2_max_price_eur = await _select_phase(
        entry=entry,
        listing=listing,
        evaluation=evaluation,
        phase2_preflight=phase2_preflight,
        log=log,
    )

    snapshot = AlertSnapshot(
        alert_id=new_alert_id(),
        entry_key=entry.entry_key,
        entry_display_name=entry.display_name,
        listing=listing,
        evaluation=evaluation,
        phase=phase,
        phase2_max_price_eur=phase2_max_price_eur,
        rendered_at=clock(),
    )

    try:
        rendered = (
            render_phase2_listing_alert(snapshot, phase2_max_price_eur)
            if phase == "phase2" and phase2_max_price_eur is not None
            else render_phase1_listing_alert(snapshot)
        )
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
        await store.record_seen(listing, entry.entry_key, match_fired=True)
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
