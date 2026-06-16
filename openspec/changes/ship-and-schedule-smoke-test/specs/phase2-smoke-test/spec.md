## ADDED Requirements

### Requirement: Smoke-Test Fixtures Ship With The Runtime Image

The price-parser smoke-test fixtures SHALL ship inside the runtime artifact (Docker image and any built wheel), so `salvager phase2 smoke-test` succeeds in the deployed container with no extra volume or flag. The fixtures SHALL live under the `salvager` package, and the default fixtures directory SHALL resolve relative to the installed package (not the current working directory), valid whether the code runs from source or an installed wheel. The `--fixtures-dir` override SHALL remain available.

#### Scenario: Smoke-test runs in the deployed container

- **WHEN** `salvager phase2 smoke-test` runs in the runtime image with no `--fixtures-dir`
- **THEN** the default fixtures directory resolves to the packaged location and the fixtures are discovered
- **AND** the command does not fail with "smoke-test fixtures dir not found"

#### Scenario: Packaged fixtures resolve from the installed package

- **WHEN** the default fixtures directory is computed
- **THEN** it points at a path under the `salvager` package and contains the parser fixture pairs

---

### Requirement: Daemon Runs The Smoke-Test On Startup And Daily

The daemon SHALL run the price-parser smoke-test once on startup and once per UTC day at the configured `smoke_test_hour_utc`, persisting the result onto `phase2_state` so `Phase2Preflight`'s freshness gate stays green for opted-in entries. The scheduled run SHALL honour the configured hour (the daily run fires when the current UTC hour equals `smoke_test_hour_utc` and no run has occurred yet that UTC day). The smoke job SHALL reuse the existing `run_smoke_test` orchestrator and its collaborators; it SHALL NOT change preflight, parser, or buy-path behaviour.

#### Scenario: Startup smoke refreshes the signal after deploy

- **WHEN** the daemon starts
- **THEN** it runs the smoke-test once and persists the result to `phase2_state`
- **AND** a passing result makes opted-in entries eligible without waiting for the configured hour

#### Scenario: Daily smoke fires at the configured hour, once per day

- **WHEN** the smoke job's coarse-cadence task ticks and the current UTC hour equals `smoke_test_hour_utc` and no smoke has run yet this UTC day
- **THEN** it runs the smoke-test and persists the result
- **AND** subsequent ticks in the same UTC day do not re-run it

#### Scenario: A failing scheduled smoke disables Phase 2 and alerts the operator

- **WHEN** a scheduled smoke-test fails (a parser drifts from its expected price)
- **THEN** the existing `run_smoke_test` behaviour persists the failure and globally disables Phase 2
- **AND** the operator receives the `smoke_test_failed` operational alert

#### Scenario: Freshness stays within the preflight window

- **WHEN** the daemon has been running for more than a day with the daily smoke firing
- **THEN** the most recent passing smoke is never older than `DEFAULT_SMOKE_FRESHNESS_HOURS`
- **AND** the preflight smoke-freshness check does not report `smoke_test_stale` for an otherwise-healthy daemon
