# Contributing to hardware-hunter

Welcome. hardware-hunter is a personal monitoring tool, not a general marketplace bot. Contributions are gratefully received within a narrow scope.

---

## No arbitrage PRs

hardware-hunter is **not** an arbitrage tool. The wishlist is the user's intent; the agent verifies matches against entries you declare, full stop. There is no resale-value scoring, no margin estimation, no off-wishlist surfacing, no "good deals" pipeline.

This is enforced structurally (FR3, FR17):

- `hardware-hunter validate-wishlist` refuses any YAML containing the fields `expected_resale_value`, `min_margin_percent`, `current_market_price`, `target_resale_margin`, `arbitrage_score`, or `resale_target`. The error message points to this file and to [ROADMAP.md](ROADMAP.md).
- The wishlist-anchored LLM prompt in `src/hardware_hunter/domain/prompts.py` has no codepath that produces such outputs. The LLM is asked one question only: *"does this listing match this wishlist entry?"*
- The `Store` interface and the SQLite schema have no columns or methods for resale metadata.

PRs that introduce arbitrage features — direct or indirect (price prediction, margin scoring, recommendation surfacing, resale-flag annotations) — will be closed without review. If that's the project you want to build, see the future-research repo path in [ROADMAP.md](ROADMAP.md). The MIT license permits forking; please don't take the trademark or shared name when you do.

---

## Three categories of welcome contributions

### 1. Wishlist examples

PRs that add entries to `wishlist.example.yaml` (covering parts hardware-hunter users commonly hunt) are welcome. Include realistic keywords + container_keywords; do not include fictional pricing. Keep the (c3) scope rule (no arbitrage fields) intact.

### 2. Prompt improvements

PRs to `src/hardware_hunter/domain/prompts.py` (the wishlist-anchored LLM evaluation prompt) that improve match accuracy on existing fixtures or address a documented prompt failure mode are welcome. Open an issue first if the change is non-trivial; we'll discuss the failure mode and the fixture you'd be optimizing against.

### 3. Marketplace selector / parser patches

When a marketplace UI shifts and the unofficial-API or HTML adapter breaks (the regression-set test in `tests/fixtures/price_parsers/` catches this), PRs that patch the selector + add a fixture documenting the new shape are very welcome. Time-sensitive; open an issue if you've spotted a break before opening the PR so a coordination thread exists.

---

## Dev loop

```bash
# One-time: install uv.
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and sync.
git clone https://github.com/ifuensan/hardware-hunter
cd hardware-hunter
uv sync

# Run the verification gates locally before opening a PR.
uv run ruff check .
uv run ruff format --check .
uv run ty check src tests             # informational; mypy is the gate
uv run mypy src tests scripts
uv run pytest -q
uv run python scripts/adapter_discipline_lint.py
```

All of these run in CI on every PR. The `adapter_discipline_lint` is the NFR-M1 launch-blocker mechanism: business-logic packages (`domain/`, `interfaces/`, `orchestration/`, `cli/`, `config/`, `observability/`) cannot import marketplace SDKs / Hermes / TinyFish / LLM SDKs / `python-telegram-bot` / `httpx`. Only `adapters/` can.

For changes touching `src/hardware_hunter/adapters/tinyfish_browser/` (Phase 2 buy flow, lands in Epic 5), the payment-rail lint in `scripts/payment_rail_lint.py` will also run. Any reference to `bizum`, `transferencia`, `paypal`, etc. fails the build per NFR-S5. Phase 2 purchases go through Wallapop Pay or eBay.es checkout exclusively.

---

## Code style

- Python 3.12+
- ruff defaults (E/W/F/I/B/UP/C4/ARG/SIM/TID/PTH/RUF rule sets) + double-quote format
- mypy strict on `src/`, `tests/`, `scripts/`
- Type hints on every function (including tests)
- snake_case modules, PascalCase classes
- ABCs in `interfaces/` are named after the role, not the implementation (`PageFetcher`, not `MarketplaceFetcher`)
- Telegram-bound strings live in Spanish (`es-ES`); everything else (CLI output, log messages, README, this file) is English. See UX-DR27 in the UX Design Specification.

---

## Maintainer

This is a single-maintainer project (ifuensan reviews and merges). For non-trivial PRs, open an issue first so we can talk about scope.

The walk-away triggers documented in [ROADMAP.md](ROADMAP.md) are real. If the maintenance budget spikes the project will be wound down per the documented procedure, and the repo archived. This is not a venture-backed project; please calibrate expectations.

---

## Reporting bugs

Open a GitHub issue with:

- What you ran (full command + relevant config snippet, redacted of credentials).
- What you expected.
- What happened (paste relevant audit log entries from `hardware-hunter audit show --last 5` once Epic 4 lands; until then, paste structured-log lines from `docker-compose logs`).
- Whether you've reproduced it.

Security issues: please email ivanfs@b4os.dev rather than opening a public issue.
