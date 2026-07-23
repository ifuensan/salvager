# shipping-aware-pricing Specification

## Purpose
TBD - created by archiving change shipping-aware-pricing. Update Purpose after archive.
## Requirements
### Requirement: Listings Carry Shipping And A Buyer-Total Is Computed

`Listing` SHALL carry `shipping_eur: Decimal | None` (`None` = unknown/not-parsed, `0` = free/included) and `country: str | None` (ISO 3166-1 alpha-2 item-location country, uppercased; `None` = unknown). The domain SHALL provide a pure `buyer_total_eur(listing, *, assumed_shipping_eur, assumed_import_charges_eur)` that returns the full delivered cost the buyer pays: the item price, plus shipping (`shipping_eur` when known else `assumed_shipping_eur`), plus — on Wallapop only — the mandatory Protección Wallapop fee, plus — when `country` is known and is not an EU member state — the estimated import-charges buffer `assumed_import_charges_eur` (config `pricing.assumed_import_charges_eur`, default 3,63 €). The Protección fee SHALL be `1.69 €` for an item price ≤ 13 €, and `0.69 € + 7.5 % of the item price` (clamped to a documented maximum) above 13 €. EU membership SHALL be a documented domain constant (the 27 member states). An unknown `country` SHALL add no import component. The buyer total SHALL be rounded to whole cents after summing its components, using half-up rounding (the unit a marketplace charges to).

#### Scenario: Wallapop buyer total includes fee and shipping

- **WHEN** `buyer_total_eur` is computed for a Wallapop listing priced at 55,00 € with known shipping 3,49 €
- **THEN** the total is `55.00 + proteccion_fee(55.00) + 3.49`
- **AND** `proteccion_fee(55.00)` equals `0.69 + 55.00 * 0.075`

#### Scenario: Protección fee boundary at 13 €

- **WHEN** the fee is computed for an item priced exactly 13,00 € versus 13,01 €
- **THEN** 13,00 € yields the fixed 1,69 € and 13,01 € yields the variable formula

#### Scenario: eBay buyer total has shipping but no Wallapop fee

- **WHEN** `buyer_total_eur` is computed for an eBay listing priced 63,66 € with shipping 16,82 € located in an EU country
- **THEN** the total is 80,48 € (no Protección fee, no import charges added)

#### Scenario: Unknown shipping uses the configurable buffer

- **WHEN** a listing has `shipping_eur is None`
- **THEN** the shipping component of the buyer total is `assumed_shipping_eur` (config `pricing.assumed_shipping_eur`)
- **AND** the buyer total is never computed as if shipping were zero

#### Scenario: Non-EU listing adds the import-charges buffer

- **WHEN** `buyer_total_eur` is computed for an eBay listing with `country = "CN"` priced 91,80 € with known shipping 0,00 €
- **THEN** the total is `91.80 + 0.00 + assumed_import_charges_eur`
- **AND** the import component is flagged estimated

#### Scenario: Post-Brexit UK counts as non-EU

- **WHEN** a listing has `country = "GB"`
- **THEN** the import-charges buffer is added to the buyer total

#### Scenario: Unknown country adds no import component

- **WHEN** a listing has `country is None` (e.g. any Wallapop listing)
- **THEN** the buyer total contains no import-charges component

---

### Requirement: Fetchers Parse Shipping Cost

The Wallapop API and eBay API fetchers SHALL populate `Listing.shipping_eur` from the upstream payload when available (eBay `shippingOptions[].shippingCost`; Wallapop's shipping/envío fields), and SHALL leave it `None` when the payload exposes no shipping price (e.g. in-person-only). When an eBay item carries multiple `shippingOptions`, the fetcher SHALL take the **cheapest priced** option, so a multi-option listing is not overestimated against the ceiling. The eBay fetcher SHALL additionally populate `Listing.country` from `itemLocation.country` (uppercased alpha-2; `None` when absent); the Wallapop fetcher SHALL leave `country` as `None` (domestic marketplace). Shipping and country parsing SHALL stay inside the respective adapter packages (adapter-discipline).

#### Scenario: eBay shipping parsed from the API

- **WHEN** an eBay item summary includes a `shippingOptions` shipping cost
- **THEN** the resulting `Listing.shipping_eur` equals that cost

#### Scenario: Listing with no shipping data

- **WHEN** the upstream payload exposes no shipping price for a listing
- **THEN** `Listing.shipping_eur` is `None` (not 0)

#### Scenario: eBay item-location country projected

- **WHEN** an eBay item summary carries `itemLocation.country = "CN"`
- **THEN** the resulting `Listing.country` is `"CN"`

#### Scenario: Missing item location leaves country unknown

- **WHEN** an eBay item summary carries no `itemLocation` (or one without `country`)
- **THEN** `Listing.country` is `None`

---

### Requirement: Price Ceilings Compare The Buyer Total

The Phase 1 alert gate and the Phase 2 buy gate SHALL compare the buyer total (not the item price) against the entry's ceiling. The eBay search MAY keep an item-level API pre-filter for quota economy, but the authoritative ceiling check SHALL be applied post-fetch against the buyer total.

The over-ceiling alert-gate filter SHALL have exactly one carve-out: a **Wallapop** listing on an entry with `offer.enabled = true` whose buyer total exceeds the ceiling but is at or below `ceiling × (1 + offer.band_pct)` — and for which a valid offer amount exists (`wallapop-offer-flow` amount requirement; the 70 % platform floor can rule one out) — SHALL NOT be dropped — it SHALL be tagged negotiable and routed onward to evaluation per the `wallapop-offer-flow` capability. Listings beyond the band, listings on offer-disabled entries, and all eBay listings SHALL be filtered over-ceiling exactly as before. The carve-out SHALL NOT affect the Phase 2 buy gate: a buyer total over the ceiling SHALL remain ineligible to buy regardless of offer settings.

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
---

### Requirement: Alerts Show The Delivered Total

Phase 1 and Phase 2 listing alerts SHALL show the buyer total on the price line, broken down as item + shipping (+ Protección on Wallapop) (+ import charges when applied) = total, and SHALL indicate when shipping is estimated from the buffer rather than known. The import-charges component SHALL render only when it is non-zero, SHALL always be marked estimated (its value is assumed, never parsed), and EU/domestic alerts SHALL render identically to alerts before this change.

#### Scenario: Alert shows breakdown with known shipping

- **WHEN** a Wallapop alert renders for a listing with known shipping
- **THEN** the price line shows the item price, the shipping, the Protección fee, and the delivered total

#### Scenario: Alert flags estimated shipping

- **WHEN** the listing's shipping is unknown and the buffer was applied
- **THEN** the price line marks the shipping component as estimated (e.g. `envío: ? (est.)`)

#### Scenario: Alert shows estimated import charges for a non-EU listing

- **WHEN** an eBay alert renders for a listing whose buyer total includes the import-charges buffer
- **THEN** the price line includes the import component marked estimated (e.g. `+ importación 3,63 € (est.)`) and the total reflects it

#### Scenario: EU listing breakdown is unchanged

- **WHEN** an alert renders for a listing with an EU or unknown country
- **THEN** the price line contains no import-charges component

---

### Requirement: Reconciliation Compares Like-For-Like Totals

The Phase 2 reconciler SHALL compare the receipt's delivered total against the expected buyer total, so shipping and fees do not spuriously trip the price-reconciliation safety check, while still surfacing the item-price delta in the audit context.

#### Scenario: Shipping does not trip reconciliation

- **WHEN** the receipt total includes shipping/fees that were already part of the expected buyer total
- **THEN** reconciliation does not fail solely because of the shipping/fee component

