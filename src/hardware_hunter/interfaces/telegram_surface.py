"""``TelegramSurface`` ABC — Story 3.2 (NFR-I6 / AR20 / FR22).

The port through which the poll loop delivers alerts and listens for
operator taps. The v1 implementation is
``adapters/telegram_python_telegram_bot`` (the ``python-telegram-bot``
library, pinned in dependencies).

The chat-ID allowlist (AR20: drop any inbound from a chat ID other than
the configured operator's) lives in the concrete adapter, not here —
this ABC speaks the protocol, not the policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from hardware_hunter.domain.alert import (
    CallbackEvent,
    InlineButton,
    RenderedAlert,
)

#: Async callback invoked once per inbound :class:`CallbackEvent`.
CallbackHandler = Callable[[CallbackEvent], Awaitable[None]]


class TelegramSurface(ABC):
    """Port for the Telegram delivery + callback channel."""

    @abstractmethod
    async def send(self, rendered: RenderedAlert) -> int:
        """Deliver a rendered alert and return the Telegram message_id.

        The message_id is persisted on the alert_snapshot so callback
        handling and ``edit_keyboard`` can find the originating
        message later.
        """

    @abstractmethod
    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:
        """Replace the inline keyboard on a previously sent message.

        Passing ``None`` removes the keyboard entirely (used for the
        Phase 1 acknowledgment-row replacement and for clearing the
        ``🟡 Comprando…`` row before the Phase 2 success/failure
        message is sent).
        """

    @abstractmethod
    async def listen_callbacks(self, handler: CallbackHandler) -> None:
        """Install ``handler`` for every inbound callback tap.

        Implementations dispatch to ``handler`` after the chat-ID
        allowlist (AR20) filters out anything outside the operator's
        configured chat. Returns when the underlying polling/webhook
        loop is shut down (typically via :class:`Scheduler.shutdown`).
        """


class TelegramSurfaceError(RuntimeError):
    """Adapter could not complete a delivery or callback operation."""
