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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from salvager.domain.alert import (
    AlertSnapshot,
    Phase,
    render_negotiable_listing_alert,
    render_phase1_listing_alert,
    render_phase2_listing_alert,
)
from salvager.domain.alert_watch import AlertWatch
from salvager.domain.comps import CompSummary, summarize_comps
from salvager.domain.evaluation import ListingEvaluation
from salvager.domain.listing import Listing, Marketplace, SearchQuery
from salvager.domain.pricing import (
    DEFAULT_ASSUMED_IMPORT_CHARGES_EUR,
    DEFAULT_ASSUMED_SHIPPING_EUR,
    buyer_cost,
    buyer_total_eur,
    offer_item_price_eur,
)
from salvager.domain.wishlist import Wishlist, WishlistEntry
from salvager.interfaces.listing_evaluator import ListingEvaluator
from salvager.interfaces.page_fetcher import PageFetcher
from salvager.interfaces.store import Store
from salvager.interfaces.telegram_surface import TelegramSurface
from salvager.observability.logging import get_logger
from salvager.orchestration.alert_updater import (
    DEFAULT_ALERT_UPDATE_POLICY,
    AlertUpdatePolicy,
    process_entry_watches,
)
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
    alerts_policy: AlertUpdatePolicy = DEFAULT_ALERT_UPDATE_POLICY,
    max_concurrent_evaluations: int = DEFAULT_MAX_CONCURRENT_EVALUATIONS,
    clock: Callable[[], datetime] = _utc_now,
    new_alert_id: Callable[[], UUID] = uuid_module.uuid4,
    offer_band_pct: Decimal | None = None,
    has_offered: Callable[[str, str], Awaitable[bool]] | None = None,
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
    # Shipping + import-charge buffers for the buyer-total gate + alert
    # breakdown. The preflight carries the config values (composer wires
    # them); Phase 1-only daemons fall back to the documented defaults
    # (shipping-aware-pricing, ebay-import-charges-pricing).
    shipping_buffer = (
        phase2_preflight.assumed_shipping_eur
        if phase2_preflight is not None
        else DEFAULT_ASSUMED_SHIPPING_EUR
    )
    import_buffer = (
        phase2_preflight.assumed_import_charges_eur
        if phase2_preflight is not None
        else DEFAULT_ASSUMED_IMPORT_CHARGES_EUR
    )

    # Lazy watch pruning: one cheap DELETE per cycle keeps the per-entry
    # diff join bounded (edit-alerts-on-state-change). Best-effort.
    try:
        await store.prune_expired_watches(now=now)
    except Exception as exc:
        log.warning(
            "alert_watch_prune_failed",
            extra={"marketplace": marketplace, "error_class": exc.__class__.__name__},
        )

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

        # Live alert updates: diff already-alerted listings against their
        # watches BEFORE the dedup filter throws them away — the state
        # signal is free in this cycle's fetch (edit-alerts-on-state-change).
        # Best-effort by contract: never raises, never blocks the pipeline.
        await process_entry_watches(
            entry,
            listings_by_id,
            marketplace=marketplace,
            store=store,
            telegram=telegram,
            policy=alerts_policy,
            assumed_shipping_eur=shipping_buffer,
            assumed_import_charges_eur=import_buffer,
            now=now,
            log=log,
            has_offered=has_offered,
        )

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
        # In-cycle comp signal: the reserved prices observed for THIS entry
        # this cycle. None when no reserved listing showed up — the renderer
        # then omits the comp row. Entry-level, so every buyable alert for
        # this entry shares the same summary.
        comp_summary = summarize_comps(r.price_eur for r in reserved)
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

        # Authoritative ceiling gate on the delivered buyer total (item +
        # shipping + Wallapop Protección + non-EU import charge), applied
        # before the LLM eval so an over-ceiling listing never costs an
        # evaluation or reaches the alert path (shipping-aware-pricing,
        # ebay-import-charges-pricing).
        buyable, negotiable = await _filter_over_ceiling(
            buyable,
            entry,
            store,
            summary,
            assumed_shipping_eur=shipping_buffer,
            assumed_import_charges_eur=import_buffer,
            marketplace=marketplace,
            log=log,
            offer_band_pct=offer_band_pct,
        )

        if not buyable and not negotiable:
            continue

        # Negotiable-band listings ride the same evaluation + confidence
        # gate as ordinary candidates (junk in the band must not alert just
        # because it is cheap-ish); the tag only changes the renderer.
        negotiable_ids = {listing.listing_id for listing in negotiable}
        evaluations = await _evaluate_concurrently(
            buyable + negotiable, entry, evaluator, semaphore, log
        )

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
                    comp_summary=comp_summary,
                    alerts_policy=alerts_policy,
                    new_alert_id=new_alert_id,
                    clock=clock,
                    log=log,
                    negotiable=listing.listing_id in negotiable_ids,
                    has_offered=has_offered,
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
    max_price = _entry_ceiling(entry)
    return [
        SearchQuery(keyword=kw, marketplace=marketplace, max_price_eur=max_price) for kw in keywords
    ]


def _entry_ceiling(entry: WishlistEntry) -> Decimal | None:
    """The entry's price ceiling — solo price preferred, else in-device.

    Used both for the item-level search pre-filter and the authoritative
    post-fetch buyer-total gate (shipping-aware-pricing).
    """
    return entry.max_price_solo or entry.max_price_in_device


async def _filter_over_ceiling(
    buyable: list[Listing],
    entry: WishlistEntry,
    store: Store,
    summary: PollCycleSummary,
    *,
    assumed_shipping_eur: Decimal,
    assumed_import_charges_eur: Decimal,
    marketplace: Marketplace,
    log: object,
    offer_band_pct: Decimal | None = None,
) -> tuple[list[Listing], list[Listing]]:
    """Split listings into ``(within_ceiling, negotiable)``; drop the rest.

    The search pre-filter caps the *item* price; this is the authoritative
    gate on the total the buyer actually pays (item + shipping + Wallapop
    Protección — shipping-aware-pricing). A listing at/under the item
    ceiling but over it once shipping/fees are added is dropped here, before
    the LLM eval, and recorded as seen + counted as dropped — mirroring the
    below-threshold drop path.

    Single carve-out (wallapop-offer-flow): a Wallapop listing on an
    offer-enabled entry whose buyer total is over the ceiling but within
    ``ceiling x (1 + offer_band_pct)`` — AND for which a valid offer amount
    exists (the platform's -30 % floor can rule one out) — is kept in the
    ``negotiable`` bucket instead of dropped. eBay and offer-disabled
    entries never populate it.
    """
    ceiling = _entry_ceiling(entry)
    if ceiling is None:
        return buyable, []
    within: list[Listing] = []
    negotiable: list[Listing] = []
    for listing in buyable:
        total = buyer_total_eur(
            listing,
            assumed_shipping_eur=assumed_shipping_eur,
            assumed_import_charges_eur=assumed_import_charges_eur,
        )
        if total <= ceiling:
            within.append(listing)
            continue
        if (
            offer_band_pct is not None
            and entry.offer.enabled
            and listing.marketplace == "wallapop"
            and not listing.is_refurbished
            and total <= ceiling * (1 + offer_band_pct)
            and offer_item_price_eur(
                listing,
                target_total_eur=entry.offer.target_total_eur or ceiling,
                assumed_shipping_eur=assumed_shipping_eur,
            )
            is not None
        ):
            negotiable.append(listing)
            log.info(  # type: ignore[attr-defined]
                "listing_kept_negotiable_band",
                extra={
                    "marketplace": marketplace,
                    "entry_display_name": entry.display_name,
                    "listing_id": listing.listing_id,
                    "buyer_total_eur": str(total),
                    "ceiling_eur": str(ceiling),
                },
            )
            continue
        summary.dropped_count += 1
        log.info(  # type: ignore[attr-defined]
            "listing_dropped_over_ceiling",
            extra={
                "marketplace": marketplace,
                "entry_display_name": entry.display_name,
                "listing_id": listing.listing_id,
                "item_price_eur": str(listing.price_eur),
                "buyer_total_eur": str(total),
                "ceiling_eur": str(ceiling),
            },
        )
        try:
            await store.record_seen(listing, entry.entry_key)
        except Exception as exc:
            summary.errors += 1
            log.exception(  # type: ignore[attr-defined]
                "poll_record_seen_failed",
                extra={
                    "listing_id": listing.listing_id,
                    "error_class": exc.__class__.__name__,
                },
            )
    return within, negotiable


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
                        # The message, not just the class — a class-only log
                        # made a deterministic per-listing failure take days
                        # to diagnose.
                        "error": str(exc)[:200],
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
    comp_summary: CompSummary | None,
    alerts_policy: AlertUpdatePolicy = DEFAULT_ALERT_UPDATE_POLICY,
    new_alert_id: Callable[[], UUID],
    clock: Callable[[], datetime],
    log: object,
    negotiable: bool = False,
    has_offered: Callable[[str, str], Awaitable[bool]] | None = None,
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
    phase: Phase
    phase2_max_price_eur: Decimal | None
    if negotiable:
        # Over ceiling by definition — the Phase 2 preflight would reject
        # it; the negotiable surface never carries a Comprar row.
        phase, phase2_max_price_eur = "negotiable", None
    else:
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

    # Buyer-total breakdown for the alert (item + shipping + Wallapop fee +
    # non-EU import charge). The buffers come from the preflight (composer
    # sets them from config); fall back to the defaults for Phase 1-only
    # daemons.
    buffer = (
        phase2_preflight.assumed_shipping_eur
        if phase2_preflight is not None
        else DEFAULT_ASSUMED_SHIPPING_EUR
    )
    import_buffer = (
        phase2_preflight.assumed_import_charges_eur
        if phase2_preflight is not None
        else DEFAULT_ASSUMED_IMPORT_CHARGES_EUR
    )
    cost = buyer_cost(
        listing,
        assumed_shipping_eur=buffer,
        assumed_import_charges_eur=import_buffer,
    )

    # Offer surface (wallapop-offer-flow): the computed amount + target,
    # rendered when the entry opted in, an amount exists (only possible when
    # the target sits below the delivered total — routine on the negotiable
    # band, and on under-ceiling alerts only with offer.target_total_eur),
    # and no successful offer was already sent for the listing.
    offer_eur: Decimal | None = None
    offer_target: Decimal | None = None
    if entry.offer.enabled and listing.marketplace == "wallapop" and not listing.is_refurbished:
        ceiling = _entry_ceiling(entry)
        if ceiling is not None:
            target = entry.offer.target_total_eur or ceiling
            amount = offer_item_price_eur(
                listing, target_total_eur=target, assumed_shipping_eur=buffer
            )
            if amount is not None:
                already = (
                    await has_offered(listing.marketplace, listing.listing_id)
                    if has_offered is not None
                    else False
                )
                if not already:
                    offer_eur, offer_target = amount, target

    if phase == "negotiable" and (offer_eur is None or offer_target is None):
        # The band filter only tags listings with a computable offer, but the
        # dedupe can still void it (an offer already went out). An
        # over-ceiling alert without an offer surface is noise — skip it.
        log.info(  # type: ignore[attr-defined]
            "negotiable_alert_skipped_no_offer",
            extra={"listing_id": listing.listing_id, "entry_display_name": entry.display_name},
        )
        return False

    try:
        if phase == "negotiable" and offer_eur is not None and offer_target is not None:
            rendered = render_negotiable_listing_alert(
                snapshot,
                offer_eur=offer_eur,
                offer_target_total_eur=offer_target,
                comp_summary=comp_summary,
                buyer_cost=cost,
            )
        elif phase == "phase2" and phase2_max_price_eur is not None:
            rendered = render_phase2_listing_alert(
                snapshot,
                phase2_max_price_eur,
                comp_summary=comp_summary,
                buyer_cost=cost,
                offer_eur=offer_eur,
                offer_target_total_eur=offer_target,
            )
        else:
            rendered = render_phase1_listing_alert(
                snapshot,
                comp_summary=comp_summary,
                buyer_cost=cost,
                offer_eur=offer_eur,
                offer_target_total_eur=offer_target,
            )
        message_id = await telegram.send(rendered)
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

    # The send returns before the insert, so the snapshot row is born
    # complete — append-only preserved (edit-alerts-on-state-change).
    snapshot = snapshot.model_copy(update={"telegram_message_id": message_id})

    try:
        await store.record_alert_snapshot(snapshot)
        await store.record_seen(listing, entry.entry_key, match_fired=True)
        await store.create_watch(
            AlertWatch(
                alert_id=snapshot.alert_id,
                listing_id=listing.listing_id,
                marketplace=listing.marketplace,
                entry_key=entry.entry_key,
                telegram_message_id=message_id,
                last_price_eur=listing.price_eur,
                last_is_reserved=listing.is_reserved,
                watch_until=snapshot.rendered_at + timedelta(days=alerts_policy.watch_days),
            )
        )
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
