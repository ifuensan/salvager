## 1. Ship fixtures in the package

- [x] 1.1 Move `tests/fixtures/price_parsers/active/*` (8 files) → `src/salvager/smoke_fixtures/price_parsers/active/`; move the `tests/fixtures/price_parsers/README.md` note too (or add one under the new dir).
- [x] 1.2 Repoint `_DEFAULT_FIXTURES_DIR` in `cli/app.py` to a package-relative path (`Path(__file__).resolve().parent.parent / "smoke_fixtures/price_parsers/active"`); keep the `--fixtures-dir` override.
- [x] 1.3 Ensure the fixtures ship in a built wheel. VERIFIED `uv_build` includes all files under the package by default — `uv build --wheel` produces a wheel containing `salvager/smoke_fixtures/price_parsers/...` (all 8 + README), so NO explicit pyproject package-data is needed. The guard test (1.5) fails CI if they ever stop shipping.
- [x] 1.4 Repoint `tests/unit/test_smoke_test.py` and `tests/unit/test_phase2_parsers.py` to the new location (shared constant) and confirm they pass.
- [x] 1.5 Add a guard test that the default fixtures dir resolves and is non-empty from the installed package (fails CI if fixtures stop shipping).

## 2. Wire the scheduled smoke job

- [x] 2.1 Add a smoke-job builder (composer): a closure over the parser registry, packaged fixtures dir, `Phase2AuditWriter`, `Phase2StateReader`, `Reporter`, and config tolerances, calling `run_smoke_test(...)`.
- [x] 2.2 Wrap it with the hour-gate: run only when `clock().hour == smoke_test_hour_utc` and no smoke has run yet this UTC day (decided from `phase2_state.last_smoke_at`); register it on the scheduler at a coarse cadence (e.g. 30 min).
- [x] 2.3 In `Daemon` (or composer), run **one** smoke unconditionally on startup so the signal is fresh post-deploy; register the daily job; include it in the `daemon_started` job list.
- [x] 2.4 Thread it through `compose_daemon` only when Phase 2 collaborators exist (same guard as the buy wiring); skip cleanly when Phase 2 isn't composed.

## 3. Tests

- [x] 3.1 Unit test the hour-gate / once-per-UTC-day logic (runs at the hour, skips off-hour, skips a second time same day, runs next day).
- [x] 3.2 Test the startup smoke runs once and persists a result via a fake audit writer/state reader.
- [x] 3.3 Composer test: the smoke job is registered alongside the poll jobs and is absent when Phase 2 isn't wired.

## 4. Verification

- [x] 4.1 `ruff check` + `ruff format --check`, adapter-discipline (NFR-M1), payment-rail (FR25/NFR-S5) clean.
- [x] 4.2 `mypy src tests` clean.
- [x] 4.3 Full pytest green (ignoring the 2 known sandbox `/app` failures).
- [x] 4.4 `openspec validate ship-and-schedule-smoke-test --strict` passes.
- [x] 4.5 Sanity: build/run check that `salvager phase2 smoke-test` resolves fixtures from the package (no `tests/`), e.g. run it from a clean checkout without the old path.
