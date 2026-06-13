# Changelog

All notable changes to **salvager** land here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
honours [Semantic Versioning](https://semver.org/spec/v2.0.0.html) per
NFR-M4.

## [Unreleased]

Nothing on the wire today. Post-v1 work is described in
[ROADMAP.md](ROADMAP.md) under "Post-launch (deferred)".

---

## [0.3.1] — 2026-06-13

Surfaced during the v0.3.0 burn-in: the operator had no way to click
through from a Telegram alert to the actual listing to validate it.

**Clickable deep link on listing alerts (closes FR18)**

- Every Phase 1 and Phase 2 listing alert now carries a
  `🔗 Ver anuncio en <Marketplace>` row — a MarkdownV2 inline link to
  `listing.url` — rendered immediately after the `📍` location row. FR18
  always required this "deep link to the listing"; it had never been
  implemented (the `👁 Ver` button only records a "visto" audit row and
  does not surface the URL). The Ver button, its callback, the keyboard,
  and the persisted snapshot are unchanged. The link target escapes only
  `\` and `)` so query strings (`?a=1&b=2`) keep working (#25).

---

## [0.3.0] — 2026-06-12

First release of the burn-in window. The headline is that the **Phase 2
autonomous-purchase loop is now wired end-to-end and live** behind the
safety stack and the non-bypassable Telegram tap, plus a round of
observability and reliability work surfaced by letting the daemon soak.
Phase 2 stays opt-in per wishlist entry (`phase2.enabled`); a Comprar
tap on a non-opted-in entry is impossible because the button is never
rendered.

**Phase 2 — autonomous purchase wired end-to-end**

- `BuyOrchestrator` is now composed at startup with all nine of its
  collaborators (preflight, reconciler, browser, circuit breaker, audit
  writer, Telegram surface, store, reporter, wishlist loader); a Comprar
  tap drives a real, operator-confirmed buy instead of a logged no-op.
- A marketplace-dispatching browser/page-fetcher routes each listing to
  the right checkout flow by **parsed hostname** (Wallapop Pay vs eBay
  checkout), not a URL substring.
- The reconciler's eBay re-fetch client is now shared and closed on
  shutdown, and `ComposedDaemon.aclose` isolates each closer so one
  failing close can't leak the rest (#19).

**Telegram callback loop + alerts**

- The daemon now runs the Telegram callback listener concurrently with
  the poll loop, so view / skip / snooze / buy taps are handled live
  (#8), and logs `callback_handled` on the happy path (#10).
- Buyable Phase 1 / Phase 2 listing alerts now carry an in-cycle
  reserved-comp summary line after the Confidence row —
  `💬 Comps (N reservados): <min> – <max> € · mediana <med> €` — built
  from the reserved listings observed for the same entry that cycle, so
  the operator can judge the asking price in-context. Closes Layer 2 of
  the reserved-listing work; cross-cycle comp persistence stays a later
  item (#22).

**Observability & operability**

- Opt-in pretty (human-readable, coloured) log format via
  `logging.format: pretty`; JSON stays the default (NFR-O1). The OpenSpec
  spec-driven workflow was bootstrapped in the repo (#15).
- The listener task is supervised and shutdown drains in-flight tasks
  before `aclose`, so a listener crash surfaces loudly instead of
  wedging shutdown (#9).

**Matching & reliability**

- Each wishlist entry's keyword list now fans out to N marketplace
  searches per cycle, unioned and de-duped by `listing_id`, widening
  coverage without duplicate alerts (#11).
- The LLM cache serialises concurrent `get()` against its shared SQLite
  connection, fixing an `InterfaceError` race observed under the
  poll loop's evaluation fan-out (#18).

**Wishlist**

- The HC530 SAS variant is documented as a wishlist example so the
  SAS-vs-SATA distinction is captured alongside the SATA entry (#17).

**Internal / CI**

- GitHub Actions pinned to full commit SHA + Dependabot configured
  (#12), Actions group bumped (#13), Sonar hygiene (`logger.exception`
  in except blocks, extracted duplicated prose constants, guarded test
  list accesses, `tmp_path` over `/tmp`) (#14, #16), and the
  `annotate-alerts-with-comps` OpenSpec change archived with its spec
  promoted to `openspec/specs/listing-alert-comps/` (#23).

---

## [0.2.3] — 2026-05-20

Wallapop adapter stabilisation. The v0.2.0–0.2.2 line shipped before
the operator had run a live `salvager test-search` against
Wallapop's current production traffic; three failure modes that
release-gate testing couldn't reach surfaced on the first end-to-end
run and are fixed here.

**Wallapop unofficial API — v3 endpoint migration**

- The legacy `/api/v3/general/search` endpoint was deprecated by
  Wallapop before 2026-05-18; it returns HTTP 403 (CDN-level) to
  every client. The adapter now targets `/api/v3/search/section`
  with `section_type=organic_search_results` and the required
  `latitude` / `longitude` query params (browser geolocation).
- Wallapop's CloudFront WAF rejects clients whose TLS handshake
  doesn't match a real browser (JA3/JA4 fingerprinting); `httpx`
  is one of those. The adapter now uses `curl_cffi.requests.AsyncSession`
  with `impersonate='chrome131'` so the ClientHello replays Chrome's
  bit-for-bit (NFR-M1 whitelist updated; `curl_cffi` is allowed only
  inside `adapters/wallapop_api/`).
- The SPA injects eight application-level headers the WAF cross-checks
  against cookies: `Authorization: Bearer <accessToken>`, `mpid`,
  `trackinguserid`, `x-deviceid`, `x-appversion`, `deviceos`,
  `x-deviceos`, and a custom `Accept: application/json; sequence=v2`.
  All eight are derived per-request from the cookie jar.
- Keycloak access tokens live ~5 minutes. The adapter now does a
  transparent refresh dance on 401: hit
  `/api/auth/federated-session` with the current cookies, lift the
  rotated `accessToken` + `__Secure-next-auth.session-token` from
  the `Set-Cookie` response headers, persist atomically back to
  `cookies.txt` (mode 0600), and retry the original request once.
  The refresh path is serialised behind an `asyncio.Lock` so
  concurrent search/fetch callers don't double-refresh and clobber
  each other's rotated tokens. The in-memory cookie jar also
  re-reads from disk when `cookies.txt` mtime advances, so an
  operator re-running `salvager login wallapop` after both tokens
  expire is picked up by the next poll cycle without a daemon
  restart (#5).
- New `wallapop.latitude` / `wallapop.longitude` config (defaults
  Madrid centre, 40.4168 / -3.7038). The endpoint requires them and
  enforces a sanity range on the values; operators in other regions
  override in `config.yaml`.

**Wallapop login — auto-accept the cookie banner**

- The Playwright login driver now clicks Wallapop's
  ConsentManager CMP "Aceptar todo" button itself right after
  `page.goto`, sparing the operator one step. The banner element is
  an `<a class="cmpboxbtnyes" role="button">` (not a `<button>`)
  wrapped in `<span id="cmpwelcomebtnyes">`. We target the
  ConsentManager-product class, which is stable across the CMP's
  customers because the markup belongs to the third-party widget,
  not Wallapop's own frontend. If the banner isn't there or the
  markup shifts, the click silently times out and the operator
  falls back to clicking it manually — no regression (#6).

**Reserved-listing handling**

- `Listing` gains `is_reserved: bool = False`. Wallapop sellers
  flag listings reserved when the inventory is gone but the post
  is still up; before this release the adapter parsed them
  indistinguishable from buyable ones and the daemon could fire
  Telegram buy alerts on dead inventory.
- The Phase 2 pre-flight gate adds a new `listing_reserved` reason
  that fires before the DB read, so reserved listings downgrade
  silently to Phase 1 alerts (operator still sees market signal,
  no Buy CTA they'd tap into a 404).
- The poll cycle now partitions candidates into `(buyable, reserved)`.
  Reserved listings never reach the LLM evaluator (no eval cost on
  dead inventory) and never trigger alerts, but they are recorded as
  seen so the next cycle doesn't reprocess them. Each reserved batch
  emits a structured `reserved_comps_observed` event with the comp
  prices for downstream uses.
- `PollCycleSummary` gains `reserved_count` and a matching field in
  the per-cycle log so operators can see the buyable/reserved split
  at a glance.
- `salvager test-search` adds a Reserved column to the table and a
  footer one-liner with min/median/max comp prices when any reserved
  listing showed up (#7).

**Pre-login cookie fix (already on `main` as 25d2191)**

- `_SESSION_COOKIE_NAMES` previously included `device_id`, which
  Wallapop sets on every anonymous visit to `/login` — before
  credentials are entered. The login poller was matching on that
  pre-login cookie and declaring success with a useless jar. Narrowed
  to the post-login session cookies (`accessToken` /
  `__Secure-next-auth.session-token`) verified empirically against a
  fresh Chromium context.

## [0.2.2] — 2026-05-17

Re-cut of v0.2.1 with `pyproject.toml` actually bumped to match the
tag. The v0.2.1 GHCR image (`ghcr.io/ifuensan/salvager:0.2.1`)
shipped correctly tagged at the registry level but its embedded
`importlib.metadata` version still reported `0.2.0`, so
`salvager version` lied about which release the operator was
running. v0.2.1 stays published (the binary works); v0.2.2 is the
self-consistent re-cut. Operators on v0.2.1 should upgrade to
v0.2.2; there is no functional difference.

**Fixed**

- `pyproject.toml` version now matches the git/GHCR tag, so
  `salvager version` reports the actual release number instead of
  `0.2.0`.

## [0.2.1] — 2026-05-16

Operational patch on top of v0.2.0. No functional changes to the
poll loop, evaluator, alert renderer, or Phase 2 buy path. **Known
issue**: the embedded version string still reads `0.2.0` because
`pyproject.toml` was not bumped before tagging — fixed in v0.2.2.

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

[Unreleased]: https://github.com/ifuensan/salvager/compare/v0.2.3...HEAD
[1.0.0]: https://github.com/ifuensan/salvager/releases/tag/v1.0.0
[0.2.3]: https://github.com/ifuensan/salvager/releases/tag/v0.2.3
[0.2.2]: https://github.com/ifuensan/salvager/releases/tag/v0.2.2
[0.2.1]: https://github.com/ifuensan/salvager/releases/tag/v0.2.1
[0.2.0]: https://github.com/ifuensan/salvager/releases/tag/v0.2.0
[0.1.0]: https://github.com/ifuensan/salvager/releases/tag/v0.1.0
