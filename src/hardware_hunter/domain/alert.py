"""Alert + render-output schema — Story 3.1.

Three types live here:

  - :class:`AlertSnapshot` — the immutable record of what was alerted
    (entry x listing x evaluation). Persisted to SQLite ``alert_snapshots``
    so the callback handler can look up the originating context when
    an operator taps a button hours later.
  - :class:`RenderedAlert` — the data shape every renderer produces
    and the :class:`TelegramSurface` adapter consumes.
  - :class:`InlineButton` — one button on a Telegram inline keyboard
    row. ``callback_data`` follows the locked
    ``<surface>:<verb>:<id>`` format with the 64-byte Telegram cap.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hardware_hunter.domain.evaluation import ListingEvaluation
from hardware_hunter.domain.listing import Listing

Phase = Literal["phase1", "phase2"]
ParseMode = Literal["MarkdownV2"]

# Telegram caps inline-button callback_data at 64 bytes. The locked format
# is `<surface>:<verb>:<id>` per CALLBACK_DATA_FORMAT in the UX spec.
_CALLBACK_DATA_MAX_BYTES = 64
_CALLBACK_DATA_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+:[A-Za-z0-9_\-]+$")


class InlineButton(BaseModel):
    """One inline-keyboard button on a Telegram alert.

    ``callback_data`` is the value Telegram sends back to the bot when
    the operator taps the button. The format and byte cap are
    contract — :class:`TelegramSurface` does not re-validate.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    callback_data: str

    @field_validator("callback_data")
    @classmethod
    def _enforce_callback_format(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
            raise ValueError(
                f"callback_data exceeds Telegram's {_CALLBACK_DATA_MAX_BYTES}-byte limit"
            )
        if not _CALLBACK_DATA_RE.fullmatch(value):
            raise ValueError(
                "callback_data must match <surface>:<verb>:<id> "
                "(lowercase surface/verb, alphanumeric id)"
            )
        return value


class RenderedAlert(BaseModel):
    """Output of every renderer; input to :class:`TelegramSurface.send`.

    ``photo_url`` is None for non-listing alerts (operational warnings,
    smoke-test results). ``inline_keyboard`` is None when the alert is
    informational (no buttons).
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    parse_mode: ParseMode = "MarkdownV2"
    photo_url: str | None = None
    inline_keyboard: list[list[InlineButton]] | None = None


class AlertSnapshot(BaseModel):
    """The immutable record of one alert dispatched to the operator.

    Persisted to ``alert_snapshots`` so the callback handler can replay
    the originating context (which entry, which listing, which
    evaluation) when the operator taps a button. Phase 2 adds
    ``phase2_max_price_eur`` for the autonomous-buy gate.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: UUID
    entry_key: tuple[str, str, str]
    entry_display_name: str = Field(min_length=1)
    listing: Listing
    evaluation: ListingEvaluation
    phase: Phase
    phase2_max_price_eur: Decimal | None = None
    rendered_at: datetime
