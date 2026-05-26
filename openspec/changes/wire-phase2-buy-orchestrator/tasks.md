## 1. Discovery — confirm the inventory and existing wiring

- [x] 1.1 Confirm `DegradationReporter` instantiation state in `compose_daemon` today. — Already built at `composer.py:161` as `reporter`. Reuse for both CircuitBreaker.reporter and BuyOrchestrator.reporter.
- [x] 1.2 Confirm `Phase2StateReader` constructor signature. — Concrete class is `SqlitePhase2StateReader`, constructor `__init__(self, db_path: str | Path)`.
- [x] 1.3 Locate Wallapop / eBay `PageFetcher` instances. — Built inside `_build_wallapop_path`/`_build_ebay_path` as locals (not exposed). Plan: build separate instances for the Reconciler dispatch wrapper — fetchers are stateless and cheap, no refactor needed.

## 2. Marketplace-dispatching wrappers

- [x] 2.1 Add `src/salvager/adapters/tinyfish_browser/marketplace_dispatch.py` containing `MarketplaceDispatchingBrowser(BrowserSession)`. Constructor takes `wallapop: WallapopPayFlow, ebay: EbayCheckoutFlow`. `execute_buy(listing, max_price_eur)` dispatches on `listing.marketplace` to the corresponding inner adapter. Raise `ValueError` with a clear message if `marketplace` is unknown.
- [x] 2.2 In the same module, add `MarketplaceDispatchingPageFetcher(PageFetcher)` analogous to the browser wrapper. Constructor takes `wallapop: PageFetcher, ebay: PageFetcher`. Methods dispatch on `listing.marketplace`. Used by `Reconciler.cross_source_fetcher`. `fetch(url)` dispatches by URL host since the listing isn't in scope at that callsite.
- [x] 2.3 Unit tests in `tests/unit/test_marketplace_dispatch.py`: 7 cases covering browser+search dispatch by marketplace, fetch dispatch by URL host, and ValueError on unknown marketplace/host.

## 3. WishlistLoader closure

- [x] 3.1 Inside `compose_daemon`, define a `wishlist_loader` callable matching the type alias `Callable[[EntryKey], WishlistEntry | None]`. Implementation: re-read the YAML file at `wishlist_path` on call, parse, look up by `EntryKey`, return the entry or `None`. → `_make_wishlist_loader` in composer.py.
- [x] 3.2 Add a 1-entry mtime-keyed cache so unchanged-file calls short-circuit without re-parsing. → Manual `dict[float, Wishlist]` cleared-and-set on mtime change inside `_make_wishlist_loader`.
- [x] 3.3 Unit test that the loader returns the up-to-date entry after the file is rewritten between two calls — 2 tests in `tests/unit/test_composer.py` covering mtime-cache invalidation + unknown-entry → None.

## 4. Compose the BuyOrchestrator dependencies

- [x] 4.1 In `compose_daemon` build the per-marketplace adapters needed for the dispatch wrappers: `WallapopPayFlow(api_key=env.TINYFISH_API_KEY)`, `EbayCheckoutFlow(api_key=env.TINYFISH_API_KEY)`. Both browsers only need the TinyFish key; marketplace login happens inside the browser session.
- [x] 4.2 Build `MarketplaceDispatchingBrowser(wallapop=..., ebay=...)`. Both sides always built — the dispatching wrapper raises `ValueError` only if `listing.marketplace` is neither, which can't happen since listings come from real fetchers that set the field correctly.
- [x] 4.3 Build `Phase2AuditWriter(db_path_under(data_dir))` and `SqlitePhase2StateReader(db_path_under(data_dir))`.
- [x] 4.4 Build `Phase2Preflight(state_reader=..., circuit_breaker_threshold=config.phase2.circuit_breaker_threshold)` — actual dataclass takes the threshold, not the wishlist_loader (loader goes only to BuyOrchestrator).
- [x] 4.5 Build the cross-source fetcher dispatch wrapper with fresh per-marketplace fetcher instances (cheap, stateless duplicates); build `Reconciler(cross_source_fetcher=..., tolerance_eur=config.phase2.reconciliation_tolerance_eur, tolerance_pct=config.phase2.reconciliation_tolerance_pct)`.
- [x] 4.6 Reuse the already-built `DegradationReporter` per task 1.1 — same instance passed to both `CircuitBreaker.reporter` and `BuyOrchestrator.reporter`.
- [x] 4.7 Build `CircuitBreaker(audit_writer=..., state_reader=..., reporter=..., threshold=config.phase2.circuit_breaker_threshold)` — no `cooldown` field in the actual dataclass; the breaker is stateless and the threshold gates it.
- [x] 4.8 Assemble `BuyOrchestrator(preflight=..., reconciler=..., browser=..., circuit_breaker=..., audit_writer=..., telegram_surface=composed.telegram, store=composed.store, reporter=..., wishlist_loader=...)`. Clock default is fine.
- [x] 4.9 Replace `buy_orchestrator=None` in `CallbackDispatcher(...)` with the new instance. Dropped the "Wiring lands in a follow-up PR" comment.

## 5. Tests

- [x] 5.1 Composer test extended in `tests/unit/test_composer.py` — asserts `composed.dispatcher._buy_orchestrator` is a `BuyOrchestrator` with all 9 typed fields populated and the right reuse of `telegram`/`store`.
- [x] 5.2 Marketplace dispatch tests done in task 2.3.
- [x] 5.3 WishlistLoader test done in task 3.3.
- [ ] 5.4 SKIP — dispatcher routing for `verb="buy"` is already covered in `tests/unit/test_callback_handler.py` (predates this change); the only delta here is that `_buy_orchestrator` is now non-None, which the composer test covers. Adding another e2e test would be redundant.
- [x] 5.5 Negative test `test_both_credentials_missing_raises` already exists in `test_composer.py:145` — passes unchanged, confirming no regression.

## 6. Documentation

- [x] 6.1 Added a "Phase 2: operator-confirmed buy" subsection to `README.md` covering enable flow, 6-step pipeline (snapshot → preflight → reconciliation → checkout → audit → report), circuit-breaker behaviour, and the audit-log query.
- [x] 6.2 `config.example.yaml` (and the bundled template — already in sync) already declares all Phase 2 settings (`kill_switch_global`, `reconciliation_tolerance_eur`, `reconciliation_tolerance_pct`, `circuit_breaker_threshold`, `smoke_test_hour_utc`) with annotated defaults. No edit needed.
- [x] 6.3 `openspec validate wire-phase2-buy-orchestrator` — run below.
