## ADDED Requirements

### Requirement: Default Log Format Is Structured JSON Lines

The daemon SHALL emit one JSON object per log record on stdout when no format override is set, with `level`, `ts` (ISO 8601 UTC, millisecond precision, `Z` suffix), and `event` fields plus every caller-supplied `extra={...}` key as a top-level field. This preserves the NFR-O1 contract relied on by `jq`, Loki, Promtail, and CI log capture.

#### Scenario: Daemon started with no format override

- **WHEN** the operator runs `salvager` without `--log-format`, without `SALVAGER_LOG_FORMAT`, and without a `logging.format` entry in `config.yaml`
- **THEN** each log record on stdout is a single line of valid JSON containing at least `level`, `ts`, and `event` keys
- **AND** every caller-supplied `extra={...}` key appears as a top-level field in that JSON object

#### Scenario: Existing JSON consumers see no schema change

- **WHEN** a downstream consumer parses the daemon's stdout with `jq` or an NDJSON ingestor
- **THEN** record keys (`level`, `ts`, `event`, and previously emitted `extra` fields) are byte-identical to the pre-change output

---

### Requirement: Pretty Format Available on Opt-In

The daemon SHALL support a `pretty` log format that renders the same record content in a human-readable single line: short local-time `HH:MM:SS` timestamp, level label, event name, and `key=value` extras (with `None` values omitted). When `exc_info` is present on the record, the stack trace SHALL be appended on subsequent indented lines.

#### Scenario: Operator opts into pretty format

- **WHEN** the operator selects the `pretty` format through any supported override
- **THEN** each log record renders as a single line in the form `HH:MM:SS  LEVEL  event_name  key=value key=value`
- **AND** keys whose value is `None` are omitted from the rendered line
- **AND** the JSON record schema described in the default-format requirement is not emitted on that handler

#### Scenario: Exception is logged in pretty format

- **WHEN** the daemon calls `log.exception(...)` inside an `except` block while running with `pretty` format
- **THEN** the rendered line is followed by the formatted stack trace on indented continuation lines
- **AND** the exception's class name and message remain visible as `extra` fields on the primary line if the caller passed them

---

### Requirement: Format Selection Follows CLI > Env > Config > Default Precedence

The daemon SHALL resolve the active log format from the highest-priority source that supplies a value, in the order: `--log-format` CLI flag, `SALVAGER_LOG_FORMAT` environment variable, `logging.format` field in `config.yaml`, then the built-in default of `json`. An invalid format string from any source SHALL produce a startup error with a clear message naming the offending source.

#### Scenario: CLI flag overrides environment and config

- **WHEN** the operator runs `salvager --log-format pretty` with `SALVAGER_LOG_FORMAT=json` set and `logging.format: json` in `config.yaml`
- **THEN** the daemon emits pretty-formatted records

#### Scenario: Environment variable overrides config

- **WHEN** the operator runs `salvager` (no CLI flag) with `SALVAGER_LOG_FORMAT=pretty` and `logging.format: json` in `config.yaml`
- **THEN** the daemon emits pretty-formatted records

#### Scenario: Config value used when CLI and env are absent

- **WHEN** the operator runs `salvager` with no CLI flag, no `SALVAGER_LOG_FORMAT` set, and `logging.format: pretty` in `config.yaml`
- **THEN** the daemon emits pretty-formatted records

#### Scenario: Invalid format string is rejected at startup

- **WHEN** any source supplies a format string other than `json` or `pretty` (for example `SALVAGER_LOG_FORMAT=verbose`)
- **THEN** the daemon exits non-zero before opening external connections
- **AND** the error message identifies which source supplied the invalid value

---

### Requirement: Pretty Format Suppresses ANSI Codes When Stdout Is Not a Terminal

The pretty formatter SHALL emit ANSI colour escape sequences only when `sys.stdout.isatty()` is true at the moment of writing the record. When stdout is piped, redirected, or has been captured (for example by pytest's `capsys` fixture), the formatter SHALL emit the same line contents without any ANSI codes so that downstream file consumers see clean text.

#### Scenario: Operator pipes pretty output to a file

- **WHEN** the operator runs `salvager --log-format pretty 2>&1 | tee data/logs/salvager.log`
- **THEN** the file `data/logs/salvager.log` contains the pretty-formatted lines with zero ANSI escape sequences

#### Scenario: Operator runs pretty in an interactive terminal

- **WHEN** the operator runs `salvager --log-format pretty` directly in a terminal that reports as a TTY
- **THEN** each rendered line includes ANSI sequences colouring the level label
