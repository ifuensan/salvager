## 1. Domain: shipping field + buyer-total + Wallapop fee

- [x] 1.1 Add `shipping_eur: Decimal | None = None` to `domain/listing.py` `Listing` (docstring: None=unknown, 0=free/included).
- [x] 1.2 New domain pricing module (e.g. `domain/pricing.py`): `proteccion_wallapop_fee(price) -> Decimal` (≤13 → 1.69; else 0.69 + 7.5%·price, clamped to a documented max), with constants + gualacost.com/source-date comment.
- [x] 1.3 `buyer_total_eur(listing, *, assumed_shipping_eur) -> Decimal`: item + shipping(known|buffer) + (Wallapop) Protección fee; eBay = item + shipping. Also expose whether shipping was estimated (for the renderer).
- [x] 1.4 Unit tests: fee boundaries (13.00 vs 13.01, cap), buyer total per marketplace, unknown-shipping buffer path, free shipping (0) path.

## 2. Config

- [x] 2.1 Add `pricing.assumed_shipping_eur` (default ≈ 3.50) to the config model + example config; thread it to where buyer totals are computed.

## 3. Fetchers parse shipping

- [x] 3.1 eBay API fetcher/schema: parse `shippingOptions[].shippingCost` → `Listing.shipping_eur` (None when absent).
- [x] 3.2 Wallapop API fetcher/schema: parse shipping/envío fields → `shipping_eur` (None when in-person-only/unknown). Stay within the adapter package. _(The v3 search API exposes no fixed shipping cost → always None; the buffer covers it. Documented in the fetcher.)_
- [x] 3.3 Fetcher tests: shipping parsed when present; None when absent.

## 4. Ceilings + reconciliation use the total

- [x] 4.1 `poll_loop.py` Phase 1 alert gate: compare `buyer_total_eur(...)` to the entry ceiling.
- [x] 4.2 eBay fetcher: keep the item-level API pre-filter; add a post-fetch buyer-total filter.
- [x] 4.3 `phase2_preflight.py:84`: compare buyer total to `phase2.max_price_eur` (keep the item-price delta in audit ctx).
- [x] 4.4 `reconciler.py`: compare delivered total vs expected total; surface the item-price delta in ctx.
- [x] 4.5 Tests for each gate: within/over ceiling by shipping; unknown-shipping buffer; reconciler like-for-like.

## 5. Alert renderer

- [x] 5.1 `domain/alert.py` render_phase1/phase2_listing_alert: price line shows item + shipping (+ Protección on Wallapop) = total, marking estimated shipping.
- [x] 5.2 Regenerate renderer snapshots + assert the breakdown; add an estimated-shipping case.

## 6. Verification

- [x] 6.1 `ruff check` + `ruff format --check`, adapter-discipline (NFR-M1), payment-rail (FR25/NFR-S5) clean.
- [x] 6.2 `mypy src tests` clean.
- [x] 6.3 Full pytest green (ignoring the 2 known sandbox `/app` failures).
- [x] 6.4 `openspec validate shipping-aware-pricing --strict` passes.
