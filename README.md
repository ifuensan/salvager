# salvager

[![CI](https://github.com/ifuensan/salvager/actions/workflows/ci.yml/badge.svg)](https://github.com/ifuensan/salvager/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Container](https://img.shields.io/badge/ghcr.io-hardware--hunter-blue)](https://github.com/ifuensan/salvager/pkgs/container/salvager)

A self-hosted personal agent for monitoring second-hand homelab parts. Watches your configured marketplaces against a YAML wishlist and sends Telegram alerts when matches appear — with optional autonomous purchase via a non-bypassable Telegram tap.

**Marketplaces (v1):** Wallapop and eBay.es.
**Wishlist focus:** HDDs (NAS-grade and enterprise) and DDR4 RAM. Extensible to other part types.
**Distribution:** single Docker image, single docker-compose service.

> **Status (July 2026): `v0.4.3` — Phase 1 + Phase 2 feature-complete preview, Phase 2 wired live.** All Epic 2–5 code shipped in v0.2.0; v0.2.1–v0.2.2 patched the Docker image; v0.2.3 stabilised the Wallapop adapter against the SPA's current production traffic (v3 `/search/section` endpoint, browser-TLS impersonation via `curl_cffi`, transparent JWT refresh, reserved-listing routing); v0.3.0 wired the Phase 2 autonomous-purchase loop end-to-end (BuyOrchestrator composed with all collaborators, marketplace-dispatched checkout), the Telegram callback listener, an in-cycle reserved-comp line on alerts, and a pretty-log toggle; v0.3.1 adds the clickable deep link to the listing on every alert (FR18); v0.3.2 ships the price-parser smoke-test in the image and runs it (startup + daily) so Phase 2 is actually armable in production; v0.3.3 makes every price ceiling shipping-aware — alert and buy gates plus receipt reconciliation compare the delivered buyer total (item + shipping + Wallapop's Protección fee), and alerts show the breakdown; v0.3.4 extends that total with an estimated flat import-charges buffer for eBay listings located outside the EU (`pricing.assumed_import_charges_eur`, default 3,63 €); v0.3.5 moves LLM evaluation to `gemini-2.5-flash` (Google retired 2.0-flash) and clips over-long takes instead of discarding valid verdicts; v0.4.0 adds live-updating alerts — dispatched alerts are watched (`alerts.watch_days`) and the original Telegram message edits itself on reserved flips and price drops, with a ping for big drops. The daemon polls Wallapop + eBay.es, evaluates listings against the wishlist via Gemini Flash or Claude Haiku, dispatches Telegram alerts, and the Phase 2 autonomous-purchase loop runs behind the safety stack and the non-bypassable Telegram tap. **Not yet validated in production**: the operator's burn-in window is in progress; v1.0 promotion is gated on at least 2 weeks of continuous live-traffic operation + one completed Phase 2 purchase. Recommended pinned tag: `ghcr.io/ifuensan/salvager:0.4.3`. See [CHANGELOG.md](CHANGELOG.md) for release notes and [ROADMAP.md](ROADMAP.md) for the v1.0 path.

---

## What it is and isn't

**It is:** a personal tool that one operator runs in their own homelab. It watches what you tell it to watch and tells you about matches. You decide whether to act.

**It isn't:** an arbitrage tool, a price-tracking-as-a-service, a marketplace scraper for resale, or a bot for anyone but the operator running it. The wishlist is the user's intent, period — no off-wishlist surfacing, no margin estimation, no resale-value scoring. The forbidden-field guard in `validate-wishlist` (FR3) and the LLM prompt template (FR17) enforce this structurally. See [CONTRIBUTING.md](CONTRIBUTING.md) for the "no arbitrage PRs" rule.

---

## Quick start

Prerequisites: Docker + docker-compose, a Telegram bot, a Google Gemini API key, an eBay developer account, a TinyFish API key (for the Phase 1 Wallapop fallback path and the Phase 2 buy flows), and a Wallapop / eBay.es account dedicated to the agent (see Legal disclaimer below).

The recommended image tag for new deployments is `ghcr.io/ifuensan/salvager:0.4.3` (pinned). `:latest` follows the newest release; pin to `:0.4.3` for reproducible deploys during the v1.0 burn-in window.

```bash
git clone https://github.com/ifuensan/salvager
cd salvager

# Scaffold your config from the tracked examples.
mkdir -p config data
cp .env.example                config/.env
cp wishlist.example.yaml       config/wishlist.yaml
cp config.example.yaml         config/config.yaml

# Edit config/.env with your credentials (see comments inline).
$EDITOR config/.env

# Edit config/wishlist.yaml with the parts you actually want.
$EDITOR config/wishlist.yaml

# Validate before starting (lands in Epic 2).
# docker-compose run --rm salvager validate-wishlist

# Start the daemon (Phase 1 polling lands across Epics 2-4).
docker-compose up -d

# First-time Wallapop login — interactive browser cookie capture (Story 2.9).
# docker-compose exec salvager salvager login wallapop
```

The commented-out commands above land in subsequent stories. See [ROADMAP.md](ROADMAP.md) for what's implemented today versus planned.

**Matching the container UID to your host (optional).** As of `v0.2.1` the runtime stage drops to a non-root `salvager` user (UID 1000 by default), so files written to `./data` and `./config` are owned by UID 1000 on the host. If your host user is not UID 1000, rebuild the image with your own UID/GID so volume contents stay writable without `sudo`:

```bash
docker-compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
```

Operators upgrading from `v0.2.0` (which ran as root) need to reclaim ownership of their existing volume contents once:

```bash
sudo chown -R $(id -u):$(id -g) ./data ./config
```

---

## Legal disclaimer

**Spanish ToS posture.** salvager operates within the operator's own authenticated marketplace session. The tool does not bypass authentication, scrape behind login walls without consent, or impersonate the marketplace. The terms of service for each marketplace (Wallapop, eBay.es) are click-wrap agreements between the operator and the platform; you are the operator of this tool and the party bound by those terms.

**Secondary-account recommendation.** Use a separate marketplace account dedicated to salvager rather than your primary personal account. If an unforeseen pattern triggers anti-bot or rate-limit measures, you do not lose access to your day-to-day account. The tool's poll cadences (default: every 15 min for Wallapop, every 30 min for eBay.es) are well within human-volume rates, but a dedicated account is sound precaution.

**Anti-bot honesty.** Wallapop session re-authentication is intentionally manual (`salvager login wallapop`); there is no codepath that attempts silent re-login. The Phase 2 buy flow drives a real browser session via TinyFish; the agent uses the operator's session, not API token forgery (FR30).

The tool is provided AS IS under the MIT license (see [LICENSE](LICENSE)). The operator is solely responsible for compliance with applicable law in their jurisdiction.

---

## Architecture

Hexagonal / ports-and-adapters:

```
src/salvager/
├── domain/          ← pure pydantic models, no external SDK imports
├── interfaces/      ← ABCs (PageFetcher, ListingEvaluator, Store, …)
├── orchestration/   ← composes interfaces; the poll loop + buy flow
├── adapters/        ← the ONLY package allowed to import external SDKs
├── cli/             ← typer subcommand surface (FR39–FR48)
├── config/          ← pydantic-settings loaders
└── observability/   ← structured logging + CLI rendering helpers
```

The adapter discipline boundary (NFR-M1, launch-blocker) is enforced by a custom AST-based CI lint at [`scripts/adapter_discipline_lint.py`](scripts/adapter_discipline_lint.py).

Scheduler runs in-process (asyncio-based, `adapters/asyncio_scheduler/`). TinyFish is reached directly via the official SDK from `adapters/wallapop_tinyfish/` (Phase 1 fallback when the unofficial Wallapop API path fails) and `adapters/tinyfish_browser/` (Phase 2 buy flows for Wallapop Pay + eBay.es checkout). No remote agent-orchestration service is required.

---

## Logs

The daemon writes one structured record per line to stdout — JSON by default (NFR-O1), or a coloured human-readable rendering on opt-in. Persistence is delegated to the host (`tee`, systemd journal, Docker log driver) so the app stays 12-factor.

### Picking a format

| Setting | Default | Override |
|---|---|---|
| `logging.format` in `config.yaml` | `json` | `pretty` for interactive debugging |
| `SALVAGER_LOG_FORMAT` env var | unset | `json` or `pretty` (wins over config) |
| `--log-format` CLI flag | unset | `json` or `pretty` (wins over env) |

Pretty output omits ANSI colours automatically when stdout is piped or `NO_COLOR` is set, so `salvager --log-format pretty | tee log.txt` produces a clean file.

### Persisting the stream

**Manual run** — capture stdout to a daily file while still seeing it live:

```bash
uv run salvager -c config/config.host.yaml 2>&1 \
  | tee -a "data/logs/salvager-$(date +%F).jsonl"
```

**Under systemd** — let journald handle rotation and follow with jq:

```bash
journalctl -u salvager -f --output=cat | jq .
```

**Under Docker** — stdout is captured by the runtime; configure the daemon's logging driver (`json-file`, `journald`, etc.) at the container level. The app intentionally does not write log files itself.

---

## Phase 2: operator-confirmed buy

Phase 2 lets the daemon actually purchase a listing — via TinyFish-driven Wallapop Pay or eBay.es checkout — but **only when the operator taps the "✅ Comprar" button on a Telegram alert**. There is no auto-buy mode anywhere in the codebase. The daemon polls, evaluates, and alerts; the operator decides whether to pull the trigger.

### Enabling Phase 2 for a wishlist entry

By default every entry has `phase2.enabled: false` and only the Phase 1 buttons (View / Skip / Snooze) appear on its alerts. To enable Phase 2 for a specific entry:

```bash
uv run salvager phase2 enable <entry-ref>
```

Once enabled, alerts for that entry render with an extra "✅ Comprar" button. The per-entry `phase2.max_price_eur` ceiling is the operator's stated max — the orchestrator's preflight + reconciler refuse to buy above it even if the live price has drifted from the snapshot.

### What a Comprar tap actually does

When you tap "✅ Comprar" on an open alert, the daemon runs this pipeline in the background:

1. **Snapshot lookup** — locate the alert's frozen `(entry, listing, evaluation)` snapshot in the local store.
2. **Preflight gate** — confirm the entry is still enabled, the circuit breaker is closed, and Phase 2 is not globally killed (`config.phase2.kill_switch_global`).
3. **Reconciliation** — re-fetch the listing on the marketplace and verify the displayed price still matches the snapshot within tolerance (`config.phase2.reconciliation_tolerance_eur` / `_pct`).
4. **Checkout** — drive the per-marketplace browser flow via TinyFish (Wallapop Pay or eBay.es checkout); abort if the on-page price exceeds the entry's `max_price_eur`.
5. **Audit** — write an append-only row to the Phase 2 audit log capturing inputs, outcome, and timestamps.
6. **Report** — send a Telegram follow-up summarising the outcome (success with receipt id + price paid, failure with reason, or aborted with reason).

If `N` consecutive buys fail (default `N = config.phase2.circuit_breaker_threshold = 3`), the circuit breaker opens and further taps are rejected at preflight until the operator clears the failure state (`salvager phase2 reset` — see `phase2 --help`).

### Inspecting the audit log

```bash
uv run salvager audit show --type phase2
```

Every Comprar tap that reached the orchestrator appears here — including the aborted ones — with the reason, prices, and receipt id where applicable.

---

## Planning artifacts

The BMAD planning artifacts that drove the design and implementation plan live in [`_bmad-output/planning-artifacts/`](_bmad-output/planning-artifacts):

| Artifact | Path |
|---|---|
| Product Requirements Document (54 FRs, 37 NFRs) | [prd.md](_bmad-output/planning-artifacts/prd.md) ([PDF](_bmad-output/planning-artifacts/prd.pdf)) |
| Architecture Decision Document | [architecture.md](_bmad-output/planning-artifacts/architecture.md) |
| UX Design Specification | [ux-design-specification.md](_bmad-output/planning-artifacts/ux-design-specification.md) |
| Epic & Story Breakdown (5 epics, 61 stories) | [epics.md](_bmad-output/planning-artifacts/epics.md) |
| Implementation Readiness Report | [implementation-readiness-report-2026-05-11.md](_bmad-output/planning-artifacts/implementation-readiness-report-2026-05-11.md) |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev loop and the three accepted invitation categories. Note the explicit "no arbitrage PRs" rule — fork the future-research repo named in [ROADMAP.md](ROADMAP.md) for that work.

---

## License

MIT — see [LICENSE](LICENSE).
