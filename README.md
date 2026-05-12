# hardware-hunter

[![CI](https://github.com/ifuensan/hardware-hunter/actions/workflows/ci.yml/badge.svg)](https://github.com/ifuensan/hardware-hunter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Container](https://img.shields.io/badge/ghcr.io-hardware--hunter-blue)](https://github.com/ifuensan/hardware-hunter/pkgs/container/hardware-hunter)

A self-hosted personal agent for monitoring second-hand homelab parts. Watches your configured marketplaces against a YAML wishlist and sends Telegram alerts when matches appear — with optional autonomous purchase via a non-bypassable Telegram tap.

**Marketplaces (v1):** Wallapop and eBay.es.
**Wishlist focus:** HDDs (NAS-grade and enterprise) and DDR4 RAM. Extensible to other part types.
**Distribution:** single Docker image, single docker-compose service.

> **Status (May 2026): foundation shipped, daemon not yet polling.** Epic 1 is complete; `v0.1.0` is on GHCR with the installable skeleton, structured logging, CLI surface, and Docker image. The poll loop, marketplace adapters, and Telegram alert flow are still to come (Epics 2–4). See [ROADMAP.md](ROADMAP.md) for the implementation timeline. Phase 1 (alerts only) ships first; Phase 2 (autonomous purchase) follows after a 4–8 week stabilization window.

---

## What it is and isn't

**It is:** a personal tool that one operator runs in their own homelab. It watches what you tell it to watch and tells you about matches. You decide whether to act.

**It isn't:** an arbitrage tool, a price-tracking-as-a-service, a marketplace scraper for resale, or a bot for anyone but the operator running it. The wishlist is the user's intent, period — no off-wishlist surfacing, no margin estimation, no resale-value scoring. The forbidden-field guard in `validate-wishlist` (FR3) and the LLM prompt template (FR17) enforce this structurally. See [CONTRIBUTING.md](CONTRIBUTING.md) for the "no arbitrage PRs" rule.

---

## Quick start

Prerequisites: Docker + docker-compose, a Telegram bot, a Google Gemini API key, an eBay developer account, and a running Hermes Agent service the daemon can reach (Hermes is operated separately; typical deployment is a Proxmox VM on the operator's host).

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

Hermes Agent runs as a remote service (typically on a Proxmox VM) providing the scheduler, memory, and MCP routing (including TinyFish). The daemon connects via HTTP/MCP at the `HERMES_URL` configured in `.env`.

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
