## Why

Wallapop has a native "hacer oferta" flow: a buyer can propose a lower price on a listing, and the seller can accept, reject, or counter. Salvager currently ignores it entirely — the pipeline is binary (alert at or under the ceiling, silence above it), which leaves two kinds of money on the table:

1. **Listings slightly over the ceiling are invisible.** A Corsair kit at 88 € against an 80 € ceiling never alerts, even though a routine 10 % offer would land it inside budget. During burn-in the operator has watched exactly this band of near-miss listings scroll past with no way to act on them.
2. **Even alertable listings are bought at asking price.** The Comprar path pays whatever the seller listed. On a marketplace where haggling is the norm, that is systematically overpaying.

This change gives the operator a **💰 Ofertar** button — the negotiation sibling of Comprar — that sends a bounded, auditable offer through the same safety stack the buy path already proved during burn-in (preflight, reconciliation re-fetch, TinyFish execution, append-only audit, keyboard lifecycle). Wallapop only; eBay Best Offer is explicitly out of scope for v1.

## What Changes

- **New `💰 Ofertar` button on Wallapop alerts.** Operator-tap only, mirroring FR29's no-autonomous-action rule: tap → offer preflight → cross-source reconciliation re-fetch → TinyFish sends the offer → outcome reported to Telegram and audited. No offer is ever sent without a tap.
- **Offer amount is computed, not chosen.** The offer is the highest item price whose buyer total (item + shipping + Protección) still fits the entry's ceiling — i.e. "make this listing fit my budget", derived from the existing `buyer_total_eur` pricing. No new amount-picking UI; the amount is shown on the alert before the operator taps.
- **New "negotiable band" alert.** Wallapop listings whose buyer total is over the ceiling but within a configurable band (default +20 %) stop being silently filtered and instead produce a distinct negotiable alert carrying the Ofertar button (no Comprar — they are over ceiling by definition). Listings beyond the band stay filtered. Alerted (at-or-under-ceiling) Wallapop listings additionally get the Ofertar button when an offer would still undercut the asking price.
- **v1 ends at "offer sent".** The outcome of a successful execution is `💰 Oferta enviada` (alert edited in place, audit row written). Detecting seller acceptance/rejection/counter is out of scope — the operator continues the negotiation in the Wallapop app. The design must not paint v1 into a corner for a future acceptance-detection change.
- **Per-entry opt-in.** A new `offer:` block on wishlist entries (`enabled`, default `false`), mirroring `phase2:`. Entries without it see zero behaviour change — no button, no negotiable alerts, no new filtering semantics.
- **Same guardrails as the buy path, plus a daily budget.** Offer-specific closed failure-reason enum with a full render table, in-flight `🟡 Ofertando…` badge with guaranteed keyboard restore on every outcome, append-only offer audit tables (new migration), offer events wired into the operational-event registry, a per-listing dedupe so the same listing is never offered twice, and a self-imposed daily offer budget (`offer.daily_limit`, default 5 per rolling 24 h) so Salvager never blindly burns Wallapop's own daily offer cap — which the agent also detects and reports distinctly when the platform enforces it first.

## Capabilities

### New Capabilities

- `wallapop-offer-flow`: The end-to-end operator-confirmed offer capability — eligibility (which Wallapop listings render the Ofertar button and when a negotiable-band alert is emitted), the ceiling-derived offer-amount computation, the tap → preflight → reconciliation → TinyFish send pipeline, the `Oferta enviada` terminal state with in-place alert edit, the closed offer failure-reason set with rendered outcomes, keyboard lifecycle across all outcomes, per-listing offer dedupe, and the append-only offer audit trail.

### Modified Capabilities

- `shipping-aware-pricing`: The alert-gate requirement "buyer total over ceiling → filtered" gains a carve-out: Wallapop listings over ceiling but within the configured negotiable band, on offer-enabled entries, are routed to a negotiable alert instead of dropped. Everything else about buyer-total filtering is unchanged (eBay unaffected; over-band unchanged).
- `listing-alert-state-updates`: Keyboard reconstruction after a live edit must preserve offer state — an alert whose offer was already sent keeps its `💰 Oferta enviada` badge (and the negotiable-alert keyboard must survive reserved-flip/price-drop edits the same way the Phase 2 keyboard does). Price-drop edits also re-derive Ofertar eligibility (a drop can move a listing from negotiable band to under-ceiling).

## Impact

**Affected code**:
- `src/salvager/domain/alert.py` — `CallbackVerb.offer`, `BUTTON_LABELS`/`SEVERITY_TOKENS` additions (PRD-amendment-gated sets), Ofertar button rows, negotiable-alert renderer, offer success/failure renderers, `OfferFailureReason` render table.
- `src/salvager/domain/` — offer amount derivation next to `pricing.py`; `OfferFailureReason` in `errors.py`; offer audit write-models; `offer:` block in `wishlist.py`.
- `src/salvager/orchestration/` — `offer_orchestrator.py` (sibling of `buy_orchestrator.py`), offer preflight, `callback_handler.py` routing for the `offer` verb, poll-loop alert gate carve-out for the negotiable band, `alert_updater.py` keyboard-reconstruction awareness, `composer.py` wiring.
- `src/salvager/interfaces/` + `src/salvager/adapters/tinyfish_browser/` — offer-send port and a `wallapop_offer.py` TinyFish goal (adapter discipline NFR-M1: the SDK import stays inside the adapter package; payment-rail lint still applies).
- `src/salvager/adapters/sqlite_store/` + `migrations/0004_*.sql` — offer tap/outcome tables, append-only writer methods (the append-only lint test must keep passing).
- `src/salvager/cli/dev_alert_fixtures.py` + `tests/unit/test_dev_emit_alert.py` — new variants; the 45-variant registry pin and snapshot suites grow accordingly.
- `src/salvager/config/config_yaml.py` + `config.example.yaml` (and its byte-identical bundled twin in `src/salvager/templates/`) — `offer:` config section (band percentage, kill switch); `wishlist.example.yaml` — per-entry `offer:` block.

**Affected docs**: PRD gains new FR ids for the offer flow (referenced from the new spec); `README.md` gains an "Ofertas" subsection; `CHANGELOG.md` on release.

**Real-money posture**: an offer is a commitment signal, not a payment — no money moves in v1 (Wallapop charges only when a resulting purchase completes, which stays behind the existing Comprar path). Still gated like Phase 2: per-entry opt-in, operator tap, preflight, circuit-breaker-style lockout on repeated failures, kill switch.

**Backwards compatibility**: with no `offer:` blocks in the wishlist (the default), rendering, filtering, callbacks, and the daemon lifecycle are byte-identical to v0.4.4 behaviour. The rendering re-audit (v1.0 criterion 3) is unaffected until this ships, at which point the new alert surfaces join the audit scope like every prior release.
