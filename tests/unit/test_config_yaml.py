"""Tests for the config.yaml schema + loader — Story 2.5 (FR49)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from salvager.config.config_yaml import (
    ConfigModel,
    ConfigParseError,
    ConfigValidationError,
    EbayConfig,
    LLMConfig,
    LoggingConfig,
    PathsConfig,
    Phase2Config,
    ScheduleConfig,
    TelegramConfig,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"


# ─────────────────────────────────────────────────────────────────────────
# Defaults — match config.example.yaml
# ─────────────────────────────────────────────────────────────────────────


def test_schedule_defaults_match_example() -> None:
    s = ScheduleConfig()
    assert s.wallapop_minutes == 15
    assert s.ebay_minutes == 30


def test_llm_defaults_match_example() -> None:
    llm = LLMConfig()
    assert llm.provider == "gemini-flash"
    assert llm.cache_ttl_hours == 24
    assert llm.cache_ttl_hours_low_confidence == 1


def test_phase2_defaults_match_example() -> None:
    p = Phase2Config()
    assert p.kill_switch_global is False
    assert p.reconciliation_tolerance_eur == Decimal("1.00")
    assert p.reconciliation_tolerance_pct == Decimal("5")
    assert p.circuit_breaker_threshold == 3
    assert p.smoke_test_hour_utc == 6


def test_telegram_defaults_match_example() -> None:
    t = TelegramConfig()
    assert t.retry_max_attempts == 3
    assert t.retry_backoff_seconds == 5.0
    assert t.locale == "es-ES"


def test_ebay_defaults_match_example() -> None:
    e = EbayConfig()
    assert e.daily_request_quota == 5000


def test_logging_defaults_match_example() -> None:
    log = LoggingConfig()
    assert log.level == "info"
    assert log.format == "json"


def test_logging_accepts_pretty_format() -> None:
    log = LoggingConfig(format="pretty")
    assert log.format == "pretty"


def test_logging_accepts_explicit_json_format() -> None:
    log = LoggingConfig(format="json")
    assert log.format == "json"


def test_paths_defaults_match_example() -> None:
    paths = PathsConfig()
    assert paths.data_dir == Path("/app/data")
    assert paths.config_dir == Path("/app/config")


def test_empty_config_model_has_all_sections() -> None:
    """All sections have defaults — an empty YAML is a valid config."""
    config = ConfigModel()
    assert isinstance(config.schedule, ScheduleConfig)
    assert isinstance(config.llm, LLMConfig)
    assert isinstance(config.phase2, Phase2Config)
    assert isinstance(config.telegram, TelegramConfig)
    assert isinstance(config.ebay, EbayConfig)
    assert isinstance(config.logging, LoggingConfig)
    assert isinstance(config.paths, PathsConfig)


# ─────────────────────────────────────────────────────────────────────────
# Range checks
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("invalid", [Decimal("-0.5"), Decimal("100.01")])
def test_reconciliation_tolerance_pct_rejects_out_of_range(invalid: Decimal) -> None:
    """0 ≤ pct ≤ 100 (Annotated[Decimal, Field(ge=0, le=100)])."""
    with pytest.raises(ValidationError):
        Phase2Config(reconciliation_tolerance_pct=invalid)


@pytest.mark.parametrize("invalid", [-1, 24, 100])
def test_smoke_test_hour_out_of_range_rejected(invalid: int) -> None:
    with pytest.raises(ValidationError):
        Phase2Config(smoke_test_hour_utc=invalid)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LLMConfig(provider="palm-2")  # type: ignore[arg-type]


def test_unknown_locale_is_rejected() -> None:
    """v1 is locale-locked to es-ES per UX-DR27 — adding en-US is OQ work."""
    with pytest.raises(ValidationError):
        TelegramConfig(locale="en-US")  # type: ignore[arg-type]


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LoggingConfig(level="critical")  # type: ignore[arg-type]


def test_unknown_log_format_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LoggingConfig(format="verbose")  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────
# extra="forbid" — typos surface immediately
# ─────────────────────────────────────────────────────────────────────────


def test_unknown_top_level_section_rejected() -> None:
    with pytest.raises(Exception, match="extra_forbidden"):
        ConfigModel(unknown_section={})  # type: ignore[call-arg]


def test_unknown_field_in_section_rejected() -> None:
    with pytest.raises(Exception, match="extra_forbidden"):
        ScheduleConfig(wallapop_minutes=15, bogus="x")  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────────
# load_config()
# ─────────────────────────────────────────────────────────────────────────


def test_load_config_example_succeeds() -> None:
    """The tracked example file must parse cleanly — it's a CI contract."""
    config = load_config(EXAMPLE_CONFIG)
    assert config.schedule.wallapop_minutes == 15
    assert config.llm.provider == "gemini-flash"
    assert config.phase2.smoke_test_hour_utc == 6
    assert config.paths.data_dir == Path("/app/data")


def test_load_config_empty_file_uses_all_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    config = load_config(path)
    assert config.schedule.wallapop_minutes == 15
    assert config.logging.level == "info"


def test_load_config_partial_yaml_keeps_other_defaults(tmp_path: Path) -> None:
    """A user who only sets one section keeps default values for the rest."""
    path = tmp_path / "config.yaml"
    path.write_text("logging:\n  level: debug\n", encoding="utf-8")
    config = load_config(path)
    assert config.logging.level == "debug"
    assert config.schedule.wallapop_minutes == 15  # default preserved


def test_load_config_invalid_field_raises_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "schedule:\n  wallapop_minutes: -5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    err = excinfo.value
    assert err.path == path
    assert err.errors[0]["section"] == "schedule"
    assert err.errors[0]["field"] == "wallapop_minutes"


def test_load_config_reconciliation_pct_out_of_range_raises(tmp_path: Path) -> None:
    """AC: 0 ≤ pct ≤ 100, range check via Annotated[Decimal, Field(ge=0, le=100)]."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "phase2:\n  reconciliation_tolerance_pct: 150\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    err = excinfo.value
    assert err.errors[0]["section"] == "phase2"
    assert err.errors[0]["field"] == "reconciliation_tolerance_pct"


def test_load_config_unknown_section_raises(tmp_path: Path) -> None:
    """A typo'd top-level section is caught via extra='forbid'."""
    path = tmp_path / "config.yaml"
    path.write_text("scheduel:\n  wallapop_minutes: 15\n", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(path)


def test_load_config_malformed_yaml_raises_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("schedule:\n  wallapop_minutes: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigParseError) as excinfo:
        load_config(path)
    err = excinfo.value
    assert err.path == path
    assert err.line > 0
