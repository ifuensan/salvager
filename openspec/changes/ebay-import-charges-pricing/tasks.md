## 1. Domain — model & pricing

- [x] 1.1 Add `country: str | None` to `Listing` (ISO 3166-1 alpha-2, uppercased, default `None`) with field docs mirroring `shipping_eur`
- [x] 1.2 Add `EU_COUNTRY_CODES` frozenset (27 member states, documented as-of date) to `domain/pricing.py`
- [x] 1.3 Extend `BuyerCost` with `import_charges_eur: Decimal` and `import_estimated: bool`; extend `buyer_cost`/`buyer_total_eur` with keyword `assumed_import_charges_eur`, applied only when `listing.country` is known and not in `EU_COUNTRY_CODES`
- [x] 1.4 Unit tests: CN adds buffer, GB adds buffer, ES/DE add nothing, `None` country adds nothing, rounding still half-up to whole cents, Wallapop totals byte-identical to v0.3.3 behaviour

## 2. Config

- [x] 2.1 Add `pricing.assumed_import_charges_eur: Decimal` (`ge=0`, default `3.63`) to `config_yaml.py` beside `assumed_shipping_eur`, with docstring citing the observed flat 3,63 € (2026-07-07)
- [x] 2.2 Config tests: default value, YAML override, rejection of negatives

## 3. eBay adapter

- [x] 3.1 Project `itemLocation.country` (uppercased) onto `Listing.country` in `adapters/ebay_api/fetcher.py`; leave `None` when absent
- [x] 3.2 Thread `assumed_import_charges_eur` into the fetcher's post-fetch buyer-total ceiling filter
- [x] 3.3 Fetcher tests: country projected, missing itemLocation → `None`, post-fetch filter drops a non-EU item whose buffered total exceeds the ceiling and keeps the same item when EU-located

## 4. Gates, reconciler & composer threading

- [x] 4.1 Thread the new keyword through `poll_loop._filter_over_ceiling` (Phase 1 alert gate) and `Phase2Preflight` (buy gate), sourced from config in the composer
- [x] 4.2 Confirm the reconciler's expected-total path picks up the enriched total (no logic change expected); add a test that an import-buffered expected total does not spuriously trip reconciliation
- [x] 4.3 Wallapop path regression test: `country=None` listings produce identical gate outcomes to v0.3.3

## 5. Alert rendering

- [x] 5.1 Append the import component to the breakdown row in `domain/alert.py` only when non-zero, always marked estimated (e.g. `+ importación 3,63 € (est.)`)
- [x] 5.2 Alert tests: non-EU eBay alert shows the import line and enriched total; EU/Wallapop alerts render byte-identical to current snapshots

## 6. Verification & docs

- [x] 6.1 Full gate: ruff + format + mypy + pytest green; `openspec validate --all --strict` passes
- [x] 6.2 Update CHANGELOG (note the behaviour shift: near-ceiling non-EU eBay listings stop alerting) and README config table with the new key
