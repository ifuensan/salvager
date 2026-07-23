# wallapop-offer-flow Specification

## Purpose
Operator-confirmed Wallapop price offers ("hacer oferta", FR58-FR65): which listings render the 💰 Ofertar surface (incl. the over-ceiling negotiable band), the target-fit whole-euro amount bounded by the platform's 70 % floor, the tap → preflight → re-fetch → bounded send pipeline with its closed failure set and keyboard lifecycle, the per-listing dedupe + rolling daily budget + independent lockout, and the append-only offers audit trail. v1 ends at "offer sent".
## Requirements
### Requirement: Offer Amount Is Derived From The Entry's Target Total

The domain SHALL provide a pure offer-amount function that returns the largest whole-euro item price `O` such that `buyer_total_eur(O)` (same shipping and Protección model as the pricing capability) is at or below the entry's **offer target**. The offer target SHALL be `offer.target_total_eur` when set on the entry, else the entry's effective ceiling (`max_price_solo`). The function SHALL return no amount (offer not possible) when `O` is not strictly below the listing's asking item price, when `O` would be ≤ 0, or when `O` is below the platform floor of 70 % of the asking item price (Wallapop rejects discounts deeper than 30 %). The computed amount SHALL be rendered on the alert before any tap, and SHALL be recomputed from the reconciled listing at tap time.

#### Scenario: Band listing gets a ceiling-fit offer

- **WHEN** a Wallapop listing asks 88,00 € with known shipping 3,50 € against an 80,00 € target
- **THEN** the offer amount is the largest whole-euro item price whose item + shipping + Protección total is ≤ 80,00 €
- **AND** the amount is strictly below 88,00 €

#### Scenario: Under-ceiling listing with default target yields no offer

- **WHEN** a listing's buyer total is already at or below the entry ceiling and the entry has no `offer.target_total_eur`
- **THEN** the ceiling-fit amount is not strictly below the asking price and no offer amount is produced (no Ofertar button)

#### Scenario: A lower per-entry target activates offers under the ceiling

- **WHEN** the entry sets `offer.target_total_eur` = 70,00 € and a listing's buyer total is 78,00 € (under the 80,00 € ceiling)
- **THEN** an offer amount fitting the 70,00 € target is produced and it is strictly below the asking price

#### Scenario: Platform floor blocks too-deep offers

- **WHEN** the target-fit price would be below 70 % of the listing's asking item price (e.g. a 40 € fit against a 60 € asking price)
- **THEN** no offer amount is produced and no Ofertar button renders

---

### Requirement: The Ofertar Button Renders Only Where An Offer Is Possible

Wallapop listing alerts (Phase 1 and Phase 2) SHALL carry a `💰 Ofertar` row when and only when: the entry has `offer.enabled = true`, an offer amount was produced per the amount requirement, and no successful offer has previously been sent for the listing. eBay alerts SHALL never carry the row. The button's callback data SHALL follow the locked `<surface>:<verb>:<id>` format with a new `offer` verb, referencing the alert snapshot id. The `💰 Ofertar` label and any new status tokens SHALL be added to the locked `BUTTON_LABELS` / severity-token sets via PRD amendment as part of this change.

#### Scenario: Offer-enabled entry with headroom shows the button

- **WHEN** a negotiable-band Wallapop alert renders for an offer-enabled entry with a computed offer of 74 €
- **THEN** the keyboard includes a `💰 Ofertar` row with callback data `listing:offer:<alert-id>`

#### Scenario: Offer-disabled entry renders unchanged

- **WHEN** a Wallapop alert renders for an entry without `offer.enabled`
- **THEN** the keyboard is byte-identical to the pre-feature keyboard for that alert type

#### Scenario: eBay alerts never offer

- **WHEN** an eBay alert renders for an entry with `offer.enabled = true`
- **THEN** no Ofertar row is present

---

### Requirement: Negotiable-Band Listings Produce A Distinct Alert

Wallapop listings on offer-enabled entries whose buyer total exceeds the entry ceiling but is at or below `ceiling × (1 + offer.band_pct)` (config, default 0.20) — AND for which a valid offer amount exists per the amount requirement (the platform's 70 % floor can rule one out) — SHALL NOT be filtered by the over-ceiling gate; they SHALL pass through the unchanged LLM evaluation and confidence gate and, when they pass, render a **negotiable alert**: a distinct severity token, the standard buyer-total breakdown, an offer line showing the computed amount and the target it fits, and the Ofertar row — never a Comprar row. Listings beyond the band, on offer-disabled entries, or on eBay SHALL be filtered exactly as before. Negotiable alerts SHALL create watch rows and participate in seen-listing dedupe like any listing alert.

#### Scenario: In-band listing alerts as negotiable

- **WHEN** a Wallapop listing's buyer total is 88 € against an 80 € ceiling with `band_pct = 0.20` on an offer-enabled entry, and its evaluation passes the entry's confidence threshold
- **THEN** a negotiable alert is dispatched with the offer line and the Ofertar row and no Comprar row

#### Scenario: Beyond-band listing stays filtered

- **WHEN** the same entry sees a listing with buyer total 97 € (> 80 € × 1.20)
- **THEN** the listing is filtered with no alert, exactly as before this change

#### Scenario: Low-confidence band listing does not alert

- **WHEN** an in-band listing's evaluation falls below the entry's confidence threshold
- **THEN** no alert is dispatched

---

### Requirement: An Offer Is Sent Only Through The Operator-Tap Safety Stack

An offer SHALL be sent only in response to an operator tap on the Ofertar button (no autonomous offers). The tap SHALL drive, in order: an offer preflight (entry still offer-enabled per a fresh wishlist read, offer path not locked out or kill-switched, daily offer budget not exhausted, listing not reserved, no prior successful offer for the listing), a cross-source reconciliation re-fetch of the listing by internal id, recomputation of the offer amount from the fresh listing (aborting when the fresh amount drifts from the displayed amount beyond the configured reconciliation tolerance, or no longer undercuts the asking price), and execution through the browser-session port with the exact bounded amount — the agent goal SHALL forbid sending any other amount. A 404 on the re-fetch SHALL abort with `listing_gone`. Safety aborts SHALL send no offer.

#### Scenario: Happy path sends the displayed amount

- **WHEN** the operator taps Ofertar on an alert showing a 74 € offer and reconciliation returns the listing unchanged
- **THEN** the browser adapter is invoked with exactly 74 € and, on success, the outcome is `Oferta enviada`

#### Scenario: Gone listing aborts fail-closed

- **WHEN** the reconciliation re-fetch returns 404
- **THEN** no offer is sent, the outcome is a `listing_gone` abort, and the lockout counter is not incremented

#### Scenario: Price rise invalidates the displayed offer

- **WHEN** the reconciled listing's price changed such that the recomputed amount drifts beyond tolerance from the displayed amount
- **THEN** no offer is sent and the outcome is a `reconciliation_tripped` abort

#### Scenario: Duplicate offer is refused

- **WHEN** the operator taps Ofertar for a listing that already has a successful offer recorded
- **THEN** no offer is sent and the outcome is a `duplicate_offer` abort

---

### Requirement: Offer Failure Reasons Are A Closed Rendered Set

Offer outcomes SHALL use a dedicated closed enum `OfferFailureReason` (`listing_gone`, `reconciliation_tripped`, `offer_unavailable`, `amount_rejected`, `daily_limit_reached`, `duplicate_offer`, `lockout_engaged`, `missing_element`, `marketplace_error`, `timeout`, `screenshot_missing`, `ui_check_failed`), separate from `BuyFailureReason`. Every variant SHALL have a Spanish label, detail rows, and next-steps in the render table — a variant without a render entry SHALL fail loudly. Failure alerts SHALL carry the reassurance line "No se ha enviado ninguna oferta.", except reasons where the send may have happened but cannot be proven (e.g. `screenshot_missing`), whose copy SHALL state that ambiguity and direct the operator to verify in the Wallapop app.

#### Scenario: Every variant renders

- **WHEN** a failure alert is rendered for each `OfferFailureReason` variant
- **THEN** each produces a complete message with label, details, next steps, and the reason-appropriate reassurance or ambiguity line

#### Scenario: Platform-rejected amount is explained

- **WHEN** the agent reports the platform refused the offered amount
- **THEN** the failure alert renders `amount_rejected` with the attempted amount and states no offer was sent

---

### Requirement: Offer Outcomes Drive The Keyboard Lifecycle

On tap, the alert's keyboard SHALL immediately repaint to a non-tappable `🟡 Ofertando…` badge. On success, the keyboard SHALL show a terminal non-tappable `💰 Oferta enviada` badge. On any failure or abort, the alert's original rows (including Comprar where previously present) SHALL be restored so the operator can retry — the preflight re-gates every tap. Keyboard restoration SHALL be attempted for every outcome; no outcome may leave the in-flight badge in place.

#### Scenario: In-flight badge during execution

- **WHEN** the offer tap is accepted for execution
- **THEN** the message's keyboard shows only the `🟡 Ofertando…` badge until an outcome lands

#### Scenario: Failure restores a tappable keyboard

- **WHEN** the offer fails with `timeout`
- **THEN** the original keyboard rows are restored and a subsequent tap runs the full preflight again

#### Scenario: Success is terminal

- **WHEN** the offer succeeds
- **THEN** the keyboard shows the `💰 Oferta enviada` badge and the Ofertar row never returns for that listing

---

### Requirement: Live Edits Keep The Offer Surface Truthful

When the alert-update machinery edits a watched alert, the re-rendered body SHALL re-derive the offer line from the listing's current values, and the reconstructed keyboard SHALL reflect current offer eligibility: a price drop that moves a negotiable listing to at-or-under ceiling SHALL re-render it with the standard (Phase 1/Phase 2) body and keyboard for its entry; a sent offer SHALL keep its `💰 Oferta enviada` badge across edits; a reserved flip on a negotiable alert SHALL dead the Ofertar row (restored on flip-back). No edit SHALL be attempted while an offer is in flight.

#### Scenario: Drop into budget upgrades the alert

- **WHEN** a watched negotiable listing's price drops so its buyer total is at or below the entry ceiling
- **THEN** the edited message renders the standard alert body and keyboard (including Comprar when the entry qualifies for Phase 2)

#### Scenario: Sent badge survives edits

- **WHEN** a reserved-flip edit fires on an alert whose offer was already sent
- **THEN** the edited message still shows the `💰 Oferta enviada` badge

---

### Requirement: Offers Respect A Daily Budget

The system SHALL enforce a self-imposed daily offer budget: when the number of successful offer sends in the trailing 24 hours (counted from the `offers` table) has reached `offer.daily_limit` (config, default 5 — deliberately under Wallapop's cap of 10 offers per calendar day per account, leaving headroom for the operator's manual offers), the offer preflight SHALL abort the tap with `daily_limit_reached` before any execution. Independently, when the browser agent reports Wallapop's own exhausted counter, the outcome SHALL also be `daily_limit_reached`; in both cases the rendered message SHALL say which limit was hit and when a retry becomes possible, and the outcome SHALL NOT increment the lockout counter (the offer path is healthy). When the offer form's "ofertas restantes" counter is visible to the agent, its value SHALL be captured into the attempt's audit row.

#### Scenario: Budget spent blocks before execution

- **WHEN** `offer.daily_limit` successful offers were sent within the trailing 24 hours and the operator taps Ofertar
- **THEN** no execution starts, the outcome is `daily_limit_reached`, the keyboard is restored, and the lockout counter is unchanged

#### Scenario: Platform cap reported mid-flow

- **WHEN** the agent reports Wallapop refused the offer because the account's daily offer limit is reached
- **THEN** the outcome is `daily_limit_reached` attributing the platform's limit, and the lockout counter is unchanged

#### Scenario: Budget window rolls

- **WHEN** the oldest of the counted sends becomes older than 24 hours
- **THEN** the next tap passes the budget check again

---

### Requirement: Repeated Offer Failures Lock The Offer Path

Consecutive offer execution failures reaching `offer.lockout_threshold` (config, default 3) SHALL globally disable offer sending until the operator clears the lockout. Safety aborts (`listing_gone`, `reconciliation_tripped`, `duplicate_offer`, `lockout_engaged`, `daily_limit_reached`) SHALL NOT increment the counter; a successful send SHALL reset it. `offer.kill_switch_global = true` SHALL disable offer sending unconditionally. The offer lockout and the Phase 2 circuit breaker SHALL be independent: neither counter's state affects the other path.

#### Scenario: Third failure engages the lockout

- **WHEN** three consecutive offer executions fail with agent errors
- **THEN** the next tap aborts with `lockout_engaged` before any execution

#### Scenario: Offer lockout leaves buys untouched

- **WHEN** the offer lockout is engaged
- **THEN** Phase 2 Comprar taps preflight and execute exactly as before

---

### Requirement: Offers Are Opt-In Per Entry And Armable Via CLI

Wishlist entries SHALL support an `offer:` block (`enabled: bool`, default false; `target_total_eur: Decimal | None`), rejected on unknown fields. The CLI SHALL provide `salvager offer enable <ref>`, `offer disable <ref>`, `offer disable --all`, and `offer status`, mirroring the `phase2` command group; `offer enable` SHALL clear the lockout. With no `offer:` block on any entry, the system's observable behaviour (filtering, rendering, callbacks) SHALL be identical to the pre-feature behaviour.

#### Scenario: Arming an entry

- **WHEN** the operator runs `salvager offer enable <ref>` and restarts the daemon
- **THEN** `offer status` shows the entry enabled with its target, and its Wallapop alerts become offer-eligible

#### Scenario: No opt-in means no change

- **WHEN** no wishlist entry carries an `offer:` block
- **THEN** alert filtering and rendering are byte-identical to pre-feature behaviour

---

### Requirement: Every Offer Attempt Is Audited Append-Only

Every executed offer attempt (success or failure) SHALL append a row to a new `offers` table (migration `0004`): listing id and marketplace, entry key, alert id, offered amount, asking item price at tap time, outcome, failure reason where applicable, screenshot path where available, a `status` column (`sent` in v1), and timestamps. Offer taps SHALL be recorded through the existing callback audit path. Writer methods for `offers` SHALL be INSERT-only (the append-only lint SHALL keep passing); the lockout state SHALL live in a separate single-row mutable `offer_state` table, mirroring `phase2_state`.

#### Scenario: Success and failure both leave rows

- **WHEN** one offer succeeds and a later one times out
- **THEN** the `offers` table contains one row per attempt with amount, outcome, and reason

#### Scenario: Append-only contract holds

- **WHEN** the audit-writer lint inspects the offer writer methods
- **THEN** no update or delete method exists for the `offers` table
