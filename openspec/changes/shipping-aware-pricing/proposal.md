## Why

Every price ceiling in the pipeline compares the **item price only**, ignoring shipping and (on Wallapop) the mandatory buyer fee. `Listing` has just `price_eur`; neither fetcher parses shipping; and the Phase 1 alert gate, the eBay search filter, and the Phase 2 buy gate (`phase2_preflight.py:84` `if listing.price_eur > max_price`) all use `price_eur`. Operator screenshots proved the gap live: eBay Corsair listings at 63,66 € + 16,82 € shipping = 80,48 € and 73,07 € + 22,18 € shipping = 95,25 € both pass an 80 € item ceiling but exceed it delivered. Phase 2 could autobuy on a Comprar tap at a total well over the operator's intended ceiling, and the alert shows only the item price so even the operator-confirm tap is uninformed. (Interim mitigation already applied: the armed Corsair entry's ceiling was dropped 80 → 55 €; this change is the real fix.)

## What Changes

- `Listing` gains `shipping_eur: Decimal | None` (`None` = unknown/not-parsed, `0` = free/included) and the domain gains a **buyer-total** helper that computes the delivered cost the buyer actually pays, per marketplace.
- **Wallapop buyer total** = item + **Protección Wallapop** fee + shipping. The Protección fee is deterministic from the item price (operator-sourced, gualacost.com, 2026-06-16): items ≤ 13 € → fixed 1,69 €; > 13 € → ≈ 0,69 € + 7,5 % of item price, capped ≈ 50 €. Shipping is parsed from the API when present, else a configurable buffer (`pricing.assumed_shipping_eur`, default ≈ 3,50 € for light ≤ 2 kg items).
- **eBay buyer total** = item + shipping from `shippingOptions[].shippingCost` (no Wallapop fee). Unknown shipping → the same configurable buffer.
- **Ceilings compare the buyer total**, not the item price: the Phase 1 alert gate, a post-fetch total filter for eBay, and the Phase 2 buy gate. Unknown-shipping policy (operator-chosen): apply the configurable buffer rather than blocking, so the gate stays usable while staying conservative.
- **Alert renderer** shows the breakdown on the price line — `precio + envío (+ Protección) = total` (and `+ envío: ?` / buffer-estimated when not exact) — so the operator-confirm tap sees the real cost. (Re-touches `domain/alert.py`, adding to the Story 5.17 rendering re-audit scope already flagged.)
- **Reconciler** compares like-for-like (delivered total vs expected total) so shipping/fees don't spuriously trip reconciliation and the receipt total is checked against the ceiling.

## Capabilities

### New Capabilities
- `shipping-aware-pricing`: listing price ceilings (alert + Phase 2 buy), the alert display, and reconciliation account for the full buyer-paid total — item + shipping + (Wallapop) mandatory fee — not the item price alone.

### Modified Capabilities
<!-- None promoted. The Phase 2 buy gate / alert anatomy were specified in PRD stories / unarchived changes, not in openspec/specs/; this adds a new capability. -->

## Impact

- **Code:** `domain/listing.py` (+`shipping_eur`), a domain pricing module (Wallapop Protección formula + buyer-total helper), `config` (`pricing.assumed_shipping_eur`), Wallapop + eBay API fetchers/schemas (parse shipping), `orchestration/poll_loop.py` + `phase2_preflight.py` (compare buyer total), `orchestration/reconciler.py` (total vs total), `domain/alert.py` renderers (price-line breakdown).
- **Tests:** Protección-fee formula (boundaries 13/13.01 €, cap), buyer-total per marketplace + unknown-shipping buffer, ceiling gates against total, fetcher shipping parsing, renderer snapshots (breakdown), reconciler like-for-like.
- **Ops:** likely **v0.4.0** (new pricing semantics) + redeploy; afterward restore the Corsair ceiling from the interim 55 € toward the operator's true total ceiling. No LLM/prompt change; adapter-discipline + payment-rail stay green.
