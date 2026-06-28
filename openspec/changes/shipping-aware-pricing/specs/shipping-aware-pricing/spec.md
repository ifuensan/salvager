## ADDED Requirements

### Requirement: Listings Carry Shipping And A Buyer-Total Is Computed

`Listing` SHALL carry `shipping_eur: Decimal | None` (`None` = unknown/not-parsed, `0` = free/included). The domain SHALL provide a pure `buyer_total_eur(listing, *, assumed_shipping_eur)` that returns the full delivered cost the buyer pays: the item price, plus shipping (`shipping_eur` when known else `assumed_shipping_eur`), plus — on Wallapop only — the mandatory Protección Wallapop fee. The Protección fee SHALL be `1.69 €` for an item price ≤ 13 €, and `0.69 € + 7.5 % of the item price` (clamped to a documented maximum) above 13 €.

#### Scenario: Wallapop buyer total includes fee and shipping

- **WHEN** `buyer_total_eur` is computed for a Wallapop listing priced at 55,00 € with known shipping 3,49 €
- **THEN** the total is `55.00 + proteccion_fee(55.00) + 3.49`
- **AND** `proteccion_fee(55.00)` equals `0.69 + 55.00 * 0.075`

#### Scenario: Protección fee boundary at 13 €

- **WHEN** the fee is computed for an item priced exactly 13,00 € versus 13,01 €
- **THEN** 13,00 € yields the fixed 1,69 € and 13,01 € yields the variable formula

#### Scenario: eBay buyer total has shipping but no Wallapop fee

- **WHEN** `buyer_total_eur` is computed for an eBay listing priced 63,66 € with shipping 16,82 €
- **THEN** the total is 80,48 € (no Protección fee added)

#### Scenario: Unknown shipping uses the configurable buffer

- **WHEN** a listing has `shipping_eur is None`
- **THEN** the shipping component of the buyer total is `assumed_shipping_eur` (config `pricing.assumed_shipping_eur`)
- **AND** the buyer total is never computed as if shipping were zero

---

### Requirement: Fetchers Parse Shipping Cost

The Wallapop API and eBay API fetchers SHALL populate `Listing.shipping_eur` from the upstream payload when available (eBay `shippingOptions[].shippingCost`; Wallapop's shipping/envío fields), and SHALL leave it `None` when the payload exposes no shipping price (e.g. in-person-only). Shipping parsing SHALL stay inside the respective adapter packages (adapter-discipline).

#### Scenario: eBay shipping parsed from the API

- **WHEN** an eBay item summary includes a `shippingOptions` shipping cost
- **THEN** the resulting `Listing.shipping_eur` equals that cost

#### Scenario: Listing with no shipping data

- **WHEN** the upstream payload exposes no shipping price for a listing
- **THEN** `Listing.shipping_eur` is `None` (not 0)

---

### Requirement: Price Ceilings Compare The Buyer Total

The Phase 1 alert gate and the Phase 2 buy gate SHALL compare the buyer total (not the item price) against the entry's ceiling. The eBay search MAY keep an item-level API pre-filter for quota economy, but the authoritative ceiling check SHALL be applied post-fetch against the buyer total.

#### Scenario: Phase 2 buy blocked when total exceeds ceiling

- **WHEN** a listing's item price is at or below the Phase 2 ceiling but its buyer total exceeds it
- **THEN** the Phase 2 buy gate returns ineligible (`phase2_max_price_below_listing` or equivalent) and the alert renders Phase 1 (no Comprar)

#### Scenario: A within-ceiling delivered total stays buyable

- **WHEN** a listing's buyer total is at or below the Phase 2 ceiling
- **THEN** the Phase 2 gate's price check passes

---

### Requirement: Alerts Show The Delivered Total

Phase 1 and Phase 2 listing alerts SHALL show the buyer total on the price line, broken down as item + shipping (+ Protección on Wallapop) = total, and SHALL indicate when shipping is estimated from the buffer rather than known.

#### Scenario: Alert shows breakdown with known shipping

- **WHEN** a Wallapop alert renders for a listing with known shipping
- **THEN** the price line shows the item price, the shipping, the Protección fee, and the delivered total

#### Scenario: Alert flags estimated shipping

- **WHEN** the listing's shipping is unknown and the buffer was applied
- **THEN** the price line marks the shipping component as estimated (e.g. `envío: ? (est.)`)

---

### Requirement: Reconciliation Compares Like-For-Like Totals

The Phase 2 reconciler SHALL compare the receipt's delivered total against the expected buyer total, so shipping and fees do not spuriously trip the price-reconciliation safety check, while still surfacing the item-price delta in the audit context.

#### Scenario: Shipping does not trip reconciliation

- **WHEN** the receipt total includes shipping/fees that were already part of the expected buyer total
- **THEN** reconciliation does not fail solely because of the shipping/fee component
