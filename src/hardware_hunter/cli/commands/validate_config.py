"""``hardware-hunter validate-config`` — Story 2.7.

Composes Stories 2.5 and 2.6 into the operator pre-flight gate:

  1. ``config.yaml`` missing → exit 1 (run ``init`` to scaffold)
  2. ``config.yaml`` malformed / out-of-range → exit 3 with section.field
  3. ``.env`` missing a required credential → exit 4 (auth) with
     ``see .env.example``
  4. Otherwise → exit 0 with ``✓ config.yaml + .env are valid``

The exit-code map differs from ``validate-wishlist``: env-validation
failures are credential-shaped, so they route through ``4`` per
:data:`hardware_hunter.config.env.ENV_AUTH_EXIT_CODE`. Config-shape
failures are validation-shaped and stay on ``3``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from hardware_hunter.config.config_yaml import (
    ConfigParseError,
    ConfigValidationError,
    load_config,
)
from hardware_hunter.config.env import (
    ENV_AUTH_EXIT_CODE,
    get_env_settings,
    reset_env_cache,
)
from hardware_hunter.observability.styling import render_prose


def run(config_path: Path, env_path: Path, output_format: str) -> int:
    """Validate ``config.yaml`` + ``.env``. Returns the exit code."""
    if output_format not in {"human", "json"}:
        _emit_error(
            output_format,
            message=f"unknown --format value: {output_format!r}",
            hint="use --format human or --format json",
            exit_code=2,
        )
        return 2

    if not config_path.exists():
        _emit_error(
            output_format,
            message=f"config.yaml not found at {config_path}",
            hint="run hardware-hunter init to scaffold one",
            exit_code=1,
        )
        return 1

    try:
        load_config(config_path)
    except ConfigValidationError as exc:
        _emit_config_validation_error(output_format, exc)
        return 3
    except ConfigParseError as exc:
        _emit_config_parse_error(output_format, exc)
        return 3

    try:
        reset_env_cache()
        get_env_settings(env_path)
    except ValidationError as exc:
        _emit_env_error(output_format, exc)
        return ENV_AUTH_EXIT_CODE

    _emit_success(output_format)
    return 0


# ─────────────────────────────────────────────────────────────────────────
# Renderers
# ─────────────────────────────────────────────────────────────────────────


def _emit_success(output_format: str) -> None:
    if output_format == "json":
        sys.stdout.write(json.dumps({"valid": True}) + "\n")
        return
    render_prose("config.yaml + .env are valid", style="success")


def _emit_config_validation_error(output_format: str, exc: ConfigValidationError) -> None:
    """Render the locked config-error template (file + section + field)."""
    first = exc.errors[0]
    section = first.get("section") or "<root>"
    field = first.get("field") or first.get("loc_str") or "<unknown>"
    message = f"{exc.path}: [{section}.{field}] {first['msg']}"

    if output_format == "json":
        _json_error(message=message, exit_code=3)
        return

    render_prose(
        message,
        style="error",
        hint=f"see config.example.yaml for the {section} schema",
    )

    extras = len(exc.errors) - 1
    if extras > 0:
        render_prose(
            f"{extras} additional config error(s) — fix one at a time and re-run.",
            style="secondary",
        )


def _emit_config_parse_error(output_format: str, exc: ConfigParseError) -> None:
    message = f"{exc.path}:{exc.line}:{exc.column}: malformed YAML"
    if output_format == "json":
        _json_error(message=message, exit_code=3)
        return
    render_prose(
        message,
        style="error",
        hint="check indentation and quoting near the reported line",
    )


def _emit_env_error(output_format: str, exc: ValidationError) -> None:
    """Render the locked .env error template (matches Story 2.6 exactly)."""
    missing = _first_missing_field(exc)
    message = f"missing required env var: {missing}" if missing else "invalid .env configuration"
    if output_format == "json":
        _json_error(message=message, exit_code=ENV_AUTH_EXIT_CODE)
        return
    render_prose(message, style="error", hint="see .env.example")


def _emit_error(
    output_format: str,
    *,
    message: str,
    hint: str | None,
    exit_code: int,
) -> None:
    if output_format == "json":
        _json_error(message=message, exit_code=exit_code)
        return
    render_prose(message, style="error", hint=hint)


def _json_error(*, message: str, exit_code: int) -> None:
    sys.stderr.write(
        json.dumps(
            {
                "error": "validate_config",
                "message": message,
                "exit_code": exit_code,
            }
        )
        + "\n"
    )


def _first_missing_field(exc: ValidationError) -> str | None:
    for err in exc.errors():
        if err.get("type") == "missing" and err.get("loc"):
            return str(err["loc"][0])
    return None
