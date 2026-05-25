## Why

The daemon's log handler is hardcoded to a single JSON Lines formatter writing to stdout. That shape is correct for machine consumers (jq, Loki, Promtail) — and NFR-O1 mandates it — but it has two operational gaps:

1. **Interactive sessions are unreadable.** When running `uv run salvager` to debug locally, every line is a 200-character JSON blob. The operator has to pipe through `jq` just to see what the daemon is doing in real time.
2. **No persistence path is documented.** The current setup writes only to stdout; close the terminal and the audit trail vanishes. There is no on-ramp for an operator who wants to keep a record without rolling their own.

This change closes both gaps without compromising the JSON contract.

## What Changes

- Add an opt-in `pretty` log format alongside the existing JSON format. Same record content, human-readable rendering: short local timestamp, ANSI-coloured level, event name, key=value extras, stack traces on following lines when `exc_info` is present.
- Default stays `json` so every existing pipeline (CI logs, future Loki ingest, ad-hoc `jq` consumers) keeps working unchanged.
- Three precedence-ordered ways to choose the format, all already used for `level`:
  1. CLI `--log-format {json|pretty}`
  2. `SALVAGER_LOG_FORMAT` environment variable
  3. `logging.format` field in `config.yaml`
- Document the two operator paths for persistence in the README: systemd (`journalctl -u salvager`) and manual (`tee data/logs/...jsonl`). No FileHandler is added to the application — log persistence is a deployment concern.

## Capabilities

### New Capabilities

- `observability`: Structured-logging contract for the daemon — record schema, level routing, format selection, and how operators persist the stream. This change introduces the capability; future observability work (metrics, tracing, log redaction) extends it.

### Modified Capabilities

<!-- None — observability is brand new. -->

## Impact

**Affected code**:
- `src/salvager/observability/logging.py` — new `PrettyConsoleFormatter`; `_configure_root()` learns to select the formatter by name.
- `src/salvager/config/config_yaml.py` — `LoggingConfig.format: Literal["json", "pretty"]` field with default `"json"`.
- `src/salvager/cli/app.py` — new `--log-format` Typer option; environment-variable override resolved here.

**Affected docs**:
- `README.md` — new "Persisting logs" subsection explaining the systemd and tee patterns.

**Backwards compatibility**: Default behaviour unchanged — operators that never set `format` keep emitting JSON Lines exactly as today. No JSON record key changes (NFR-O1 contract preserved). No tests that assert on structured records need updating.

**Out of scope**:
- Changing the JSON record schema (NFR-O1 contract).
- In-app FileHandler with rotation/retention — deliberately delegated to systemd/shell. Adding a FileHandler later is a separate change.
- Pretty rendering inside unit tests — tests inspect structured `LogRecord` fields, not formatted output.
