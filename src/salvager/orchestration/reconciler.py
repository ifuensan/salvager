"""Phase 2 reconciliation orchestrator — Story 5.4 (FR31 / FR32).

Two gates sit either side of the autonomous checkout:

  - :meth:`Reconciler.reconcile_cross_source` runs BEFORE the buy. It
    re-fetches the listing via the marketplace's alternate path
    (Wallapop API ↔ TinyFish) and compares the two parsed prices. The
    Q9 silent-failure scenario (malformed HTML, comma-vs-dot drift)
    surfaces here as ``passed=False`` — the buy orchestrator aborts.
  - :meth:`Reconciler.reconcile_receipt_vs_alert` runs AFTER the buy.
    It compares the alert-time price to the receipt's
    ``price_paid_eur``. A mismatch can't roll back the purchase, but
    it can (and does, per FR32) auto-disable Phase 2 globally so the
    next listing can't be charged on the same drifted parser.

The math itself lives in pure :mod:`salvager.domain.reconciliation`;
this module just wires it to the cross-source fetcher and the
transaction record.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from salvager.domain.pricing import buyer_total_eur
from salvager.domain.reconciliation import (
    ReconciliationResult,
    compute_tolerance,
)

if TYPE_CHECKING:
    from salvager.domain.alert import AlertSnapshot
    from salvager.domain.listing import Listing
    from salvager.domain.phase2_audit import TransactionRecord
    from salvager.interfaces.page_fetcher import PageFetcher


@dataclass(frozen=True)
class CrossSourceOutcome:
    """The Reconciler's view of the cross-source check.

    Carries both raw prices so the buy orchestrator can render them
    into the ``BuyFailure`` ctx and the operator's audit row.
    """

    result: ReconciliationResult
    primary_price_eur: Decimal
    cross_source_price_eur: Decimal


@dataclass
class Reconciler:
    """Wires the pure reconciliation math to the cross-source fetcher."""

    cross_source_fetcher: PageFetcher
    tolerance_eur: Decimal
    tolerance_pct: Decimal
    #: Buffer for a listing's shipping when the marketplace didn't expose it,
    #: so the expected receipt total is computed like-for-like against the
    #: charged total (shipping-aware-pricing). Composer wires
    #: ``config.pricing.assumed_shipping_eur``.
    assumed_shipping_eur: Decimal

    async def reconcile_cross_source(self, listing: Listing) -> CrossSourceOutcome:
        """Re-fetch ``listing`` via the alternate path and compare prices.

        Any exception from the alternate fetch propagates: the buy
        orchestrator catches it and aborts the purchase with a
        marketplace-error variant. Reconciliation is fail-closed by
        design — if we can't verify the price, we don't pay.
        """
        cross = await self.cross_source_fetcher.fetch(listing.url)
        result = compute_tolerance(
            listing.price_eur,
            cross.price_eur,
            tolerance_eur=self.tolerance_eur,
            tolerance_pct=self.tolerance_pct,
        )
        return CrossSourceOutcome(
            result=result,
            primary_price_eur=listing.price_eur,
            cross_source_price_eur=cross.price_eur,
        )

    def reconcile_receipt_vs_alert(
        self,
        alert_snapshot: AlertSnapshot,
        transaction: TransactionRecord,
    ) -> ReconciliationResult:
        """Compare the expected buyer total to the receipt total.

        The marketplace charges the *delivered* total (item + shipping +
        any Protección fee), so we compare it against the expected buyer
        total — not the bare item price — or shipping/fees that were always
        part of the cost would spuriously trip the safety check
        (shipping-aware-pricing). The item-price delta stays available to
        the caller via ``alert_snapshot.listing.price_eur`` for the audit ctx.

        Synchronous: both values are already in hand by the time the
        buy orchestrator calls this, and there is no IO to do.
        """
        expected_total = buyer_total_eur(
            alert_snapshot.listing, assumed_shipping_eur=self.assumed_shipping_eur
        )
        return compute_tolerance(
            expected_total,
            transaction.price_paid_eur,
            tolerance_eur=self.tolerance_eur,
            tolerance_pct=self.tolerance_pct,
        )


__all__ = ["CrossSourceOutcome", "Reconciler"]
