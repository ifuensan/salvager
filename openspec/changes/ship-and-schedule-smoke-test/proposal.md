## Why

Phase 2 is **un-armable in production today**. `Phase2Preflight` gates every buy on a *fresh passing* smoke-test result in `phase2_state`; with none, it returns `eligible=False` (`smoke_test_never_run` / `_failed` / `_stale`) and the alert silently downgrades to Phase 1 — no Comprar button, no buy. The smoke result comes from `salvager phase2 smoke-test`, whose default `--fixtures-dir` is `tests/fixtures/price_parsers/active`, but the Dockerfile only `COPY`s `src/`, so the deployed container fails with "smoke-test fixtures dir not found". And nothing schedules a smoke automatically — `smoke_test_hour_utc: 6` has no consumer. Net: even after `phase2 enable`, no real purchase can occur. Confirmed live on hermes001 (v0.3.1). This blocks ROADMAP promotion criterion 2 (a real Phase 2 purchase or verified abort).

## What Changes

- **Ship the smoke fixtures in the image.** Relocate the price-parser smoke fixtures from `tests/fixtures/price_parsers/` into package data under `src/salvager/smoke_fixtures/price_parsers/active/` so they travel with the existing `COPY src/` (no `tests/` dir in the runtime image). Repoint `_DEFAULT_FIXTURES_DIR` to a package-relative path that resolves whether run from `/app/src` or an installed wheel, and add pyproject package-data so the files survive a wheel build. Update the two unit tests that reference the old path.
- **Wire a daemon-scheduled smoke-test.** Add a smoke job the daemon registers alongside the wallapop/ebay poll jobs. Because the `Scheduler` port is interval-only (no cron/at-hour), the job honours `smoke_test_hour_utc` via an hour-gated coarse-cadence task. The daemon also runs **one smoke on startup** so the freshness signal is green immediately after a (re)deploy instead of waiting for the configured hour.
- **No behaviour change** to the buy path, preflight logic, parsers, alert renderers, LLM prompt, or DB schema. The smoke job reuses the existing `run_smoke_test` orchestrator and persists the same `SmokeTestRecord` onto `phase2_state`.

## Capabilities

### New Capabilities
- `phase2-smoke-test`: the price-parser smoke-test ships with the runtime image and runs automatically (on startup + daily at the configured UTC hour), keeping the Phase 2 preflight's freshness signal green so opted-in entries stay armable.

### Modified Capabilities
<!-- None promoted. phase2-purchase-flow was specified in PRD stories / an unarchived change, not in openspec/specs/; this adds a new capability rather than editing a promoted spec. -->

## Impact

- **Code:** `src/salvager/smoke_fixtures/price_parsers/active/` (relocated 8 files); `cli/app.py` (`_DEFAULT_FIXTURES_DIR` → package-relative); `pyproject.toml` (package-data); `orchestration/composer.py` (build + register the smoke job with its collaborators: parser registry, fixtures dir, `Phase2AuditWriter`, `Phase2StateReader`, `TelegramSurface`); `orchestration/daemon.py` (register smoke job + run-once-on-startup); possibly a small smoke-job wrapper module.
- **Tests:** repoint `tests/unit/test_smoke_test.py` + `test_phase2_parsers.py` to the package fixtures; new tests for the hour-gate / run-once-per-day logic, the startup smoke, and composer wiring; a guard test that the shipped fixtures resolve from the package.
- **Ops:** ships as **v0.3.2**, redeployed to hermes001 (single release to minimise burn-in-clock resets). After deploy, `salvager phase2 smoke-test` works and the daemon keeps the signal fresh → Phase 2 can be armed.
- **No change** to: Dockerfile `COPY` set beyond what `src/` already carries, adapter-discipline / payment-rail surfaces, marketplace adapters.
