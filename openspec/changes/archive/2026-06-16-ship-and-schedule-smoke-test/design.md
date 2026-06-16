## Context

`Phase2Preflight` requires `phase2_state.last_smoke_result == "pass"` within `DEFAULT_SMOKE_FRESHNESS_HOURS = 24`, else it downgrades the alert to Phase 1 (no Comprar). The smoke is produced by `run_smoke_test(*, fixtures, parsers, audit_writer, state_reader, reporter, tolerance_eur, tolerance_pct, clock)` (orchestration/smoke_test.py), which persists a `SmokeTestRecord` onto `phase2_state` and emits `smoke_test_failed` / `smoke_test_recovered` operational alerts via the `Reporter`. Today nothing runs it in production: the CLI default `_DEFAULT_FIXTURES_DIR = Path("tests/fixtures/price_parsers/active")` isn't in the image (Dockerfile only `COPY`s `src/`), and the `Scheduler` port is interval-only (`register(job_name, cadence_minutes, task)`) with no consumer of `smoke_test_hour_utc`. The composer already builds the Phase 2 collaborators (parser registry, `Phase2AuditWriter`, `Phase2StateReader`, `Reporter`, `TelegramSurface`) for the buy path.

## Goals / Non-Goals

**Goals:**
- `salvager phase2 smoke-test` works in the deployed image (fixtures ship).
- The daemon keeps the freshness signal green automatically (startup + daily), so opted-in entries stay armable without manual smoke runs.
- Reuse the existing `run_smoke_test` orchestrator unchanged.

**Non-Goals:**
- No change to preflight logic, the buy path, parsers, alert renderers, DB schema, or LLM prompt.
- No new cron/at-hour primitive on the `Scheduler` port (work within interval-only).
- Not removing the `--fixtures-dir` CLI override (only its default moves).

## Decisions

**1. Relocate fixtures into the package, resolve via `Path(__file__)`.**
Move `tests/fixtures/price_parsers/active/*` → `src/salvager/smoke_fixtures/price_parsers/active/`. They then ship via the existing `COPY src/ ./src/` — no `tests/` in the runtime image (which a reviewer would rightly flag). `_DEFAULT_FIXTURES_DIR` becomes `Path(__file__).resolve().parent.parent / "smoke_fixtures/price_parsers/active"` (from `cli/app.py` → `src/salvager/`), valid at `/app/src/salvager/...`. Add `[tool.*] package-data`/`force-include` so a wheel build keeps them. The two unit tests that read the old path repoint to the new location via a shared constant. _Alternative considered:_ `COPY tests/fixtures` into the image — rejected (ships a `tests/` dir in runtime; smell). _Alternative:_ `importlib.resources` — cleaner in theory but the orchestrator wants a `Path` (`discover_fixtures(Path)`); a filesystem path under the package is simpler and works for both src-run and wheel.

**2. Hour-gated daily smoke on the interval scheduler + run-once-on-startup.**
The `Scheduler` only does fixed intervals, so register a `smoke_test` job at a coarse cadence (e.g. 30 min) whose task runs the actual smoke only when `clock().hour == smoke_test_hour_utc` AND it hasn't already run this UTC day (decided from `phase2_state.last_smoke_at`). This honours the configured hour without a cron primitive and self-dedupes across the coarse ticks. Additionally the daemon runs **one smoke on startup** (unconditional) so the signal is fresh right after a (re)deploy — otherwise Phase 2 stays blocked until the next configured hour. With a once-per-day run the signal age stays strictly < 24h (just under, right before the next day's run), inside the freshness window. _Alternative considered:_ daily-cadence (1440 min) interval job — rejected, it fires "24h after daemon start" (drifts off the configured hour, and a restart resets the clock). _Alternative:_ extend the Scheduler port with a cron primitive — rejected as out-of-proportion for one daily job.

**3. Compose the smoke task from existing collaborators.**
The composer builds a `smoke_task` closure over the parser registry (`phase2_parsers`), the package fixtures dir, the existing `Phase2AuditWriter`, `Phase2StateReader`, `Reporter`, and the config tolerances (`phase2.reconciliation_tolerance_eur/pct`), then hands it to the `Daemon` to register + run-on-startup. No new buy-path code; `run_smoke_test` is reused verbatim.

## Risks / Trade-offs

- **24h freshness boundary** → a strictly-once-daily run leaves the signal at ~24h just before the next run. Mitigation: the run-once-on-startup plus the daily run keep age < 24h in practice; if it ever proves tight, lower the gate to "hour match OR age > 20h". Documented so it's a known tuning knob.
- **An automated failing smoke auto-disables Phase 2 globally** (existing `run_smoke_test` behaviour). That is the intended safety response (a drifted parser must not buy), but it now fires unattended — the operator is told via the `smoke_test_failed` alert and re-arms with `phase2 enable`. Called out, not changed.
- **Wheel packaging miss** → if package-data isn't configured, the fixtures vanish from a built wheel even though `COPY src/` carries them in *this* image. Mitigation: add a guard test that resolves the shipped fixtures from the installed package, so CI fails if they stop shipping.
- **Startup smoke adds ~1s to boot** and emits an operational alert on first deploy — acceptable; it's the signal that Phase 2 is ready.
