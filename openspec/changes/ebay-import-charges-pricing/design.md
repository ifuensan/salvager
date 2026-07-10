# ebay-import-charges-pricing — Design

## Context

v0.3.3's buyer-total pipeline (`domain/pricing.py::buyer_cost`) sums item + shipping (+ Protección on Wallapop) and is consumed by four gates: the eBay post-fetch ceiling filter (`adapters/ebay_api/fetcher.py:151`), the Phase 1 alert gate (`poll_loop._filter_over_ceiling`), the Phase 2 buy gate (`orchestration/phase2_preflight.py:92`), and the reconciler. eBay now charges a flat import fee (observed 3,63 €/item) on listings shipped from outside the EU into Spain.

Ground truth verified against the live Browse API (2026-07-08, EBAY_ES marketplace):

- `item_summary/search` → `shippingOptions` carries **only** `shippingCost` + `shippingCostType`. A CN-located item (91,80 €) returned shipping 0,00 € and no import field. **The exact charge is not obtainable at search time.**
- `getItem` detail → has `importCharges` inside `shippingOptions`, but only for Global Shipping / eBay International Shipping fulfilment; the sampled SpeedPAK item exposed none (VAT 21 % `includedInPrice`). Exact values would cost one extra API call per candidate and still not cover all programs.
- `itemLocation.country` is already parsed into `EbayApiLocation` but only `city` is projected onto `Listing.location` (`fetcher.py:309`) — non-EU detection at search time is free once we project the country.

Operator decision: estimated flat buffer, not detail calls, not exclusion.

## Goals / Non-Goals

**Goals:**

- Buyer totals for non-EU-located eBay listings include a conservative flat import-charges buffer so all four gates and the alert breakdown reflect the real delivered cost.
- Zero extra API calls; zero behaviour change for EU/domestic listings.
- Buffer value configurable (`pricing.assumed_import_charges_eur`, default 3,63 €) so the operator can track eBay's fee without a release.

**Non-Goals:**

- Exact per-item import charges via `getItem` (revisit only if the flat buffer proves materially wrong).
- Modelling customs duty / VAT for high-value imports (> 150 € customs threshold) — the wishlist ceilings sit well below territory where that dominates.
- Excluding non-EU listings (operator wants them, correctly priced).
- Wallapop changes (domestic marketplace).

## Decisions

1. **Flat buffer keyed on item country, computed in the domain.** `buyer_cost` gains a keyword `assumed_import_charges_eur` and adds it when `listing.country` is known and not in the EU set. Alternatives: per-item `getItem` (quota + latency + incomplete coverage) and search-time exclusion (loses bargains) — both rejected by the operator. Mirrors the existing `assumed_shipping_eur` pattern so call sites and config stay symmetric.

2. **`Listing.country: str | None`** (ISO 3166-1 alpha-2, uppercased; `None` = unknown). Projected from eBay `itemLocation.country`; the Wallapop fetcher leaves it `None`. We do NOT overload the human-readable `location` city string — gates need a machine-comparable code.

3. **EU membership as a domain constant** `EU_COUNTRY_CODES` (frozenset, the 27 member states, documented with an as-of date). GB is non-EU (post-Brexit) — correct: UK-shipped items do incur the charge. Kept in `domain/pricing.py` next to its only consumer; no config surface (membership changes are rare enough to be a code change).

4. **Unknown country ⇒ no buffer.** Opposite polarity to the shipping buffer (unknown shipping ⇒ assume cost). Rationale: Wallapop listings and any country-less payload are overwhelmingly domestic; taxing them 3,63 € would silently drop in-ceiling domestic bargains. eBay's `itemLocation.country` is in practice always present, so the escape window is negligible. The estimated flag on the component makes the alert honest either way.

5. **`BuyerCost` grows `import_charges_eur: Decimal` (0 when not applied) and an `import_estimated: bool`** (always `True` when non-zero — the value is never parsed, only assumed). The alert renderer appends `+ importación ~3,63 € (est.)` to the breakdown row only when the component is non-zero; EU/domestic alerts render byte-identical to today.

6. **Reconciliation unchanged in logic:** the expected buyer total already flows in enriched; the receipt's delivered total naturally contains the real import charge. The item-price delta stays in audit context as shipped in v0.3.3.

7. **Config:** `pricing.assumed_import_charges_eur: Decimal = Decimal("3.63")`, `ge=0`, beside `assumed_shipping_eur` in `config_yaml.py`. Composer threads it to the same places it already threads the shipping buffer.

## Risks / Trade-offs

- [eBay's fee may not be flat forever (per-price/per-category tiers)] → config key adjusts the value without a release; if it becomes structural, a follow-up change can swap the flat buffer for a formula the same way Protección is modelled.
- [Over-estimation: some non-EU items (e.g. SpeedPAK with VAT `includedInPrice`) may not actually incur the charge at checkout] → conservative by design — worst case a borderline bargain (within 3,63 € of the ceiling) is dropped; the operator preferred that over under-estimating with real money armed.
- [Under-estimation for heavy/expensive imports (customs above 150 €)] → out of scope (ceilings are far below); documented non-goal.
- [Post-upgrade behaviour shift: non-EU listings previously alerting near the ceiling stop alerting] → intended; release notes call it out so the operator isn't surprised by quieter eBay cycles.
- [A non-EU item with unknown country escapes the buffer] → accepted (Decision 4); eBay payloads virtually always carry the country.

## Migration Plan

Normal tag-driven release (no DB migration — `Listing` is an in-memory pydantic model; `country` defaults to `None` for anything replayed). Deploy to hermes001 = image bump + quadlet restart. Rollback = repin previous tag. The armed Corsair entry (80 €) immediately gets correct totals for the CN-located listings already appearing in its eBay cycles.

## Open Questions

None — the operator resolved the approach (flat buffer) and the value (3,63 € observed).
