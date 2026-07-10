# ebay-import-charges-pricing

## Why

eBay now applies an import charge to items shipped into Spain from outside the European Union (operator observed a flat **3,63 € per item** in the eBay UI, 2026-07-07). Our buyer-total pipeline (shipped in v0.3.3) sums item + shipping only, so a non-EU listing under-estimates the real delivered cost — the same class of real-money risk the shipping-aware-pricing change fixed: Phase 2 could autobuy (and Phase 1 could alert) at a total the operator would never accept.

The Browse API gives us no exact value at search time: `item_summary/search` `shippingOptions` carry only `shippingCost` + `shippingCostType` (verified against the live API 2026-07-08 — a China-located item priced 91,80 € returned shipping 0,00 € and no import field). `importCharges` exists only in the per-item `getItem` detail, and only for some fulfilment programs. Operator decision: use an **estimated flat buffer** for non-EU items rather than per-item detail calls or excluding non-EU listings.

## What Changes

- `Listing` gains a `country` field (ISO 3166-1 alpha-2, `None` = unknown), populated by the eBay fetcher from `itemLocation.country` (already parsed into the API schema, currently dropped). The Wallapop adapter always leaves `country` as `None` (domestic marketplace; unknown country adds no import component).
- `domain/pricing.py::buyer_cost` adds an **import-charges component**: when the listing's country is known and outside the EU, add a configurable flat buffer `pricing.assumed_import_charges_eur` (default **3,63 €**), flagged as estimated. Unknown/missing country adds nothing (no false positives on Wallapop or country-less payloads).
- The EU membership set lives in the domain as a documented constant (27 member states).
- The buffered total flows through every existing buyer-total consumer unchanged (they already call `buyer_total_eur`): eBay post-fetch ceiling filter, Phase 1 alert gate, Phase 2 buy gate, reconciler.
- The alert breakdown row shows an import-charges line, marked estimated, **only when the buffer was applied** (EU/domestic listings render exactly as today).
- New config key `pricing.assumed_import_charges_eur` (Decimal ≥ 0, default 3.63) beside `assumed_shipping_eur`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `shipping-aware-pricing`: the buyer-total requirement grows an import-charges component for non-EU listings (estimated flat buffer); the fetcher requirement grows country capture; the alert-breakdown requirement grows a conditional import line. Ceiling-comparison and reconciliation requirements are unchanged in wording but now operate on the enriched total.

## Impact

- **Code:** `domain/listing.py` (new field), `domain/pricing.py` (`BuyerCost` + `buyer_cost`/`buyer_total_eur` signature grows a keyword), `adapters/ebay_api/fetcher.py` (project `itemLocation.country`), `config/config_yaml.py` (new pricing key), `domain/alert.py` (breakdown row), plus the thin call-site updates in `poll_loop`, `phase2_preflight`, reconciler, and the eBay post-fetch filter where the new keyword is threaded.
- **Config:** new optional `pricing.assumed_import_charges_eur`; existing deployments keep today's behaviour for EU listings and gain the buffer for non-EU ones on upgrade.
- **No API-quota impact** (no extra eBay calls). No DB migration (`Listing` is not persisted with a rigid schema — verify in design).
- **Ops:** hermes001 picks this up via a normal release (image bump + restart); armed Corsair entry benefits immediately since its eBay results include CN-located listings.
