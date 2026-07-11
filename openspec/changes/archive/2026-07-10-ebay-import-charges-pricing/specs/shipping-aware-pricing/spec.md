## MODIFIED Requirements

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
