## MODIFIED Requirements

### Requirement: Price Ceilings Compare The Buyer Total

The Phase 1 alert gate and the Phase 2 buy gate SHALL compare the buyer total (not the item price) against the entry's ceiling. The eBay search MAY keep an item-level API pre-filter for quota economy, but the authoritative ceiling check SHALL be applied post-fetch against the buyer total.

The over-ceiling alert-gate filter SHALL have exactly one carve-out: a **Wallapop** listing on an entry with `offer.enabled = true` whose buyer total exceeds the ceiling but is at or below `ceiling × (1 + offer.band_pct)` SHALL NOT be dropped — it SHALL be tagged negotiable and routed onward to evaluation per the `wallapop-offer-flow` capability. Listings beyond the band, listings on offer-disabled entries, and all eBay listings SHALL be filtered over-ceiling exactly as before. The carve-out SHALL NOT affect the Phase 2 buy gate: a buyer total over the ceiling SHALL remain ineligible to buy regardless of offer settings.

#### Scenario: Phase 2 buy blocked when total exceeds ceiling

- **WHEN** a listing's item price is at or below the Phase 2 ceiling but its buyer total exceeds it
- **THEN** the Phase 2 buy gate returns ineligible (`phase2_max_price_below_listing` or equivalent) and the alert renders Phase 1 (no Comprar)

#### Scenario: A within-ceiling delivered total stays buyable

- **WHEN** a listing's buyer total is at or below the Phase 2 ceiling
- **THEN** the Phase 2 gate's price check passes

#### Scenario: In-band Wallapop listing survives the alert gate

- **WHEN** a Wallapop listing on an offer-enabled entry has a buyer total of 88 € against an 80 € ceiling with `offer.band_pct = 0.20`
- **THEN** the alert gate keeps the listing, tagged negotiable, instead of filtering it

#### Scenario: The carve-out never applies to eBay or offer-disabled entries

- **WHEN** an eBay listing, or a Wallapop listing on an entry without `offer.enabled`, has a buyer total over the ceiling
- **THEN** it is filtered exactly as before this change

#### Scenario: Negotiable never means buyable

- **WHEN** a negotiable-tagged listing reaches the Phase 2 buy gate with a buyer total over the ceiling
- **THEN** the buy gate returns ineligible
