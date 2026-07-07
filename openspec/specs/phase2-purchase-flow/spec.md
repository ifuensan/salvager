# phase2-purchase-flow Specification

## Purpose
TBD - created by archiving change wire-phase2-buy-orchestrator. Update Purpose after archive.
## Requirements
### Requirement: Phase 2 Alert Renders Comprar Button For Opted-In Entries Only

When the daemon's poll cycle produces an alert for a listing whose wishlist entry has `phase2.enabled=true`, the rendered Telegram alert SHALL include a "✅ Comprar" inline button. When the entry has `phase2.enabled=false` (the default), the alert SHALL render with only the Phase 1 button row (View / Skip / Snooze) and NO Comprar button.

#### Scenario: Phase 2 entry produces a buyable listing

- **WHEN** the poll cycle evaluates a listing under an entry where `phase2.enabled=true` and the LLM confidence meets the entry's threshold
- **THEN** the alert sent to Telegram includes a row with a "✅ Comprar" button
- **AND** the button's `callback_data` matches `<surface>:buy:<alert_id>` per the locked callback format

#### Scenario: Phase 1-only entry produces an alert

- **WHEN** the poll cycle evaluates a listing under an entry where `phase2.enabled=false`
- **THEN** the alert sent to Telegram includes only the Phase 1 button row
- **AND** no callback button with verb `buy` is present

---

### Requirement: Comprar Tap Drives The Buy Orchestrator

When the operator taps a Comprar button on a Phase 2 alert, the daemon SHALL dispatch the resulting `CallbackEvent` to a fully-wired `BuyOrchestrator` instance. The orchestrator's `execute_buy_from_callback` method SHALL be invoked with the event. The daemon SHALL NOT log `buy_orchestrator_not_wired` or otherwise no-op the tap.

#### Scenario: Operator taps Comprar with the orchestrator wired

- **WHEN** an operator taps the "✅ Comprar" button on an open Phase 2 alert
- **THEN** the callback dispatcher routes the event to the wired `BuyOrchestrator` instance
- **AND** the dispatcher does not emit the `buy_orchestrator_not_wired` log event

#### Scenario: Daemon starts with all Phase 2 dependencies satisfied

- **WHEN** `compose_daemon` runs against valid env, config, and wishlist inputs
- **THEN** the returned `ComposedDaemon`'s callback dispatcher holds a `BuyOrchestrator` instance (not `None`)
- **AND** every typed field on that orchestrator (`preflight`, `reconciler`, `browser`, `circuit_breaker`, `audit_writer`, `telegram_surface`, `store`, `reporter`, `wishlist_loader`) is populated with a non-null collaborator

---

### Requirement: Browser Session Routes By Listing Marketplace

The `BrowserSession` instance passed to `BuyOrchestrator` SHALL select the per-marketplace checkout flow based on the listing's `marketplace` field at call time. A Wallapop listing's `execute_buy` SHALL invoke the Wallapop Pay flow; an eBay listing's `execute_buy` SHALL invoke the eBay checkout flow.

#### Scenario: Wallapop listing reaches Wallapop Pay

- **WHEN** the orchestrator calls `browser.execute_buy(listing, max_price_eur)` with a listing whose `marketplace == "wallapop"`
- **THEN** the Wallapop Pay flow's `execute_buy` is the implementation that runs
- **AND** the eBay checkout flow's `execute_buy` is not invoked

#### Scenario: eBay listing reaches eBay checkout

- **WHEN** the orchestrator calls `browser.execute_buy(listing, max_price_eur)` with a listing whose `marketplace == "ebay"`
- **THEN** the eBay checkout flow's `execute_buy` is the implementation that runs
- **AND** the Wallapop Pay flow's `execute_buy` is not invoked

---

### Requirement: Wishlist Loader Reflects Operator Edits Between Alert And Tap

The `wishlist_loader` collaborator the `BuyOrchestrator` receives SHALL resolve an `EntryKey` against the current wishlist file contents at the moment of the buy tap. An entry the operator removed, edited, or whose `phase2.enabled` flag the operator flipped between the alert dispatch and the Comprar tap SHALL be observed in that updated state by the orchestrator's preflight.

#### Scenario: Operator disables a Phase 2 entry between alert and tap

- **WHEN** the daemon sends a Phase 2 alert for an entry, and the operator then sets that entry's `phase2.enabled=false` in `wishlist.yaml` before tapping Comprar
- **WHEN** the operator subsequently taps Comprar on the still-displayed alert
- **THEN** the orchestrator's preflight sees `phase2.enabled=false` and aborts the buy with the corresponding ineligibility reason
- **AND** no checkout flow runs

#### Scenario: Operator removes a Phase 2 entry between alert and tap

- **WHEN** the operator deletes a wishlist entry that previously matched a still-open alert
- **WHEN** the operator taps Comprar on that alert
- **THEN** the orchestrator's preflight observes that the `EntryKey` no longer resolves to any entry
- **AND** the buy is aborted before any checkout flow runs

---

### Requirement: Circuit Breaker Halts Further Buys After Consecutive Failures

When the configured number of consecutive Phase 2 buy attempts fail, the circuit breaker SHALL open. With the breaker open, subsequent Comprar taps SHALL abort the buy before any checkout flow runs. The breaker has no time-based auto-recovery: it SHALL remain open until the operator explicitly lifts the lockout via `salvager phase2 enable <entry>` (which calls `Phase2AuditWriter.clear_global_disable`). A Phase 2 success while the breaker is open still resets the consecutive-failure counter, but the global-disable flag is independent and only the operator-action path clears it.

#### Scenario: Failure threshold reached opens the breaker

- **WHEN** the orchestrator has recorded `N` consecutive failed buy outcomes where `N` equals the configured threshold
- **WHEN** the operator taps Comprar on a fresh Phase 2 alert
- **THEN** the orchestrator aborts the buy without invoking the browser
- **AND** the abort reason in the audit row reflects `circuit_breaker_open`

#### Scenario: Successful buy resets the failure streak

- **WHEN** a buy outcome is `success` while the breaker is closed
- **THEN** the consecutive-failure counter resets so the breaker remains closed on subsequent taps

---

### Requirement: Every Buy Attempt And Outcome Persists To The Phase 2 Audit Log

For every Comprar tap that reaches the orchestrator, the orchestrator SHALL write an audit row capturing the inputs (alert id, listing id, entry key, marketplace, price snapshot), the outcome (success / failure / aborted with reason), and the timestamps. A buy that is aborted at preflight or by the breaker SHALL still produce an audit row recording the abort reason. The audit log is append-only.

#### Scenario: Successful buy writes a success audit row

- **WHEN** a buy completes successfully with a receipt id and price paid
- **THEN** the orchestrator writes one new audit row marking the transaction as `success` with the receipt id and the actual price paid
- **AND** prior audit rows for the same alert remain unchanged

#### Scenario: Aborted buy writes an aborted audit row

- **WHEN** a buy is aborted at preflight or by the circuit breaker
- **THEN** the orchestrator writes one new audit row marking the transaction as `aborted` with the specific reason
- **AND** no transaction row marks the buy as `success`

---

### Requirement: Buy Outcome Is Reported Back To The Operator In Telegram

After every buy attempt that reaches the browser flow, the orchestrator SHALL send a follow-up message to the operator's Telegram chat summarising the outcome — success with receipt id and price paid, failure with the failure reason, or aborted with the abort reason. This follow-up is in addition to the audit row; the audit log captures structured state for queries, while the Telegram report keeps the operator in the loop without needing to open the CLI.

#### Scenario: Successful buy reports back

- **WHEN** the browser flow returns a `BuySuccess`
- **THEN** a Telegram message is sent to the operator's configured chat
- **AND** the message contains the receipt id and the actual price paid

#### Scenario: Failed buy reports back

- **WHEN** the browser flow returns a `BuyFailure`
- **THEN** a Telegram message is sent to the operator's configured chat
- **AND** the message identifies the failure reason in operator-readable form

