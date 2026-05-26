## Why

Phase 2 — operator-confirmed buy via Telegram — is fully designed and almost fully implemented, but the wiring step was deferred to a follow-up PR that never landed. Today the daemon renders a Phase 2 alert with a "✅ Comprar" button, the operator taps it, the callback handler routes the `buy` verb, the dispatcher reaches for the `BuyOrchestrator`… and finds `None`. It logs `buy_orchestrator_not_wired` and returns. The operator's tap is acknowledged but nothing happens.

The hole is one explicit `buy_orchestrator=None` in `compose_daemon` (`src/salvager/orchestration/composer.py:216`) with a TODO comment saying the wiring is a separate pass. This proposal closes that pass: instantiate `BuyOrchestrator` with its 9 dependencies and pass it to `CallbackDispatcher`. Net result: tapping Comprar actually drives the TinyFish checkout flow and reports the outcome back to Telegram.

This is a wiring change, not an architectural one. There is no auto-buy mode in the codebase and this proposal does not introduce one. The flow stays alert → operator tap → checkout, exactly as it has always been designed.

## What Changes

- Instantiate `BuyOrchestrator` inside `compose_daemon` with all 9 dependencies and pass it to `CallbackDispatcher` instead of `None`. Drop the `buy_orchestrator_not_wired` deferred-PR comment at `composer.py:216`.
- Introduce a small `MarketplaceDispatchingBrowser(BrowserSession)` wrapper that holds a `WallapopPayFlow` + `EbayCheckoutFlow` pair and routes each call to the right adapter based on `listing.marketplace`. The `BuyOrchestrator` interface takes a single `BrowserSession`; the dispatching wrapper is how compose_daemon gives it both marketplaces in one slot.
- Define a `WishlistLoader` closure in compose_daemon that re-reads the wishlist file by `EntryKey` so the orchestrator's preflight sees the operator's latest edits (important if the operator removed or retuned an entry between alert and tap).
- Wire the rest of the deps from already-existing concrete classes: `Phase2Preflight`, `Reconciler`, `CircuitBreaker`, `Phase2AuditWriter`, `Reporter`. The orchestrator's `telegram_surface` and `store` reuse the instances compose_daemon already builds for Phase 1.
- Tests cover the wiring shape (composer hands BuyOrchestrator with 9 deps to the dispatcher) and the marketplace-dispatch wrapper (Wallapop listing → WallapopPayFlow, eBay listing → EbayCheckoutFlow).

## Capabilities

### New Capabilities

- `phase2-purchase-flow`: The end-to-end operator-confirmed buy capability — Phase 2 alert renders with a Comprar button when the entry has `phase2.enabled=true`, the Comprar tap dispatches the buy through the orchestrator's nine collaborators, every attempt + outcome is written to the Phase 2 audit log, the result is reported back to Telegram, and the circuit breaker halts further buys once consecutive failures cross the configured threshold. This capability's contract is what every future change to Phase 2 will modify (auto-buy gating, batched buys, multi-marketplace strategy, etc.).

### Modified Capabilities

<!-- None — phase2-purchase-flow is brand new. observability stays untouched. -->

## Impact

**Affected code**:
- `src/salvager/orchestration/composer.py` — instantiate the 9 deps + the dispatching browser + the wishlist loader closure; pass the assembled `BuyOrchestrator` to `CallbackDispatcher`. Drop the `buy_orchestrator=None` line and its comment.
- `src/salvager/adapters/tinyfish_browser/` — add `marketplace_dispatch.py` containing the `MarketplaceDispatchingBrowser` wrapper. ~30 LOC, no third-party dependencies.
- Possibly `src/salvager/orchestration/buy_orchestrator.py` — only if the wiring surfaces a constructor signature gap. Default expectation: zero changes.

**Affected config**:
- Phase 2 settings already exist in `config.yaml` (circuit-breaker threshold, etc.) and per-entry in `wishlist.yaml` (`phase2.enabled`, `phase2.max_price_eur`). Operators who opt entries in will, post-merge, see actual buys executed when they tap Comprar. Operators who keep all entries with `phase2.enabled=false` see zero behaviour change.

**Affected docs**:
- `README.md` — add a "Phase 2: operator-confirmed buy" subsection explaining how to enable an entry, what tapping Comprar does, and how to read the audit log (`salvager audit show --type phase2`).

**Backwards compatibility**: Operators with all entries at `phase2.enabled=false` (the current default for every entry in the example wishlist) see no behaviour change. Phase 1 alerts, callbacks, and the daemon lifecycle are untouched.

**Out of scope**:
- No auto-buy mode. Phase 2 stays operator-confirmed.
- No new buy adapters or TinyFish features. The Wallapop Pay + eBay checkout flows are already implemented.
- No changes to alert renderers, callback verb set, or the callback handler's routing logic.
- No reconciliation algorithm changes.
- Per-entry Phase 2 enablement (`phase2 enable <entry>` CLI) is already implemented and continues to work unchanged.
