"""Credential-file permission gate — NFR-S2 / AR22 / Story 2.11.

Every CLI command that loads credentials (the daemon, ``login wallapop``,
``login ebay``, ``validate-config``) calls
:func:`verify_credential_permissions` at startup. The check enforces that
every listed file:

  - exists, AND
  - has mode exactly ``0o600``.

A non-0600 mode is treated as a refusal to start — there is no "looks
mostly OK" tolerance. ``.env`` at 0644 means a sibling container or
another user on the host could read it; the gate makes that mistake a
loud exit, not a silent risk.

The daemon entry helper :func:`verify_or_exit` renders the Story 2.11
locked error template and exits with code ``4`` (auth) — same code the
``.env`` loader uses for missing credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hardware_hunter.config.env import ENV_AUTH_EXIT_CODE
from hardware_hunter.observability.styling import render_prose

EXPECTED_MODE = 0o600


class CredentialError(Exception):
    """Base class for any credential-file gate failure."""


class CredentialMissingError(CredentialError):
    """A required credential file does not exist on disk.

    The operator has not yet run the matching ``hardware-hunter login``
    subcommand (Wallapop cookies, eBay OAuth tokens) or has not yet
    created ``.env``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"missing credential file: {path}")


class CredentialPermissionsError(CredentialError):
    """A credential file exists but is not at mode 0600.

    ``observed_mode`` is the file's permission bits masked to the lower
    9 bits — formatted by the renderer as a four-digit octal so the
    operator sees the exact value that needs ``chmod 600``.
    """

    def __init__(self, path: Path, observed_mode: int) -> None:
        self.path = path
        self.observed_mode = observed_mode
        super().__init__(f"{path} has mode {observed_mode:04o}, expected 0600")


def verify_credential_permissions(paths: list[Path]) -> None:
    """Walk ``paths`` and raise on the first violation.

    Stops at the first failure: operators fix one credential at a time,
    and reporting them all at once would just be noise. The caller
    chooses which files are "required" for the surface in question
    (e.g. ``validate-config`` checks only ``.env``; the daemon also
    checks the marketplace credential files).
    """
    for path in paths:
        if not path.exists():
            raise CredentialMissingError(path)
        mode = path.stat().st_mode & 0o777
        if mode != EXPECTED_MODE:
            raise CredentialPermissionsError(path, mode)


def verify_or_exit(paths: list[Path]) -> None:
    """Verify and exit 4 on failure with the locked Story 2.11 templates.

    Used by every CLI surface that loads credentials. On success the
    function returns silently and the caller proceeds.
    """
    try:
        verify_credential_permissions(paths)
    except CredentialMissingError as exc:
        render_prose(
            f"missing credential file: {exc.path}",
            style="error",
            hint=_missing_hint(exc.path),
        )
        sys.exit(ENV_AUTH_EXIT_CODE)
    except CredentialPermissionsError as exc:
        render_prose(
            f"{exc.path} has mode {exc.observed_mode:04o}, expected 0600",
            style="error",
            hint=f"chmod 600 {exc.path}",
        )
        sys.exit(ENV_AUTH_EXIT_CODE)


def _missing_hint(path: Path) -> str:
    """Tailor the missing-file hint to the credential being asked about."""
    name = path.name
    if name == ".env":
        return "run hardware-hunter init, then fill in .env"
    if "wallapop" in name:
        return "run hardware-hunter login wallapop"
    if "oauth" in name or "ebay" in name:
        return "run hardware-hunter login ebay"
    return "run the matching hardware-hunter login subcommand"
