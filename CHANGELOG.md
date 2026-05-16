# Changelog

All notable changes to **salvager** land here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
honours [Semantic Versioning](https://semver.org/spec/v2.0.0.html) per
NFR-M4.

## [Unreleased]

Nothing on the wire today. Post-v1 work is described in
[ROADMAP.md](ROADMAP.md) under "Post-launch (deferred)".

---

## [0.2.1] — _pending publish_

Operational patch on top of v0.2.0. No functional changes to the
poll loop, evaluator, alert renderer, or Phase 2 buy path.

**Fixed**

- Docker image now bakes the build-time git short SHA into
  `SALVAGER_COMMIT`, so `salvager version` reports the actual commit
  instead of `unknown` (#1). The release workflow truncates
  `github.sha` to 7 chars and passes it as a `--build-arg`.
- Runtime stage of the image now runs as a non-root `salvager` user
  (UID 1000 by default, overridable at build time via `APP_UID` /
  `APP_GID` build-args), so files the daemon writes to bind-mounted
  `./data` and `./config` volumes are no longer owned by `root` on
  the host (#2). Operators upgrading from v0.2.0 need to
  `chown -R $(id -u):$(id -g)` their existing volume contents once;
  see README "Quick start" for the migration snippet.

**Operator-impacting**

- Bind-mounted volume ownership changes from `root:root` to
  `1000:1000` (or whatever `APP_UID`/`APP_GID` you build with). One-
  time host-side `chown` documented in README.

---

## [1.0.0] — _future, gated on production burn-in of v0.2.0_

Tagging `v1.0.0` requires empirical evidence from running v0.2.0
against real Wallapop + eBay.es traffic in the operator's homelab,
including at least one successful Phase 2 purchase end-to-end with
the safety stack engaging as designed. Until then the codebase is
shipped as v0.2.x with semver 0.x semantics (breaking changes are
allowed without a major bump).

---

## [0.2.0] — _pending publish_

**Phase 1 + Phase 2 feature-complete preview.** All Epic 2-5 code has
shipped + been audited for rendering invariants (UX-DR22/23/32 audit
recorded in [`docs/release-audits/v1.0/SUMMARY.md`](docs/release-audits/v1.0/SUMMARY.md)
— "v1.0" in the path refers to the eventual stability gate; the audit
applies to v0.2.0 today and to v1.0.0 when that ships).

**Not yet validated in production.** The poll loop, the LLM
evaluation, the cross-source reconciliation, the circuit breaker,
the smoke test, and the autonomous purchase have all passed unit +
integration tests + the release-gate manual audit, but the operator
has not yet run the agent against live Wallapop + eBay.es traffic for
a sustained period. v0.2.0 is published so the operator can start
that burn-in window on their own homelab and surface issues that
synthetic tests cannot reach.

Tag: `v0.2.0` → GHCR `ghcr.io/ifuensan/salvager:0.2.0`,
`:0.2`, `:latest` (semver auto-tagging from
`.github/workflows/release.yml`).

Breaking changes between 0.2.x and the next minor/major (0.3.0, 1.0.0,
etc.) are allowed without notice per semver 0.x semantics. Operators
pinning to `:0.2.0` exactly are protected from those.

### Added

- **Phase 2 autonomous-purchase critical path** (Epic 5):
  - SQLite schema v2 with append-only `tap_events`, `transactions`,
    `phase2_smoke_tests` + single-row mutable `phase2_state` (Story 5.1).
  - Phase 2 listing alert renderer + preflight gate — five checks
    (per-entry enabled, max-price ceiling, listing under ceiling,
    confidence ≥ threshold, global lockout / circuit / smoke freshness)
    consulted at alert dispatch AND on every Buy tap (Story 5.2).
  - `BrowserSession` port + `WallapopPayFlow` / `EbayCheckoutFlow`
    TinyFish-driven adapters with a 9-step buy contract and
    fail-closed mapping of every SDK error → typed `BuyFailure`
    (Story 5.3).
  - Cross-source price reconciliation (FR31) + receipt-vs-alert
    reconciliation (FR32) gating the buy on both sides of the
    checkout (Story 5.4).
  - Per-purchase circuit breaker + auto-disable lockout (FR34 /
    FR35) — three consecutive failures opens the breaker, only
    `phase2 enable` clears it (Story 5.5).
  - Daily synthetic smoke test against a fixture set under
    `tests/fixtures/price_parsers/active/` (Story 5.6).
  - `BuyOrchestrator` composing preflight + reconcile + UI check +
    buy + screenshot + audit-write + receipt reconciliation + circuit
    record + Telegram dispatch in a single
    `execute_buy_from_callback` call returning a typed `BuyOutcome`
    discriminated union (Story 5.7).
  - Phase 2 buy success renderer with mandatory-screenshot guard
    (UX-DR9 — Story 5.8) and buy failure renderer with the locked
    reassurance line on every variant (UX-DR10 — Story 5.9).
  - `[🟡 Comprando…]` in-flight keyboard edit + Buy callback handler
    extending the Phase 1 `CallbackDispatcher` (Story 5.10).
  - Six new operational `EventName` variants for the Phase 2 surface
    (`phase2_disabled`, `phase2_re_enabled`,
    `phase2_buy_callback_received`, `phase2_screenshot_missing`,
    `phase2_buy_completion_slow`, `buy_orchestrator_error` — Story 5.11).
  - `salvager phase2 enable / disable / status` CLI commands
    with TTY-gated typing-a-number confirm on `--all` (Story 5.12).
  - `salvager phase2 smoke-test` + `phase2 reconcile` CLI
    commands for operator-driven safety-stack triage (Story 5.13).

- **Release-blocking CI gates**:
  - **Payment-rail enforcement** — AST + per-line lint walks
    `adapters/tinyfish_browser/` and rejects any reference to Bizum,
    transferencia, PayPal, Revolut, bank_transfer or tarjeta_propia
    that is not annotated `verified by payment_rail_lint`
    (Story 5.14, FR25 / NFR-S5).
  - **Per-module 90% line-coverage gate** on the Phase 2 critical
    path (`buy_orchestrator`, `reconciler`, `circuit_breaker`,
    `smoke_test`, `audit_writer`) — fails the build below 90%
    (Story 5.15, NFR-M2).
  - **Snapshot tests + property tests** for every Phase 2 renderer
    and every `BuyFailureReason` variant; reassurance line invariant
    is asserted on every non-`screenshot_missing` failure (Story
    5.16, UX-DR10).

- **Release-audit tooling** (Story 5.17):
  - `salvager dev emit-alert <variant>` fires any of the 37
    locked alert variants against the configured Telegram chat
    (--dry-run prints rendered MarkdownV2 to stdout for inspection).
  - `scripts/dump_audit_snapshots.py` writes one reference `.txt`
    per variant under `docs/release-audits/v1.0/reference-text/`
    for client-variance diffing.
  - `docs/release-checklist.md` documents the 4 Telegram contexts,
    3 colour-blind simulators and macOS VoiceOver pass that gate
    the v1.0 tag.
  - `docs/release-audits/v1.0/SETUP.md` walks through the
    throwaway-bot + audit-chat setup so the production wiring stays
    untouched during the audit window.

### Changed

- `version` in `pyproject.toml` bumped `0.1.0` → `0.2.0`. The
  `salvager version` CLI command surfaces the new value alongside
  the git short SHA.
- README status block reframed: the project is now a "Phase 1 + Phase 2
  feature-complete preview, pending production burn-in" rather than a
  pre-poll-loop skeleton. The recommended pinned tag is `:0.2.0`
  (or pull `:latest` for the most recent release).
- The obsolete "Hermes Agent runs as a remote service…" paragraph in
  README "Architecture" is removed. Hermes was dropped from the v0.x
  scope per the 2026-05-13 design pivot (memory note); scheduling now
  runs in-process via `adapters/asyncio_scheduler/` and TinyFish is
  reached directly via its SDK from `adapters/wallapop_tinyfish/` (Phase
  1 fallback) and `adapters/tinyfish_browser/` (Phase 2 buy flows).

### Security

- Payment-rail boundary structurally enforced: the only payment rails
  the agent can reach are Wallapop Pay and eBay.es checkout. The CI
  lint trips any drift before merge (NFR-S5).
- Mandatory-screenshot guard on Phase 2 buys (UX-DR9): a buy that
  completes without a captured receipt surfaces as
  `BuyFailure(reason=screenshot_missing)` with the alternate
  reassurance line, never as a silent success.

### Project notes

- Schema reaches version 2 (Phase 1 + Phase 2 migrations applied). The
  schema is NOT locked yet — v0.x semantics allow breaking changes;
  the lock kicks in at v1.0.
- Promotion criteria to v1.0.0 (informal, refined as burn-in surfaces
  reality):
  1. At least 2 weeks of v0.2.0 running continuously against
     production Wallapop + eBay.es traffic without unhandled crashes.
  2. At least one Phase 2 purchase completed end-to-end with the
     safety stack engaging or being verified as inert.
  3. No critical rendering regression surfaced between v0.2.0 and
     candidate v1.0.0 (re-audit if any Rich / domain.alert change lands).
- Post-v1.0 deferred items (multi-marketplace expansion, additional
  LLM providers as config-only, the arbitrage-as-separate-repo
  path) live in [ROADMAP.md](ROADMAP.md).

---

## [0.1.0] — 2026-04-XX

Foundation release. Installable skeleton + OSS posture; no marketplace
polling yet. Published to GHCR as `ghcr.io/ifuensan/salvager:0.1.0`.

### Added

- uv-managed Python 3.12+ package with hexagonal directory layout
  (`domain/`, `interfaces/`, `orchestration/`, `adapters/`, `cli/`,
  `config/`, `observability/`).
- CI quality gates: `ruff check`, `ruff format --check`, `ty` + `mypy`
  strict, `pytest`, custom adapter-discipline lint enforcing NFR-M1
  (only `adapters/` may import marketplace SDKs / TinyFish / Hermes /
  python-telegram-bot / httpx).
- Docker image + GHCR release workflow on `v*` tag push.
- Tracked example configuration files (`.env.example`,
  `wishlist.example.yaml`, `config.example.yaml`).
- OSS posture documentation (README, CONTRIBUTING, ROADMAP, LICENSE).
- Structured JSON Lines logging foundation (NFR-O1 / NFR-R5).
- rich-based CLI rendering helpers + locked theme tokens (UX-DR16).
- typer CLI skeleton with the `salvager version` subcommand
  (FR39 / FR48).

---

[Unreleased]: https://github.com/ifuensan/salvager/compare/v0.2.1...HEAD
[1.0.0]: https://github.com/ifuensan/salvager/releases/tag/v1.0.0
[0.2.1]: https://github.com/ifuensan/salvager/releases/tag/v0.2.1
[0.2.0]: https://github.com/ifuensan/salvager/releases/tag/v0.2.0
[0.1.0]: https://github.com/ifuensan/salvager/releases/tag/v0.1.0
