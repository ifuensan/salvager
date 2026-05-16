# hardware-hunter

[![CI](https://github.com/ifuensan/hardware-hunter/actions/workflows/ci.yml/badge.svg)](https://github.com/ifuensan/hardware-hunter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Container](https://img.shields.io/badge/ghcr.io-hardware--hunter-blue)](https://github.com/ifuensan/hardware-hunter/pkgs/container/hardware-hunter)

A self-hosted personal agent for monitoring second-hand homelab parts. Watches your configured marketplaces against a YAML wishlist and sends Telegram alerts when matches appear — with optional autonomous purchase via a non-bypassable Telegram tap.

**Marketplaces (v1):** Wallapop and eBay.es.
**Wishlist focus:** HDDs (NAS-grade and enterprise) and DDR4 RAM. Extensible to other part types.
**Distribution:** single Docker image, single docker-compose service.

> **Status (May 2026): `v0.2.0` — Phase 1 + Phase 2 feature-complete preview.** All Epic 2–5 code has shipped + been audited for rendering invariants ([release-audit summary](docs/release-audits/v1.0/SUMMARY.md)). The daemon polls Wallapop + eBay.es, evaluates listings against the wishlist via Gemini Flash, dispatches Telegram alerts, and the Phase 2 autonomous-purchase loop is wired end-to-end behind the safety stack and the non-bypassable Telegram tap. **Not yet validated in production**: the operator's burn-in window is in progress; v1.0 promotion is gated on at least 2 weeks of continuous live-traffic operation + one completed Phase 2 purchase. Recommended pinned tag: `ghcr.io/ifuensan/hardware-hunter:0.2.0`. See [CHANGELOG.md](CHANGELOG.md) for v0.2.0 release notes and [ROADMAP.md](ROADMAP.md) for the v1.0 path.

---

## What it is and isn't

**It is:** a personal tool that one operator runs in their own homelab. It watches what you tell it to watch and tells you about matches. You decide whether to act.

**It isn't:** an arbitrage tool, a price-tracking-as-a-service, a marketplace scraper for resale, or a bot for anyone but the operator running it. The wishlist is the user's intent, period — no off-wishlist surfacing, no margin estimation, no resale-value scoring. The forbidden-field guard in `validate-wishlist` (FR3) and the LLM prompt template (FR17) enforce this structurally. See [CONTRIBUTING.md](CONTRIBUTING.md) for the "no arbitrage PRs" rule.

---

## Quick start

Prerequisites: Docker + docker-compose, a Telegram bot, a Google Gemini API key, an eBay developer account, a TinyFish API key (for the Phase 1 Wallapop fallback path and the Phase 2 buy flows), and a Wallapop / eBay.es account dedicated to the agent (see Legal disclaimer below).

The recommended image tag for new deployments is `ghcr.io/ifuensan/hardware-hunter:0.2.0` (pinned). `:latest` follows the newest release; pin to `:0.2.0` for reproducible deploys during the v1.0 burn-in window.

```bash
git clone https://github.com/ifuensan/hardware-hunter
cd hardware-hunter

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
# docker-compose run --rm hardware-hunter validate-wishlist

# Start the daemon (Phase 1 polling lands across Epics 2-4).
docker-compose up -d

# First-time Wallapop login — interactive browser cookie capture (Story 2.9).
# docker-compose exec hardware-hunter hardware-hunter login wallapop
```

The commented-out commands above land in subsequent stories. See [ROADMAP.md](ROADMAP.md) for what's implemented today versus planned.

---

## Legal disclaimer

**Spanish ToS posture.** hardware-hunter operates within the operator's own authenticated marketplace session. The tool does not bypass authentication, scrape behind login walls without consent, or impersonate the marketplace. The terms of service for each marketplace (Wallapop, eBay.es) are click-wrap agreements between the operator and the platform; you are the operator of this tool and the party bound by those terms.

**Secondary-account recommendation.** Use a separate marketplace account dedicated to hardware-hunter rather than your primary personal account. If an unforeseen pattern triggers anti-bot or rate-limit measures, you do not lose access to your day-to-day account. The tool's poll cadences (default: every 15 min for Wallapop, every 30 min for eBay.es) are well within human-volume rates, but a dedicated account is sound precaution.

**Anti-bot honesty.** Wallapop session re-authentication is intentionally manual (`hardware-hunter login wallapop`); there is no codepath that attempts silent re-login. The Phase 2 buy flow drives a real browser session via TinyFish; the agent uses the operator's session, not API token forgery (FR30).

The tool is provided AS IS under the MIT license (see [LICENSE](LICENSE)). The operator is solely responsible for compliance with applicable law in their jurisdiction.

---

## Architecture

Hexagonal / ports-and-adapters:

```
src/hardware_hunter/
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
