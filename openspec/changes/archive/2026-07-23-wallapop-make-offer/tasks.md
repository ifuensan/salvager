## 1. Domain: amount, settings, errors

- [x] 1.1 Add `offer_item_price_eur(listing, target, *, assumed_shipping_eur)` next to `domain/pricing.py` — largest whole-euro item price whose buyer total fits the target; returns `None` when not strictly below the asking price or ≤ 0 — with unit tests covering the band case, the under-ceiling default-target case (no offer), a lower per-entry target, the 70 %-of-asking platform floor (no offer below it), the Protección 13 € boundary, and unknown shipping (buffer)
- [x] 1.2 Add `OfferSettings` (`enabled: bool = False`, `target_total_eur: Decimal | None`, `extra="forbid"`) to `domain/wishlist.py` as `WishlistEntry.offer`, mirroring `Phase2Settings`; update `wishlist.example.yaml` and wishlist-yaml tests
- [x] 1.3 Add closed `OfferFailureReason` enum (12 variants per spec) to `domain/errors.py` with `@enum.unique`
- [x] 1.4 Add `OfferConfig` (`band_pct = 0.20`, `daily_limit = 5`, `lockout_threshold = 3`, `kill_switch_global = False`) to `config/config_yaml.py` as `ConfigModel.offer`; update `config.example.yaml` AND its byte-identical bundled twin `src/salvager/templates/config.example.yaml`; extend config tests

## 2. Persistence: migration 0004 + writers

- [x] 2.1 Write `migrations/0004_offer_schema.sql`: append-only `offers` table (listing id, marketplace, entry key, alert id, offered amount, asking price at tap, outcome, failure reason, screenshot path, `platform_remaining` nullable — the "ofertas restantes" counter when the agent saw it, `status` default `'sent'`, timestamps) + single-row mutable `offer_state` (`CHECK id=1`: `globally_disabled`, `disabled_reason/at`, `consecutive_failures`)
- [x] 2.2 Add INSERT-only `record_offer_attempt` plus `offer_state` counter/lockout methods to the sqlite audit writer, and a `has_successful_offer(marketplace, listing_id)` reader; verify `test_audit_writer_append_only.py` still passes (no `update_*`/`delete_*` on `offers`)
- [x] 2.3 Migration test on a copy of a real-schema DB: 0004 applies cleanly over 0003, existing rows untouched

## 3. Pipeline: negotiable band carve-out

- [x] 3.1 Extend the over-ceiling filter in `poll_loop` with the single carve-out (Wallapop + `offer.enabled` + buyer total ≤ ceiling × (1 + `offer.band_pct`) → keep, tagged negotiable); over-band / offer-disabled / eBay unchanged; thread the negotiable tag through evaluation to render time
- [x] 3.2 Tests: in-band kept and tagged, over-band filtered, eBay and offer-disabled entries byte-identical to current behaviour, confidence gate still applies to band listings, Phase 2 buy gate still rejects negotiable (over-ceiling) listings

## 4. Rendering: alerts, buttons, failure table

- [x] 4.1 Add `CallbackVerb.offer`, the `💰 Ofertar` / `🟡 Ofertando…` / `💰 Oferta enviada` labels and the negotiable severity token to the locked sets in `domain/alert.py`; record the PRD amendment (new FR ids for the offer flow) in `_bmad-output/planning-artifacts/prd.md` and reference them from the spec
- [x] 4.2 Implement the negotiable-alert renderer (distinct token, buyer-total breakdown, offer line with amount + target, Ofertar row, no Comprar) within the photo-caption cap
- [x] 4.3 Append the Ofertar row to eligible Phase 1/Phase 2 Wallapop alerts (eligibility: offer-enabled entry + computed amount + no prior successful offer); eBay and offer-disabled rendering byte-identical (existing snapshots must not change)
- [x] 4.4 Implement offer success renderer (`💰 Oferta enviada`, amount, next-steps pointing at the Wallapop app for the negotiation) and the failure renderer with the full `OfferFailureReason` render table (labels, details, next steps, "No se ha enviado ninguna oferta." reassurance; ambiguity copy for `screenshot_missing`)
- [x] 4.5 Extend `cli/dev_alert_fixtures.py` VARIANT_REGISTRY with every new variant (negotiable listing shapes, offer success, 12 failures, any new operational events); update the 45-variant count pins in `test_dev_emit_alert.py` (with the derivation comment) and regenerate golden snapshots — fixtures built FROM the registry, Sonar-safe

## 5. Adapter: TinyFish offer goal

- [x] 5.1 Verify the WEB "hacer oferta" flow against the operator's app captures in `captures/` (app rules already pinned: listing-page button, 70 %-of-asking floor, 10/day counter, whole-euro amounts): confirm the web UI exposes the same flow and sent-state confirmation, and whether the amount field accepts cents — the remaining open questions in design.md, before goal authoring
- [x] 5.1b Check whether the Wallapop search/detail payload exposes a PRO-seller or offer-eligibility flag; if yes, pre-filter offer eligibility (no Ofertar button / no negotiable alert on ineligible listings) and cover with schema tests
- [x] 5.2 Add an `execute_offer(listing, amount_eur) -> OfferResult` port (either on `BrowserSession` or a sibling `OfferSession` protocol — decide against the walkthrough) and `adapters/tinyfish_browser/wallapop_offer.py` with the goal contract (exact-amount clause, sent-state verification, screenshot) + an `OFFER_OUTPUT_CONTRACT` mirroring the buy one; map agent outcomes → `OfferFailureReason`; `tinyfish` import stays in the adapter package; payment-rail lint passes
- [x] 5.3 Unit tests with a faked TinyFish client: success, each mapped failure outcome, malformed agent payload → `marketplace_error`

## 6. Orchestration: preflight, orchestrator, callbacks

- [x] 6.1 Implement `OfferPreflight` (offer-enabled per fresh wishlist read, kill switch, lockout, daily budget from the trailing-24h `offers` count, not reserved, no prior successful offer) with stable string reasons, cheap→expensive ordering like `Phase2Preflight`
- [x] 6.2 Implement `orchestration/offer_orchestrator.py` (sibling of `BuyOrchestrator`): snapshot lookup → preflight → reconciliation re-fetch by internal id (404 → `listing_gone`, no lockout increment) → recompute amount from fresh listing (drift beyond `phase2.reconciliation_tolerance_*` or amount ≥ asking → `reconciliation_tripped`) → `execute_offer` → append `offers` row → lockout `record_outcome` (aborts don't count, success resets) → dispatch outcome alert → keyboard restore/terminal badge on EVERY path
- [x] 6.3 Route the `offer` verb in `callback_handler.py`: HANDLED_VERBS, audit-first contract, `🟡 Ofertando…` repaint, tracked background task set (the `_buy_tasks` pattern)
- [x] 6.4 Extend `alert_updater.reconstruct_keyboard` + edit-skip logic: skip under in-flight offer (suppression window like buy), preserve `Oferta enviada` badge, dead/restore Ofertar on reserved/flip-back, re-derive offer line + eligibility on price edits (drop into ceiling → standard alert surface)
- [x] 6.5 Wire everything in `composer.py` (orchestrator deps, offer writer/reader, dispatching browser gains the offer flow)
- [x] 6.6 Orchestrator/integration tests: happy path, every abort path, lockout engage at threshold + independence from the Phase 2 circuit breaker, daily budget blocks the N+1th tap and rolls off after 24 h (no lockout increment), keyboard restored on every non-success outcome, dedupe blocks a second tap

## 7. CLI

- [x] 7.1 Add the `salvager offer` command group: `enable <ref>` (clears lockout), `disable <ref>`, `disable --all`, `status` — mirroring `phase2` semantics incl. non-TTY behaviour; exit-code tests
- [x] 7.2 Decide (per design open question) whether `audit show` grows `--type offer` now; if yes, wire the `offers` table into `audit_cmd.py`, else record the deferral in the change notes

## 8. Docs & release readiness

- [x] 8.1 README "Ofertas" subsection: how to opt in, what the negotiable alert means, what `Oferta enviada` does and does NOT promise (v1 has no acceptance detection; follow through in the Wallapop app)
- [x] 8.2 Full gate: ruff/format/mypy clean, pytest green (the 2 known `/app` sandbox fails excepted), `openspec validate --strict`, snapshot suites regenerated deliberately (no drift in pre-existing reference texts — re-audit posture), variant-count pins consistent
- [x] 8.3 Note for the release PR: new alert surfaces enter the v1.0 criterion-3 audit scope; plan the capture additions (negotiable alert + offer outcome variants) in `docs/release-audits/`
