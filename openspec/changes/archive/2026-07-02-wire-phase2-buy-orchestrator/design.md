## Context

The repo has a fully-implemented `BuyOrchestrator` (`src/salvager/orchestration/buy_orchestrator.py:143-168`) that drives one Phase 2 buy from snapshot lookup → preflight → reconciliation → browser checkout → audit → operator-facing report. It expects 9 collaborators in its constructor. `compose_daemon` currently builds none of them and passes `buy_orchestrator=None` to the `CallbackDispatcher`, with a comment dating that decision to "a separate composition pass". This change is that pass.

Phase 1 is healthy in production: poll loop, alerts, callbacks (view/skip/snooze), audit writer, store, observability are all running. Phase 2's alert renderer (`render_phase2_listing_alert` with `_phase2_button_row`) is already used by the poll loop when an entry's `phase2.enabled=true`, so a Comprar button reaches the operator today — it just no-ops on tap.

The `BuyOrchestrator` does not auto-buy. It exists only to fulfil an operator's explicit Comprar tap. There is no batch tick, no scheduler entry, no autonomous trigger anywhere in the orchestrator's path; `execute_buy_from_callback` is the only public method and it takes a `CallbackEvent` produced by an operator tap.

## Goals / Non-Goals

**Goals:**

- One typed `BuyOrchestrator` instance available to the `CallbackDispatcher` after `compose_daemon` returns, so `buy` callback taps actually fire the checkout flow.
- Per-marketplace browser dispatch: a Wallapop listing's Comprar uses `WallapopPayFlow`, an eBay listing's uses `EbayCheckoutFlow`. The orchestrator sees a single `BrowserSession` regardless.
- A `WishlistLoader` closure that reads the current wishlist state when the operator taps, not a snapshot captured at compose time — so an entry the operator removed or edited between alert and tap is respected.
- Composer stays a single declarative file: every new collaborator is built inline in `compose_daemon` (no new module just to host a builder), with one exception (the dispatching browser wrapper, justified below).
- Zero behaviour change for operators who keep every entry at `phase2.enabled=false` (today's example default).

**Non-Goals:**

- No new daemon flow. The buy fires only on operator tap; nothing in this change can trigger a buy autonomously.
- No new alert templates, callback verbs, or routing in the dispatcher. The plumbing from tap → orchestrator already works at the seam; we are filling the orchestrator slot.
- No new TinyFish features or browser-flow changes. `WallapopPayFlow` and `EbayCheckoutFlow` are done and stay untouched.
- No changes to `BuyOrchestrator` itself unless wiring surfaces a constructor mismatch. Default expectation: the orchestrator is read-only here.
- No new Phase 2 audit fields or schema migration. The existing `Phase2AuditWriter` schema is sufficient.
- No CLI changes. `phase2 enable <entry>` already works; the wiring change does not need a new flag.

## Decisions

### Decision 1: Add `MarketplaceDispatchingBrowser` as the BrowserSession slot

`BuyOrchestrator.browser` is typed `BrowserSession` — a single instance. The repo ships two concrete `BrowserSession` implementations (`WallapopPayFlow`, `EbayCheckoutFlow`). We add a tiny adapter that owns both and forwards `execute_buy(listing, …)` to the one matching `listing.marketplace`. Lives at `src/salvager/adapters/tinyfish_browser/marketplace_dispatch.py`, ~30 LOC, no dependencies beyond what the per-marketplace flows already import.

**Alternative considered**: extend `BuyOrchestrator` to take a `dict[Marketplace, BrowserSession]` and dispatch internally. Rejected — touches orchestration code that's already tested and stable, and the orchestrator's "one browser session" abstraction is correct from its point of view. Pushing the dispatch into the composition layer is the cleaner cut.

### Decision 2: `WishlistLoader` is a `lru_cache(maxsize=1)`-wrapped closure reading the YAML, not a snapshot of the in-memory `Wishlist`

The orchestrator's preflight needs the *current* `WishlistEntry` for a given `EntryKey` — not the entry at compose time. If the operator runs `salvager phase2 disable <entry>` between an alert firing and the buy tap, the buy must respect the new state.

Implementation: a closure over the wishlist file path that re-reads + parses on every call (the file is small, the parse is microseconds). A 1-tuple LRU cache on `(mtime, path)` lets us short-circuit when the file hasn't changed, so the common case is one stat + a hash hit.

**Alternative considered**: pass the live `Wishlist` instance compose_daemon already loads. Rejected — that's frozen at startup; an operator edit between startup and tap would be invisible. The cost of re-reading is negligible vs the cost of a stale preflight gate.

### Decision 3: `Reconciler.cross_source_fetcher` is the same marketplace's primary fetcher, not a different one

`Reconciler` takes a `PageFetcher` whose only job here is to re-fetch the listing right before checkout to confirm the displayed price still matches the snapshot within tolerance. Reusing the marketplace's primary fetcher (Wallapop's `WallapopApiFetcher`, eBay's `EbayApiFetcher`) is the natural choice. Like the browser dispatch, this is per-marketplace — but the Reconciler dataclass takes one PageFetcher field, so the same pattern applies: a small dispatching wrapper, or a separate orchestrator instance per marketplace.

After tracing the flow once more, the simplest cut is: the composer builds two `BuyOrchestrator`s (one per marketplace, each with the marketplace's fetcher as its reconciler input) and wraps them behind a `MarketplaceDispatchingOrchestrator`. **This contradicts Decision 1's premise** that the orchestrator stays single. Re-thinking: keep one orchestrator, give it a dispatching browser (Decision 1), and give the Reconciler a dispatching fetcher analogous to the dispatching browser. Both wrappers live in the same `marketplace_dispatch.py` file. The orchestrator stays one instance.

**Alternative considered**: build a third fetcher exclusively for reconciliation (e.g. a head-request-only HTML scrape). Rejected — premature and adds attack surface. The primary fetchers already do what's needed.

### Decision 4: `DegradationReporter` is constructed inside compose_daemon, not lazily

`Reporter` is a Protocol; the concrete `DegradationReporter` needs the logger, the Telegram surface, and a health-state cache. Building it inline near the rest of the buy plumbing keeps the composer easy to read. The same instance is shared between `CircuitBreaker.reporter` and `BuyOrchestrator.reporter`.

### Decision 5: Tests cover the wire shape only; the orchestrator's behaviour stays as already tested

`BuyOrchestrator` has its own unit tests under `tests/unit/test_buy_orchestrator*.py`. This change adds two new test surfaces:

1. A composer test that instantiates `compose_daemon` and asserts the dispatcher's `_buy_orchestrator` is a real `BuyOrchestrator` (not `None`) and that all 9 typed fields are populated. Test seam: a fake env / config / wishlist (the kind the existing composer tests already use).
2. Unit tests for the two `MarketplaceDispatchingBrowser` + `MarketplaceDispatchingPageFetcher` wrappers: feed each a Wallapop and an eBay listing, assert the inner adapter received the call.

No end-to-end "operator taps Comprar → real TinyFish run" test — that would require live credentials and is the domain of the existing `phase2 smoke-test` CLI.

## Risks / Trade-offs

- **[Risk] An operator enables `phase2.enabled` on an entry under the impression that nothing dangerous can happen** (because today it doesn't), and then taps Comprar expecting an alert preview, but instead the TinyFish browser actually runs a checkout. → **Mitigation**: README's new "Phase 2: operator-confirmed buy" section spells out exactly what tapping Comprar does; the CLI's `phase2 enable` already prints a warning per FR-spec; the per-entry `max_price_eur` ceiling is enforced by preflight + reconciler so a runaway price won't cross the operator's stated limit even if the live price has moved.
- **[Risk] The dispatching wrappers leak listings to the wrong adapter** (Wallapop listing to eBay checkout flow, etc.). → **Mitigation**: the dispatch key is `listing.marketplace` which is set by the fetcher at ingest time and never mutated; the unit tests on the wrappers enumerate both marketplaces.
- **[Risk] `WishlistLoader` re-parses the YAML on a hot path and adds latency to every buy tap**. → **Mitigation**: YAML parse is sub-millisecond at this file size; the 1-entry mtime-keyed LRU short-circuits unchanged-file calls. Operator-perceived buy latency is dominated by the TinyFish run (~30-60 s), not the parse.
- **[Trade-off] Two dispatching wrappers (browser + reconciler fetcher) instead of one** because the orchestrator takes both. → Accepted — the alternative (multi-orchestrator design with a top-level dispatcher) is a bigger refactor for the same operational outcome.

## Migration Plan

Single-deploy, no schema migration:

1. Merge the change. `compose_daemon` now produces a wired `BuyOrchestrator`.
2. Operators with all entries at `phase2.enabled=false` (default) see no behaviour change — their alerts are still Phase 1 only, no Comprar button rendered.
3. To exercise the new flow: operator runs `salvager phase2 enable <entry>` on a wishlist entry, the next matching listing triggers a Phase 2 alert with the Comprar button, and tapping fires the buy through the orchestrator.
4. Rollback: revert the composer change and ship — the dispatcher falls back to logging `buy_orchestrator_not_wired` and the operator's tap no-ops as before.

## Open Questions

- Whether the existing `DegradationReporter` already gets instantiated in `compose_daemon` for Phase 1's degraded-fetch path. If yes, reuse the same instance; if no, this change builds it. Trivial to confirm during implementation; no architectural impact either way.
