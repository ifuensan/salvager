"""Telegram bot :class:`TelegramSurface` — Story 3.12.

Wraps ``python-telegram-bot`` with the retry semantics + chat-ID
allowlist this project requires.

Retry policy (NFR-I6)
---------------------
Transient failures (network errors, timeouts, HTTP 5xx, RetryAfter)
are retried with exponential backoff: 3 attempts total, default delays
of 5s and 15s between them. After the third failure the surface
raises :class:`TelegramDeliveryFailed`; the orchestration layer
swallows the error and continues — delivery failure must NOT block
polling.

Non-retryable failures (HTTP 4xx — invalid token, chat not found,
bot kicked) raise :class:`TelegramConfigError` immediately so the
operator gets a loud signal instead of silent retry-storms.

Chat-ID allowlist (AR20)
------------------------
``parse_callback_query`` drops anything arriving from a chat ID other
than the configured ``recipient_chat_id``. The drop is silent — no
visible reply, no log spam beyond a single ``debug`` line. The bot
talks to one operator; everything else is noise.

Test seam
---------
Both the bot and the ``sleep`` function are dependency-injected so
unit tests can run fast (sleep no-op) against a fake bot that
records calls and synthesizes failures.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import SecretStr

from salvager.domain.alert import (
    CallbackEvent,
    CallbackVerb,
    InlineButton,
    RenderedAlert,
)
from salvager.domain.errors import (
    TelegramConfigError,
    TelegramDeliveryFailed,
    TelegramMessageGone,
)
from salvager.interfaces.telegram_surface import (
    CallbackHandler,
    TelegramSurface,
)
from salvager.observability.logging import get_logger

#: Pause-between-retries in seconds. Defaults give ~3 attempts in ~20s.
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)

_KNOWN_VERBS: frozenset[str] = frozenset({"view", "skip", "snooze", "buy", "offer"})


@runtime_checkable
class TelegramBotProtocol(Protocol):
    """The slice of ``telegram.Bot`` that this adapter actually calls.

    A Protocol so tests can pass an in-memory fake without dragging
    the real Bot class. The signatures match python-telegram-bot's
    async API.
    """

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = ...,
        reply_markup: Any = ...,
        reply_to_message_id: int | None = ...,
    ) -> Any: ...

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str | None = ...,
        parse_mode: str | None = ...,
        reply_markup: Any = ...,
        reply_to_message_id: int | None = ...,
    ) -> Any: ...

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        *,
        reply_markup: Any = ...,
    ) -> Any: ...

    async def edit_message_caption(
        self,
        chat_id: int,
        message_id: int,
        *,
        caption: str | None = ...,
        parse_mode: str | None = ...,
        reply_markup: Any = ...,
    ) -> Any: ...

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | None = ...,
        message_id: int | None = ...,
        *,
        parse_mode: str | None = ...,
        reply_markup: Any = ...,
    ) -> Any: ...

    async def get_updates(
        self,
        offset: int | None = ...,
        limit: int | None = ...,
        timeout: int | None = ...,
        allowed_updates: list[str] | None = ...,
    ) -> Any: ...

    async def answer_callback_query(
        self,
        callback_query_id: str,
    ) -> Any: ...


class TelegramBotSurface(TelegramSurface):
    """``TelegramSurface`` backed by ``python-telegram-bot``."""

    def __init__(
        self,
        bot_token: SecretStr,
        recipient_chat_id: int,
        *,
        bot: TelegramBotProtocol | None = None,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot_token = bot_token
        self._recipient_chat_id = recipient_chat_id
        self._bot: TelegramBotProtocol = bot if bot is not None else _build_default_bot(bot_token)
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._log = get_logger("adapter.telegram_bot")

    # ─────────────────────────────────────────────────────────────────
    # TelegramSurface — send / edit_keyboard / listen_callbacks
    # ─────────────────────────────────────────────────────────────────

    async def send(
        self,
        rendered: RenderedAlert,
        *,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Send a rendered alert; return Telegram's ``message_id``.

        Retries transient failures with exponential backoff. 4xx
        (config) failures bail out immediately.
        """
        attempt = 0
        started = time.perf_counter()
        while True:
            try:
                message = await self._invoke_send(rendered, reply_to_message_id)
            except _RetryableTelegramError as exc:
                if attempt >= len(self._retry_delays):
                    self._log.error(
                        "telegram_send_failed",
                        extra={
                            "error_class": exc.original.__class__.__name__,
                            "attempts": attempt + 1,
                        },
                    )
                    raise TelegramDeliveryFailed(
                        f"send failed after {attempt + 1} attempts: {exc.original}"
                    ) from exc.original
                delay = self._retry_delays[attempt]
                self._log.warning(
                    "telegram_send_retry",
                    extra={
                        "error_class": exc.original.__class__.__name__,
                        "attempt": attempt + 1,
                        "delay_s": delay,
                    },
                )
                await self._sleep(delay)
                attempt += 1
                continue
            except _NonRetryableTelegramError as exc:
                self._log.error(
                    "telegram_config_error",
                    extra={"error_class": exc.original.__class__.__name__},
                )
                raise TelegramConfigError(str(exc.original)) from exc.original

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._log.info(
                "telegram_alert_sent",
                extra={
                    "latency_ms": elapsed_ms,
                    "message_id": message.message_id,
                    "attempts": attempt + 1,
                },
            )
            return int(message.message_id)

    async def edit_alert(
        self,
        message_id: int,
        rendered: RenderedAlert,
        *,
        has_photo: bool,
    ) -> None:
        """Edit an alert body in place — single attempt, no retry loop.

        Edits are non-critical: the poll cycle re-diffs and retries
        naturally, so a transient failure raises
        :class:`TelegramDeliveryFailed` immediately instead of burning
        the send retry budget. Two BadRequest variants are semantic:
        "message is not modified" is success (identical re-render);
        "message to edit not found" means the operator deleted the
        alert → :class:`TelegramMessageGone` (terminal for the watch).
        """
        markup = _to_telegram_keyboard(rendered.inline_keyboard)
        try:
            if has_photo:
                await self._bot.edit_message_caption(
                    self._recipient_chat_id,
                    message_id,
                    caption=rendered.text,
                    parse_mode=rendered.parse_mode,
                    reply_markup=markup,
                )
            else:
                await self._bot.edit_message_text(
                    rendered.text,
                    self._recipient_chat_id,
                    message_id,
                    parse_mode=rendered.parse_mode,
                    reply_markup=markup,
                )
        except Exception as exc:
            lowered = str(exc).lower()
            if "message is not modified" in lowered:
                return  # identical content — semantically a successful edit
            if "message to edit not found" in lowered:
                self._log.info(
                    "telegram_edit_target_gone",
                    extra={"message_id": message_id},
                )
                raise TelegramMessageGone(str(exc)) from exc
            if _is_retryable(exc):
                self._log.warning(
                    "telegram_edit_alert_failed",
                    extra={"error_class": exc.__class__.__name__, "message_id": message_id},
                )
                raise TelegramDeliveryFailed(str(exc)) from exc
            self._log.error(
                "telegram_edit_alert_config_error",
                extra={"error_class": exc.__class__.__name__, "message_id": message_id},
            )
            raise TelegramConfigError(str(exc)) from exc
        self._log.info(
            "telegram_alert_edited",
            extra={"message_id": message_id, "has_photo": has_photo},
        )

    async def edit_keyboard(
        self,
        message_id: int,
        keyboard: list[list[InlineButton]] | None,
    ) -> None:
        try:
            await self._bot.edit_message_reply_markup(
                self._recipient_chat_id,
                message_id,
                reply_markup=_to_telegram_keyboard(keyboard),
            )
        except Exception as exc:
            if _is_retryable(exc):
                self._log.warning(
                    "telegram_edit_failed",
                    extra={"error_class": exc.__class__.__name__},
                )
                raise TelegramDeliveryFailed(str(exc)) from exc
            self._log.error(
                "telegram_edit_config_error",
                extra={"error_class": exc.__class__.__name__},
            )
            raise TelegramConfigError(str(exc)) from exc

    async def listen_callbacks(self, handler: CallbackHandler) -> None:
        """Long-poll Telegram for callback queries and dispatch them.

        Runs until cancelled (``asyncio.CancelledError``) — the daemon
        runs this concurrently with the scheduler and cancels it on
        SIGTERM/SIGINT. For each ``callback_query`` update:

        1. Parse it via :meth:`parse_callback` — this drops malformed
           data and off-allowlist chat IDs silently.
        2. Invoke the handler. Handler exceptions are caught + logged;
           the loop never dies on one bad tap.
        3. ``answer_callback_query`` so Telegram stops the operator-
           visible loading spinner. Acknowledgment is best-effort — if
           the API call fails we log and move on; the keyboard edit
           the dispatcher made is what the operator actually sees.

        Transient ``get_updates`` failures (network, RetryAfter) back
        off by the configured retry delays and resume. Non-retryable
        failures raise :class:`TelegramConfigError` so the daemon's
        supervisor task surfaces them loudly.

        Offset bookkeeping: Telegram's ``get_updates`` is at-least-once
        until the next call passes ``offset=update_id+1``. We advance
        the offset after each update so the next ``get_updates`` only
        returns NEW updates — re-fetching the same callback would
        double-record the audit row.
        """
        offset: int | None = None
        backoff_index = 0
        while True:
            try:
                updates = await self._invoke_get_updates(offset=offset)
            except _RetryableTelegramError as exc:
                if not self._retry_delays:
                    # `retry_delays=()` opts out of retries entirely
                    # (matches the send() semantics). Without this
                    # guard, indexing an empty tuple at ``-1`` would
                    # crash the listener loop on the first transient
                    # blip — caught by Devin on PR #8.
                    self._log.error(
                        "telegram_listen_failed_no_retries_configured",
                        extra={"error_class": exc.original.__class__.__name__},
                    )
                    raise TelegramDeliveryFailed(
                        f"get_updates failed (no retries configured): {exc.original}"
                    ) from exc.original
                delay = self._retry_delays[min(backoff_index, len(self._retry_delays) - 1)]
                self._log.warning(
                    "telegram_get_updates_retry",
                    extra={
                        "error_class": exc.original.__class__.__name__,
                        "delay_s": delay,
                    },
                )
                await self._sleep(delay)
                backoff_index += 1
                continue
            except _NonRetryableTelegramError as exc:
                self._log.error(
                    "telegram_listen_config_error",
                    extra={"error_class": exc.original.__class__.__name__},
                )
                raise TelegramConfigError(str(exc.original)) from exc.original

            backoff_index = 0
            for update in updates:
                offset = self._max_update_id(offset, update)
                await self._dispatch_update(update, handler)

    async def _invoke_get_updates(self, *, offset: int | None) -> list[Any]:
        """One ``get_updates`` call with retry-classification.

        Long-polls for 30 s server-side — the call returns immediately
        when a callback arrives, otherwise blocks until the timeout.
        We constrain ``allowed_updates`` to ``callback_query`` so we
        don't process group messages, edited messages, etc. (the bot
        is a one-operator surface).
        """
        try:
            updates = await self._bot.get_updates(
                offset=offset,
                timeout=30,
                allowed_updates=["callback_query"],
            )
        except Exception as exc:
            if _is_retryable(exc):
                raise _RetryableTelegramError(exc) from exc
            raise _NonRetryableTelegramError(exc) from exc
        return list(updates)

    @staticmethod
    def _max_update_id(current_offset: int | None, update: Any) -> int:
        """Compute the offset to pass to the NEXT ``get_updates`` call.

        Telegram increments ``update_id`` per update; the docs say to
        pass ``last_seen_id + 1`` to acknowledge consumption.
        """
        update_id = int(update.update_id)
        candidate = update_id + 1
        if current_offset is None:
            return candidate
        return max(current_offset, candidate)

    async def _dispatch_update(self, update: Any, handler: CallbackHandler) -> None:
        """Parse + hand off one Telegram Update; never raises."""
        callback_query = getattr(update, "callback_query", None)
        if callback_query is None:
            return  # not a callback (e.g. a stray non-allowed update type)

        callback_id = str(callback_query.id)
        message = getattr(callback_query, "message", None)
        if message is None:
            # Telegram allows callback_query without an attached message
            # (e.g. inline-mode replies). Our buttons always travel
            # alongside a message, so the absence is a sign of stray
            # input — ack it and drop it.
            await self._ack_callback_best_effort(callback_id)
            return

        event = self.parse_callback(
            chat_id=int(message.chat.id),
            message_id=int(message.message_id),
            callback_query_id=callback_id,
            callback_data=str(callback_query.data or ""),
        )
        if event is None:
            # parse_callback already logged the reason at debug.
            await self._ack_callback_best_effort(callback_id)
            return

        try:
            await handler(event)
        except Exception as exc:
            # Handler is application-layer code; a single bad tap must
            # not kill the daemon's listener loop.
            self._log.error(
                "telegram_callback_handler_failed",
                extra={
                    "error_class": exc.__class__.__name__,
                    "callback_data": event.callback_data,
                },
            )
        await self._ack_callback_best_effort(callback_id)

    async def _ack_callback_best_effort(self, callback_query_id: str) -> None:
        """Stop Telegram's loading-spinner on the operator side.

        Failure here is non-fatal: the keyboard edit the dispatcher
        already made is the real "tap registered" signal; this call
        just clears the transient spinner.
        """
        try:
            await self._bot.answer_callback_query(callback_query_id)
        except Exception as exc:
            self._log.warning(
                "telegram_answer_callback_failed",
                extra={"error_class": exc.__class__.__name__},
            )

    # ─────────────────────────────────────────────────────────────────
    # Test affordances + production helper for the future orchestrator
    # ─────────────────────────────────────────────────────────────────

    def parse_callback(
        self,
        *,
        chat_id: int,
        message_id: int,
        callback_query_id: str,
        callback_data: str,
    ) -> CallbackEvent | None:
        """Parse one inbound Telegram callback into a typed event.

        Returns None when the chat ID is not the configured operator
        (AR20 chat-ID allowlist). The drop is silent at the operator
        surface; a single ``debug`` log line records the event.

        Malformed callback_data (wrong shape, unknown verb) also
        returns None — the bot stays well-behaved when third parties
        guess at our format.
        """
        if chat_id != self._recipient_chat_id:
            self._log.debug(
                "telegram_inbound_unknown_chat",
                extra={"chat_id": chat_id, "expected": self._recipient_chat_id},
            )
            return None

        parts = callback_data.split(":")
        if len(parts) != 3:
            self._log.debug(
                "telegram_inbound_malformed_callback",
                extra={"callback_data": callback_data},
            )
            return None

        _surface, verb, _alert_id = parts
        if verb not in _KNOWN_VERBS:
            self._log.debug(
                "telegram_inbound_unknown_verb",
                extra={"verb": verb},
            )
            return None

        return CallbackEvent(
            callback_query_id=callback_query_id,
            chat_id=chat_id,
            message_id=message_id,
            callback_data=callback_data,
            verb=cast(CallbackVerb, verb),
        )

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    async def _invoke_send(self, rendered: RenderedAlert, reply_to_message_id: int | None) -> Any:
        markup = _to_telegram_keyboard(rendered.inline_keyboard)
        try:
            if rendered.photo_url is not None:
                return await self._bot.send_photo(
                    self._recipient_chat_id,
                    photo=rendered.photo_url,
                    caption=rendered.text,
                    parse_mode=rendered.parse_mode,
                    reply_markup=markup,
                    reply_to_message_id=reply_to_message_id,
                )
            return await self._bot.send_message(
                self._recipient_chat_id,
                text=rendered.text,
                parse_mode=rendered.parse_mode,
                reply_markup=markup,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:
            if _is_retryable(exc):
                raise _RetryableTelegramError(exc) from exc
            raise _NonRetryableTelegramError(exc) from exc


# ─────────────────────────────────────────────────────────────────────────
# Error classification + keyboard conversion
# ─────────────────────────────────────────────────────────────────────────


class _RetryableTelegramError(Exception):
    """Internal wrapper marking a Telegram exception as retry-worthy."""

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(str(original))


class _NonRetryableTelegramError(Exception):
    """Internal wrapper marking a Telegram exception as a config failure."""

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(str(original))


_RETRYABLE_CLASS_NAMES: frozenset[str] = frozenset(
    {
        "NetworkError",
        "TimedOut",
        "RetryAfter",
    }
)


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether an exception from ``telegram.error`` is retry-worthy.

    We inspect the class name (rather than catching specific types) so
    the adapter doesn't have to import ``telegram.error.*`` at module
    scope — keeps the import surface small and tests fast.
    """
    cls_name = exc.__class__.__name__
    return cls_name in _RETRYABLE_CLASS_NAMES


def _to_telegram_keyboard(
    keyboard: list[list[InlineButton]] | None,
) -> Any:
    """Convert our domain :class:`InlineButton` rows into the python-
    telegram-bot ``InlineKeyboardMarkup`` shape."""
    if keyboard is None:
        return None
    # Lazy import keeps telegram.* out of the module import graph for tests.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data) for btn in row]
            for row in keyboard
        ]
    )


def _build_default_bot(bot_token: SecretStr) -> TelegramBotProtocol:
    """Construct the production ``telegram.Bot`` instance.

    The import is lazy so tests that inject a fake never pull
    python-telegram-bot, and so the adapter-discipline lint sees
    ``telegram.*`` used exclusively inside this adapter package.
    """
    from telegram import Bot

    return Bot(token=bot_token.get_secret_value())


# Re-export a stable name so the test suite can assert callback_data
# parsing without importing the private alias.
__all__ = [
    "DEFAULT_RETRY_DELAYS",
    "CallbackVerb",
    "TelegramBotProtocol",
    "TelegramBotSurface",
]
