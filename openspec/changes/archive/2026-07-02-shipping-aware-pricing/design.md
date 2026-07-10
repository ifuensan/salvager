## Context

`Listing` carries only `price_eur`. Ceilings are checked in three places, all item-only: the Phase 1 alert gate + `SearchQuery` filter (`poll_loop.py`), the eBay API `price:[..max]` filter (`ebay_api/fetcher.py`), and the Phase 2 buy gate (`phase2_preflight.py:84`). The reconciler compares an item-only expected price. On Wallapop the buyer ALSO always pays a mandatory "Protección Wallapop" fee + shipping (operator-sourced, gualacost.com): ≤13 € → 1,69 € fixed; >13 € → ≈ 0,69 € + 7,5 % of item price, capped ≈ 50 €. eBay exposes shipping in `shippingOptions[].shippingCost`; it has no Protección fee.

## Goals / Non-Goals

**Goals:**
- Ceilings (alert + Phase 2 buy) and reconciliation reflect the **delivered total the buyer pays**.
- The operator sees the real cost (item + shipping + fee) in the alert before tapping Comprar.
- Stay usable when shipping is unknown (configurable buffer), without silently assuming free.

**Non-Goals:**
- Perfect shipping prediction (carrier/weight/destination vary) — a buffer is acceptable for the unknown case.
- Modelling eBay's own buyer fees beyond the API-reported shipping.
- Changing the LLM prompt, parsers, or the Phase-2-buy/operational alert flows beyond the price line.

## Decisions

**1. `shipping_eur: Decimal | None` on `Listing` + a domain `buyer_total_eur()` helper.**
`None` = unknown/not-parsed; `0` = free/included. The delivered total is computed by a pure domain function `buyer_total_eur(listing, *, assumed_shipping_eur)`:
- shipping component = `listing.shipping_eur` if not None, else `assumed_shipping_eur` (the configurable buffer).
- Wallapop: `price + proteccion_fee(price) + shipping`.
- eBay: `price + shipping`.
Keeping the total in one helper means the alert gate, buy gate, reconciler, and renderer all agree.

**2. Protección Wallapop fee is a deterministic, documented function.**
`proteccion_fee(price)`: `Decimal("1.69")` for `price <= 13`; else `Decimal("0.69") + price * Decimal("0.075")`, clamped to a max ≈ `Decimal("50")`. Constants live in one place with a comment citing gualacost.com + the date the operator provided them, since Wallapop can change them. _Alternative considered:_ make every constant config — rejected as over-config; the buffer (`assumed_shipping_eur`) is the one knob operators realistically tune.

**3. Unknown shipping → configurable buffer, not block (operator-chosen).**
`pricing.assumed_shipping_eur` (default ≈ 3,50 €, light ≤2 kg standard). Applied when `shipping_eur is None` so the buy gate stays usable; conservative because it adds cost rather than assuming free. _Alternative considered:_ block-the-autobuy on unknown — rejected by the operator (too restrictive; most Wallapop listings would never autobuy). The operator-confirm tap + the buffer + the alert showing "envío: ? (estimado)" are the combined safeguard.

**4. Ceilings compare buyer total.**
Phase 1 alert gate and Phase 2 buy gate (`phase2_preflight.py:84`) compare `buyer_total_eur(...)` to the ceiling instead of `listing.price_eur`. eBay's API `price` filter stays (item-level pre-filter to save quota) but a post-fetch filter drops listings whose buyer total exceeds the ceiling. Wallapop's search filter is best-effort; the authoritative gate is post-fetch.

**5. Alert price line shows the breakdown.**
`render_phase1/phase2_listing_alert` render e.g. `55,00 € + 3,49 € envío + 4,82 € Protección = 63,31 €`, or `… + envío: ? (est. 3,50 €) …` when shipping is buffered. Exact wording locked by snapshot tests. This re-touches the renderer → noted for the Story 5.17 re-audit.

**6. Reconciler compares total vs total.**
The expected value carried into reconciliation becomes the buyer total; the receipt total (which includes shipping/fees) is compared like-for-like, so shipping no longer spuriously trips the price-reconciliation safety check.

## Risks / Trade-offs

- **Buffer under-estimates real shipping** → a buy could still slightly exceed the ceiling. Mitigation: default buffer set to the upper Wallapop standard band; operator tunes `assumed_shipping_eur`; the operator-confirm tap remains. Heavy items (>2 kg) cost more — documented as a known limitation of the buffer.
- **Protección formula drift** → Wallapop changes its fee schedule. Mitigation: constants in one documented place with the source/date; a unit test pins the boundary cases so a wrong edit fails loudly.
- **Wallapop API may not expose shipping at search time** → most Wallapop listings hit the buffer path; acceptable per decision 3.
- **Every listing alert's price line changes** → all renderer snapshots churn (additive); regenerate + assert the breakdown. Adds to the Story 5.17 re-audit (narrow — one line).
- **Reconciler semantics change** could mask a genuine item-price mismatch if total-vs-total hides it. Mitigation: keep the item-price delta available in the audit ctx even when gating on total.
