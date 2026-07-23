# salvager — Roadmap

This document names what's planned, what's deferred post-launch, what's permanently out of scope by design, and the documented wind-down triggers for the project.

---

## Where we are

**`v0.2.3` shipped — Phase 1 + Phase 2 feature-complete preview.** All five planned epics' code landed in v0.2.0; v0.2.1 + v0.2.2 were Docker operational patches; v0.2.3 stabilises the Wallapop adapter against the SPA's current production traffic (v3 `/search/section` endpoint, browser-TLS impersonation via `curl_cffi`, transparent JWT refresh, reserved-listing routing) and auto-accepts the ConsentManager cookie banner during `login wallapop`. See [`CHANGELOG.md`](CHANGELOG.md) for the per-patch detail.

- ✓ **Epic 1** (Foundation) — installable skeleton, hexagonal layout, CI gates, Docker image. Shipped as `v0.1.0` in April 2026.
- ✓ **Epic 2** (Wishlist, Config, Credentials) — pydantic v2 schemas, `.env` loader, wishlist YAML round-trip.
- ✓ **Epic 3** (Polling + Adapters) — Wallapop two-path fetcher (unofficial API + TinyFish fallback), eBay API adapter, async scheduler, listing evaluation via Gemini Flash.
- ✓ **Epic 4** (Telegram Surface + CLI Operability) — alert renderer, callback handler, snooze flow, audit log + audit/health/explain CLI commands, operational alert variants.
- ✓ **Epic 5** (Phase 2 Autonomous Purchase + Safety Stack) — Phase 2 listing/buy renderers, TinyFish browser adapter (Wallapop Pay + eBay checkout), cross-source + receipt reconciliation, per-purchase circuit breaker, daily synthetic smoke test, BuyOrchestrator, `phase2 enable/disable/status/smoke-test/reconcile` CLI, payment-rail enforcement CI lint, 90% critical-path coverage gate, release-audit tooling.

**Promotion to `v1.0.0` is gated on production burn-in**, not feature completion. Recommended pinned tag for burn-in: `ghcr.io/ifuensan/salvager:0.5.0`. See [`CHANGELOG.md`](CHANGELOG.md) for the release notes and the `[1.0.0] — future` placeholder spelling out the promotion criteria.

The full epic + story breakdown lives in [`_bmad-output/planning-artifacts/epics.md`](_bmad-output/planning-artifacts/epics.md). The release-gate audit artefact lives in [`docs/release-audits/v1.0/SUMMARY.md`](docs/release-audits/v1.0/SUMMARY.md).

---

## Path to v1.0

The v0.2.x line ships all Phase 1 + Phase 2 code. v1.0.0 is gated on production burn-in, not on additional features.

**Promotion criteria** (informal; tightened if reality requires it):

1. ≥ 2 weeks of the current stable image (`:0.5.0` recommended) running continuously against live Wallapop + eBay.es traffic without unhandled crashes.
2. ≥ 1 Phase 2 purchase completed end-to-end (or one verified Phase 2 abort with the safety stack engaging as designed). Counts as "the autonomous-buy path got exercised against the real world, not just synthetic tests".
3. No critical rendering regression surfaced between v0.2.x and the v1.0.0 candidate (re-audit if `domain/alert.py` or the styling layer changes).
4. **OQ3** — measured per-purchase TinyFish Browser cost (NFR-C2 cap is ≤ €1.00). v0.2.x is when this number first appears empirically; v1.0.0 confirms it.
5. **OQ6** — language-register bias check on the first batch of real Telegram alerts. Castilian is the supported locale; Catalan / regional Spanish / Basque listings get best-effort treatment with a README disclosure at v1.

The release-gate audit (Story 5.17) was performed against the v0.2.0 candidate and recorded `RESULT: PASS` in [`docs/release-audits/v1.0/SUMMARY.md`](docs/release-audits/v1.0/SUMMARY.md). The audit applied unchanged through v0.2.3 (v0.2.1–v0.2.2 are Docker-only patches; v0.2.3 changes the Wallapop adapter and the `salvager test-search` CLI table but left `domain/alert.py` + the Telegram alert renderer untouched). **v0.3.0–v0.4.3 break that invariant**: v0.3.0 adds the in-cycle reserved-comp line, v0.3.1 the clickable deep-link row, v0.3.3 the 💶 buyer-total breakdown, v0.3.4 the importación term, v0.4.0 the whole edit surface (banners, price-drop ping, dead-reserved keyboard), v0.4.1 the `listing_gone` buy-failure variant and v0.4.3 the post-outcome keyboard repaint — so per promotion criterion 3 the Story 5.17 rendering / accessibility audit MUST be re-confirmed against the v1.0.0 candidate before promotion. The **code-level re-audit ran 2026-07-19 and PASSED** (see the v0.4.3 delta in the audit SUMMARY: single-escape-pass verified on every new row, zero drift in the pre-existing reference text, snapshot + `dev emit-alert` coverage extended to all 45 variants); v0.4.4 ships that coverage in the image so the remaining work — the operator's on-device capture pass listed there — runs against an image that renders exactly what production dispatches.

**Phase 2 release-gate criteria already met by v0.2.0** (re-stated for completeness):

- Telegram client variance audit (UX-DR32) — passed on Android + Desktop (operator's actual clients).
- Color-blind audit (UX-DR22) — passed on the 3 simulators (Coblis), distinguishability via shape + text preserved.
- VoiceOver accessibility (UX-DR23) — passed with documented limitation ([docs/accessibility.md](docs/accessibility.md)).
- ≥ 90% line coverage on Phase 2 critical-path modules (NFR-M2) — enforced by CI.

---

## Post-launch (deferred)

**Multi-marketplace expansion.** Additional Spanish marketplaces (Milanuncios, Vibbo) or international (eBay.com, Adevinta network sites). Architecture is marketplace-agnostic; adding a marketplace = a new adapter in `src/salvager/adapters/<name>/` plus a fixture in `tests/fixtures/`. No commitments on timeline; depends on whether Wallapop + eBay.es cover the wishlist usage adequately. The (c3) scope contract caps v1 at the two named marketplaces deliberately.

**PyPI publication.** Currently the only distribution channel is the GHCR Docker image. A PyPI publication (`pip install salvager`) is post-launch nice-to-have; depends on demand from forkers running the agent outside Docker (e.g., directly on a homelab host without containers).

**`config.yaml > telegram.locale = en-US` and other locales.** The Telegram surface is Spanish-only at v1 per UX-DR27. Adding English (or other) Telegram strings post-launch requires the locale flag wired through `domain/alert.py` renderers and a parallel string table. Tracked as OQ.

**agentskills.io publication.** Hermes ecosystem visibility — see OQ8 in the PRD. Decision deferred until post-v1; depends on whether v1 hits its success criteria and whether the Hermes community shows interest in the wishlist-anchored evaluation pattern.

**Bilingual asymmetry CI lint.** Today the Spanish-Telegram + English-CLI split is enforced by code review only. A future CI lint that asserts Telegram-bound strings are Spanish (and the inverse for code / log / CLI strings) would be a useful guardrail. Low priority.

**LLM provider auto-switch on rate-limit.** Provider is config-driven (`llm.provider` in `config.yaml`) and the adapter pattern supports swapping at startup; auto-switch mid-run on rate-limit is out of scope for v1. Operator handles by editing `config.yaml` and restarting.

**External observability integration.** Logs go to stdout (docker-compose-captured) at v1; no Grafana / Loki / external sink shipped. Operators wire their own log forwarder if they want one.

---

## Future-research repo (separate)

**`salvager-research`** is the documented home for arbitrage-flavored experimentation. This separate repo will:

- Carry the same MIT license + the same adapter discipline.
- Have a different name + framing (focused on quantitative resale analysis, not on monitoring + buying).
- Be a separate code base that the user explicitly forks/installs, not a feature flag on the main repo.
- Not ship at the same time as `salvager v1`; it's a research repo with no v1 commitment.

The fork separation is a structural choice per the (c3) scope contract. `salvager` the tool will never carry arbitrage features in mainline; if you need that capability, the future-research repo is the path.

Planned URL: https://github.com/ifuensan/salvager-research (stub, not yet created).

---

## Permanently out of scope

These are not "not yet" — these are "never, by design":

- **Web dashboard or any browser-facing UI.** Telegram + CLI are the user surfaces. Adding a web UI would inflate maintenance and conflict with the (c3) "personal homelab tool" framing.
- **Multi-user / multi-tenant operation.** One operator, one homelab, one Telegram chat ID. The chat-ID allowlist (AR20) silently drops messages from any other chat.
- **Cloud-hosted SaaS mode.** All data plane is local (NFR-PR3); credentials never leave the operator's host. There is no remote-storage codepath, and the architecture forbids one.
- **Arbitrage scoring / resale margin / price prediction.** Structurally prevented at the schema layer (FR3) and the prompt layer (FR17). See `CONTRIBUTING.md` "No arbitrage PRs".
- **Fully autonomous purchase without a Telegram tap.** FR29 — there is no setting, flag, environment variable, or CLI command that bypasses the per-purchase Telegram tap. This is non-negotiable.
- **Alternative payment rails.** FR25 + NFR-S5: only Wallapop Pay and eBay.es checkout. The payment-rail CI lint (Story 5.14) deny-lists `bizum`, `transferencia`, `paypal`, etc.

---

## Walk-away triggers (sustainability)

This is a single-maintainer project. Two documented triggers wind it down:

**Technical-debt walk-away.** When a marketplace UI / API change breaks an adapter and the operator cannot restore service within ≤ 30 hours of patch effort across ≤ 3 attempts, the affected adapter is considered upstream-hostile and is mothballed (with an operational alert + a ROADMAP addendum) rather than perpetually patched. If both Wallapop adapter paths hit this threshold simultaneously, the project is wound down via the graceful off-ramp procedure below.

**Sustained-burden walk-away.** When the rolling 3-month maintenance budget exceeds 20 hours/month sustained, the project is wound down via the graceful off-ramp procedure:

1. Pin all dependencies to known-working versions in `pyproject.toml` and commit `uv.lock`.
2. Tag a final release (e.g., `v1.x-sunset`).
3. Add a README addendum naming the wind-down reason.
4. Archive the repository on GitHub.
5. Encourage forks via the MIT license; the future-research repo path remains open separately.

This is documented up-front. Users running salvager accept that this is a real possibility for any solo-maintained tool that interacts with a marketplace it does not own.

---

## C&D-induced sunset

If salvager receives a credible legal notice (cease-and-desist or similar) from any party, the project will be wound down via the graceful off-ramp procedure above. The repository will be archived; no anti-C&D circumvention will be attempted (per the legal posture in the PRD's §Legal Disclaimer and in the README).

This is a documented possible end state for any tool that interacts with a marketplace it does not own. Operators running salvager accept this risk.
