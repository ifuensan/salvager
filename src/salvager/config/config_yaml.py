"""config.yaml schema + loader — FR49.

This is the single source of truth for operational tunables. Every section
listed in ``config.example.yaml`` has a typed model below; defaults match
the example file. Adding a tunable means: adding a field here, documenting
its default + range in the example, and (where range checks matter)
constraining it via :class:`Annotated[type, Field(ge=…, le=…)]`.

Why ``BaseModel`` and not ``BaseSettings``
------------------------------------------
``pydantic-settings`` ``BaseSettings`` is the right tool for env-var
hydration (``.env`` → ``EnvSettings`` lands in Story 2.6). For YAML
operational tunables, plain ``BaseModel`` with ``extra="forbid"`` is the
idiom — we already have a YAML loader pattern from
``config/wishlist_yaml.py``, and BaseSettings' env-priority logic would
just be noise here.
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# ─────────────────────────────────────────────────────────────────────────
# Locked enumerations
# ─────────────────────────────────────────────────────────────────────────

LLMProvider = Literal["gemini-flash", "gpt-4o-mini", "claude-haiku"]
LogLevel = Literal["debug", "info", "warn", "error"]
TelegramLocale = Literal["es-ES"]  # UX-DR27 bilingual asymmetry — v1 is Spanish only.


# ─────────────────────────────────────────────────────────────────────────
# Section models
# ─────────────────────────────────────────────────────────────────────────


class ScheduleConfig(BaseModel):
    """Per-marketplace poll cadences (FR8, NFR-P1)."""

    model_config = ConfigDict(extra="forbid")

    wallapop_minutes: Annotated[int, Field(ge=1, le=1440)] = 15
    ebay_minutes: Annotated[int, Field(ge=1, le=1440)] = 30


class LLMConfig(BaseModel):
    """Listing evaluation knobs (FR13-FR17, NFR-I3, NFR-C3)."""

    model_config = ConfigDict(extra="forbid")

    provider: LLMProvider = "gemini-flash"
    cache_ttl_hours: Annotated[int, Field(ge=0)] = 24
    cache_ttl_hours_low_confidence: Annotated[int, Field(ge=0)] = 1


class Phase2Config(BaseModel):
    """Autonomous-purchase safety-stack tunables (FR23-FR35)."""

    model_config = ConfigDict(extra="forbid")

    kill_switch_global: bool = False
    reconciliation_tolerance_eur: Annotated[Decimal, Field(ge=0)] = Decimal("1.00")
    reconciliation_tolerance_pct: Annotated[Decimal, Field(ge=0, le=100)] = Decimal("5")
    circuit_breaker_threshold: Annotated[int, Field(ge=1)] = 3
    smoke_test_hour_utc: Annotated[int, Field(ge=0, le=23)] = 6


class TelegramConfig(BaseModel):
    """Telegram delivery semantics (NFR-I6, UX-DR27)."""

    model_config = ConfigDict(extra="forbid")

    retry_max_attempts: Annotated[int, Field(ge=0)] = 3
    retry_backoff_seconds: Annotated[float, Field(ge=0)] = 5.0
    locale: TelegramLocale = "es-ES"


class EbayConfig(BaseModel):
    """eBay daily-request budget knobs (NFR-I5)."""

    model_config = ConfigDict(extra="forbid")

    daily_request_quota: Annotated[int, Field(ge=1)] = 5000


class WallapopConfig(BaseModel):
    """Wallapop-specific knobs. ``latitude`` / ``longitude`` are
    required by the v3 ``/api/v3/search/section`` endpoint — the
    SPA passes the operator's browser geolocation. We default to
    Madrid centre (40.4168, -3.7038); operators who care about
    proximity ranking should set their own."""

    model_config = ConfigDict(extra="forbid")

    latitude: Annotated[float, Field(ge=-90.0, le=90.0)] = 40.4168
    longitude: Annotated[float, Field(ge=-180.0, le=180.0)] = -3.7038


class LoggingConfig(BaseModel):
    """Structured-log threshold (NFR-O1, NFR-O4)."""

    model_config = ConfigDict(extra="forbid")

    level: LogLevel = "info"


class ObservabilityConfig(BaseModel):
    """Degradation-reporting knobs (NFR-R3, Story 4.2)."""

    model_config = ConfigDict(extra="forbid")

    #: Repeated ``(event, ctx)`` degradations inside this window emit only
    #: one Telegram alert — the log + health state still update for every
    #: occurrence. Prevents alert storms during cascading failures.
    degradation_dedup_window_seconds: Annotated[int, Field(ge=0)] = 300


class PathsConfig(BaseModel):
    """Operator-owned state locations (NFR-PR1, NFR-S2, AR17)."""

    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("/app/data")
    config_dir: Path = Path("/app/config")


# ─────────────────────────────────────────────────────────────────────────
# Top-level model
# ─────────────────────────────────────────────────────────────────────────


class ConfigModel(BaseModel):
    """Top-level config.yaml schema — all sections optional, all field
    defaults match ``config.example.yaml``. Unknown top-level keys raise
    ``ValidationError`` via ``extra="forbid"``."""

    model_config = ConfigDict(extra="forbid")

    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    phase2: Phase2Config = Field(default_factory=Phase2Config)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    ebay: EbayConfig = Field(default_factory=EbayConfig)
    wallapop: WallapopConfig = Field(default_factory=WallapopConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)


# ─────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """Base class for any config.yaml loader failure."""


class ConfigParseError(ConfigError):
    """The YAML parser rejected the file (malformed syntax)."""

    def __init__(self, path: Path, line: int, column: int, message: str) -> None:
        self.path = path
        self.line = line
        self.column = column
        super().__init__(f"{path}:{line}:{column}: {message}")


class ConfigValidationError(ConfigError):
    """pydantic rejected a field. ``errors`` carries the typed loc trail
    so the CLI's renderer (Story 2.7) can format section + field cleanly."""

    def __init__(
        self,
        path: Path,
        errors: list[dict[str, Any]],
        underlying: ValidationError,
    ) -> None:
        self.path = path
        self.errors = errors
        self.underlying = underlying
        first = errors[0]
        section = first.get("section") or "<root>"
        field = first.get("field") or first.get("loc_str")
        super().__init__(f"{path}: [{section}.{field}] {first['msg']}")


# ─────────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────────


def load_config(path: str | Path) -> ConfigModel:
    """Parse and validate ``config.yaml``. Returns a typed :class:`ConfigModel`.

    Validation order:
      1. ruamel.yaml parse → :class:`ConfigParseError`
      2. pydantic validate → :class:`ConfigValidationError`
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    try:
        raw = YAML(typ="safe").load(StringIO(text))
    except YAMLError as exc:
        line, column = _extract_parse_position(exc)
        raise ConfigParseError(path, line, column, str(exc)) from exc

    if raw is None:
        raw = {}

    try:
        return ConfigModel.model_validate(raw)
    except ValidationError as exc:
        errors = _enrich_validation_errors(exc)
        raise ConfigValidationError(path, errors, exc) from exc


# ─────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────


def _extract_parse_position(exc: YAMLError) -> tuple[int, int]:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is None:
        return (0, 0)
    return (int(mark.line) + 1, int(mark.column) + 1)


def _enrich_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Split each error's loc into (section, field) so the CLI can render
    a section-first message without re-parsing the dotted path."""
    enriched: list[dict[str, Any]] = []
    for raw in exc.errors():
        loc: tuple[Any, ...] = raw["loc"]
        section = str(loc[0]) if loc else None
        field = ".".join(str(p) for p in loc[1:]) if len(loc) > 1 else None
        enriched.append(
            {
                **raw,
                "section": section,
                "field": field,
                "loc_str": ".".join(str(p) for p in loc),
            }
        )
    return enriched
