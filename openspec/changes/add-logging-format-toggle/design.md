## Context

`src/salvager/observability/logging.py` configures a single root logger under the `salvager` namespace with one handler (`_DynamicStdoutHandler`) and one formatter (`JsonLineFormatter`). The configuration is idempotent — `_configure_root()` short-circuits on the second call — and the level is the only knob exposed (config file, `SALVAGER_LOG_LEVEL` env var, or default `info`). NFR-O1 mandates JSON Lines on stdout because every downstream consumer (jq, future Loki/Promtail ingest, CI log capture) parses NDJSON.

The current handler is `_DynamicStdoutHandler`, which re-resolves `sys.stdout` on every emit so pytest's `capsys`/`capfd` fixtures work — that constraint stays. Tests assert on `LogRecord` fields, not on the formatted string, so the formatter swap is invisible to them.

`compose_daemon()` reads `config.yaml` and calls `configure_log_level(config.logging.level)` at startup. That same hook is the natural place to also push the format selection through, but the CLI also needs to be able to override before `compose_daemon` runs (so `--log-format pretty` works even when `config.yaml` is wrong or missing).

## Goals / Non-Goals

**Goals:**

- Operators can pick `pretty` for interactive runs and `json` for production — default stays `json` so existing pipelines and NFR-O1 are untouched.
- Format selection follows the same precedence ladder as `level` (CLI > env > config > default) so there is one mental model.
- Pretty output is genuinely readable: short timestamp, coloured level (when stdout is a TTY), event name, key=value extras, indented stack traces. No external dependencies.
- Persistence is documented, not implemented in-app — the README earns its keep here.

**Non-Goals:**

- No `FileHandler` inside the daemon. Rotation, retention, path conventions, and disk-fill safety all become the operator's problem (systemd or shell), which keeps the app 12-factor and Docker/K8s-friendly.
- No changes to the JSON record schema or keys. `event`, `ts`, `level`, and all caller-supplied `extra={...}` fields stay byte-identical.
- No third-party formatter library (`rich`, `structlog`, `colorlog`). The pretty formatter is ~60 LOC of stdlib; pulling in a dep for this is over-engineering and bumps NFR-M5's direct-dep budget.
- Pretty rendering is not exercised by unit tests — they read `LogRecord` fields, not the formatted line.

## Decisions

### Decision 1: Format selection lives on the existing `_configure_root()` seam

`_configure_root(level_name: str | None, format_name: str | None)` takes a new optional `format_name` parameter; the formatter is selected from a `{"json": JsonLineFormatter, "pretty": PrettyConsoleFormatter}` table at handler-attachment time. The existing idempotency guard preserves call-twice safety. A new `configure_log_format(format: str)` companion to `configure_log_level()` lets the config-yaml loader reconfigure after the CLI has run.

**Alternative considered**: a separate `LoggingConfigurator` class encapsulating both level and format. Rejected — adds a class for two functions' worth of state and breaks the existing module-level call sites with no concrete benefit.

### Decision 2: Precedence resolved in `cli/app.py`, not in `logging.py`

The CLI layer is the only point that sees all three sources (flag, env, config) before the daemon composes. Resolution lives in a small `_resolve_log_format(cli_flag, env, config)` helper there. `logging.py` stays a pure renderer.

**Alternative considered**: resolution inside `logging.py` reading `os.environ` directly. Rejected — couples the formatter module to environment state and makes test injection painful.

### Decision 3: TTY detection at format time, not at config time

`PrettyConsoleFormatter.format()` calls `sys.stdout.isatty()` per record and skips ANSI codes when the answer is False. This handles the `pretty | tee` case (operator wants pretty but pipes the output) correctly — colours would corrupt the saved file.

**Alternative considered**: detect TTY once at handler construction and bake the result in. Rejected — `_DynamicStdoutHandler` already re-resolves stdout per emit specifically because pytest swaps it; baking TTY state in would re-introduce the same kind of brittleness.

### Decision 4: Pretty extras render as `key=value`, omitting Nones

Extras render in caller-declared order (Python 3.7+ dict ordering on `record.__dict__`), space-separated, `None` values dropped. Multi-token values (anything containing whitespace) get `key="value"` quoting. This matches the de facto stdlib logging idiom and is readable without a parser.

**Alternative considered**: JSON-encoded extras at the end of the pretty line for round-trippability. Rejected — defeats the readability goal; if the operator wants JSON they pick `json`.

### Decision 5: Honour the [NO_COLOR](https://no-color.org/) convention

If `NO_COLOR` is set in the environment, the pretty formatter skips ANSI codes regardless of TTY status — established cross-tool convention. Implementation is one extra clause inside the existing `use_color` check (Decision 3), so this lands in this change rather than being deferred. A regression test (`test_pretty_no_color_env_var_suppresses_ansi`) covers it.

## Risks / Trade-offs

- **[Risk] Operator sets `format: pretty` in production**, then a downstream `jq` consumer breaks. → Mitigation: default stays `json`; README + config-schema docstring both call out "pretty is for interactive runs only".
- **[Risk] Pretty output drifts in shape over time** (a new caller adds a different extras style). → Mitigation: one syrupy snapshot test pins the pretty render of a representative record. Drift becomes a visible diff in PRs.
- **[Risk] ANSI codes leak when stdout is a pipe**, polluting `tee`'d logs. → Mitigation: `sys.stdout.isatty()` check per emit (Decision 3) covers this. `NO_COLOR` env var (Decision 5) covers users with terminals that misreport.
- **[Risk] Operators lose audit trail** because nobody reads the README about `tee` / journalctl. → Mitigation: the JSON default + stdout default means every existing deployment continues however it was already capturing stdout. The README addition is for the **new** workflow (running by hand), not a regression.
- **[Trade-off] One extra config field to keep aligned across `config.yaml`, env var, and CLI**. Accepted — exactly mirrors the existing `level` plumbing; cost is bounded.

## Migration Plan

Single deploy, no migration:

1. Merge the change; `config.yaml` schema accepts an optional `format` field (defaults to `"json"` when absent).
2. Existing `config.host.yaml` files without the field keep working — they get JSON, same as today.
3. Operators who want pretty add `logging.format: pretty` to their host config OR pass `--log-format pretty` for one-off interactive runs.
4. Rollback is a flag/config flip — no data migration, no schema rev.

## Open Questions

- None — the decisions above cover the cases that came up during proposal review.
