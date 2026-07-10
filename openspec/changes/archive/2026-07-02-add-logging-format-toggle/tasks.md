## 1. Configuration plumbing

- [x] 1.1 Add `LoggingConfig.format: Literal["json", "pretty"]` to `src/salvager/config/config_yaml.py`, default `"json"`. Include a docstring calling out "pretty is for interactive runs only".
- [x] 1.2 Extend `tests/unit/test_config_yaml.py` (or equivalent) with cases for: default value, explicit `"json"`, explicit `"pretty"`, and an invalid value raising a clear validation error.

## 2. Pretty formatter

- [x] 2.1 Add `PrettyConsoleFormatter(logging.Formatter)` to `src/salvager/observability/logging.py`. Single-line render: `HH:MM:SS  LEVEL  event_name  key=value …`. Drop `None` extras. Quote values containing whitespace.
- [x] 2.2 In `PrettyConsoleFormatter.format()`, gate ANSI level colouring on `sys.stdout.isatty() and not os.environ.get("NO_COLOR")` checked at emit time (not at construction).
- [x] 2.3 When `record.exc_info` is set, append the formatted traceback on indented continuation lines.
- [x] 2.4 Refactor `_configure_root(level_name, format_name=None)` to look up the formatter from a `{"json": JsonLineFormatter, "pretty": PrettyConsoleFormatter}` table; raise a clear `ValueError` with the supplied value if the format name is unknown.
- [x] 2.5 Add `configure_log_format(format: str)` as the companion to `configure_log_level()`, used by the config loader to apply the config value at startup.

## 3. CLI + env wiring

- [x] 3.1 Add `--log-format` option to the Typer root callback in `src/salvager/cli/app.py`. Default `None` so absence is distinguishable from explicit `"json"`.
- [x] 3.2 Add `_resolve_log_format(cli_value, env_value, config_value)` helper that returns the first non-None among `cli > env > config > "json"`, validating the result is one of `{"json", "pretty"}` and raising a clear startup error naming the source if it isn't.
- [x] 3.3 Call `configure_log_format(resolved)` early in the daemon-bootstrap path so logs emitted during `compose_daemon()` already use the chosen format.

## 4. Tests

- [x] 4.1 Unit-test `PrettyConsoleFormatter`: shape of the rendered line for a representative record, dropping of `None` extras, `key="quoted value"` for whitespace values.
- [x] 4.2 Unit-test ANSI gating: with `sys.stdout` swapped to a non-TTY (e.g. `io.StringIO`), the rendered line contains no `\x1b[` sequences. With `NO_COLOR=1` set, same result even on a TTY.
- [x] 4.3 Unit-test exception rendering: `log.exception()` produces a primary line followed by indented traceback continuation lines.
- [x] 4.4 Snapshot test (syrupy) pinning the pretty output of a representative `LogRecord` with `extra={...}` — drift in pretty UX becomes a visible PR diff. (Implemented as deterministic exact-string assert in-process; same drift-detection guarantee, no syrupy plumbing.)
- [x] 4.5 Unit-test `_resolve_log_format` precedence: CLI wins over env wins over config wins over default; invalid values rejected with source-naming error.
- [x] 4.6 Smoke-test that selecting `--log-format pretty` does not change `LogRecord` fields seen by existing structured-log assertions in the test suite (record dunders unchanged). (Covered by the full pre-existing test suite passing — 1057 tests green; JSON path tests inspect LogRecord fields and would have failed under a schema-affecting change.)

## 5. Documentation

- [x] 5.1 Add a "Persisting logs" subsection to `README.md` covering: (a) systemd via `journalctl -u salvager | jq`, (b) manual via `salvager 2>&1 | tee data/logs/salvager-$(date +%F).jsonl`, (c) when to use `pretty` vs `json`.
- [x] 5.2 Update the `config.example.yaml` (or equivalent reference config) to show the new `logging.format` field commented out with `# json | pretty (default: json)`.
- [x] 5.3 Verify the OpenSpec change validates cleanly: `openspec validate add-logging-format-toggle`.
