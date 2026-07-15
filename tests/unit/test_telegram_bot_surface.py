"""Tests for the Telegram bot adapter — Story 3.12."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from pydantic import SecretStr

from salvager.adapters.telegram_bot.surface import (
    DEFAULT_RETRY_DELAYS,
    TelegramBotSurface,
)
from salvager.domain.alert import InlineButton, RenderedAlert
from salvager.domain.errors import (
    TelegramConfigError,
    TelegramDeliveryFailed,
    TelegramMessageGone,
)

# ─────────────────────────────────────────────────────────────────────────
# Fake bot + fixtures
# ─────────────────────────────────────────────────────────────────────────


class _FakeMessage:
    """Stand-in for ``telegram.Message`` carrying just ``message_id``."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeBot:
    """Minimal fake satisfying :class:`TelegramBotProtocol`."""

    def __init__(
        self,
        *,
        send_message_id: int = 101,
        send_photo_id: int = 202,
        failures: list[Exception] | None = None,
    ) -> None:
        self.send_message_calls: list[dict[str, Any]] = []
        self.send_photo_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self._send_message_id = send_message_id
        self._send_photo_id = send_photo_id
        # If `failures` is non-empty, each call pops the next exception
        # until the list is empty, then returns a Message.
        self._failures = list(failures or [])

    def _maybe_raise(self) -> None:
        if self._failures:
            raise self._failures.pop(0)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        reply_to_message_id: int | None = None,
    ) -> _FakeMessage:
        self.send_message_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        self._maybe_raise()
        return _FakeMessage(self._send_message_id)

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        reply_to_message_id: int | None = None,
    ) -> _FakeMessage:
        self.send_photo_calls.append(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        self._maybe_raise()
        return _FakeMessage(self._send_photo_id)

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        *,
        reply_markup: Any = None,
    ) -> None:
        self.edit_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            }
        )
        self._maybe_raise()

    async def edit_message_caption(
        self,
        chat_id: int,
        message_id: int,
        *,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_markup: Any = None,
    ) -> None:
        self.edit_caption_calls: list[dict[str, Any]] = getattr(self, "edit_caption_calls", [])
        self.edit_caption_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        self._maybe_raise()

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
    ) -> None:
        self.edit_text_calls: list[dict[str, Any]] = getattr(self, "edit_text_calls", [])
        self.edit_text_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        self._maybe_raise()

    async def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int | None = None,
        allowed_updates: list[str] | None = None,
    ) -> list[Any]:
        # Test seam unused — listener tests inject their own bot.
        return []

    async def answer_callback_query(
        self,
        callback_query_id: str,
    ) -> None:
        self.answer_calls: list[str] = getattr(self, "answer_calls", [])
        self.answer_calls.append(callback_query_id)


class _NetworkError(Exception):
    """Class name mirrors python-telegram-bot's NetworkError for routing."""

    pass


# Rename so the surface's class-name check sees it as retryable.
_NetworkError.__name__ = "NetworkError"


class _BadRequest(Exception):
    pass


_BadRequest.__name__ = "BadRequest"


def _record_sleeps() -> tuple[list[float], Any]:
    recorded: list[float] = []

    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    return recorded, _sleep


def _build_surface(
    bot: _FakeBot,
    *,
    chat_id: int = 12345,
    retry_delays: tuple[float, ...] = (0.0, 0.0),
    sleep: Any = None,
) -> tuple[TelegramBotSurface, list[float]]:
    sleeps, sleep_fn = _record_sleeps()
    surface = TelegramBotSurface(
        SecretStr("test-token"),
        chat_id,
        bot=bot,
        retry_delays=retry_delays,
        sleep=sleep if sleep is not None else sleep_fn,
    )
    return surface, sleeps


def _rendered(*, with_photo: bool = True, with_keyboard: bool = True) -> RenderedAlert:
    keyboard = (
        [
            [
                InlineButton(text="👁 Ver", callback_data="listing:view:abc"),
                InlineButton(text="🙅 Saltar", callback_data="listing:skip:abc"),
            ]
        ]
        if with_keyboard
        else None
    )
    return RenderedAlert(
        text="📦 *Test* — *55,00 €*\n🔍 Confidence: high",
        photo_url="https://cdn/photo.jpg" if with_photo else None,
        inline_keyboard=keyboard,
    )


# ─────────────────────────────────────────────────────────────────────────
# Sending — happy path
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_with_photo_uses_send_photo_and_returns_message_id() -> None:
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    message_id = await surface.send(_rendered(with_photo=True))
    assert message_id == 202  # _FakeBot.send_photo_id
    assert len(bot.send_photo_calls) == 1
    call = bot.send_photo_calls[0]
    assert call["photo"] == "https://cdn/photo.jpg"
    assert call["parse_mode"] == "MarkdownV2"
    assert call["caption"].startswith("📦")
    assert call["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_without_photo_uses_send_message() -> None:
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    message_id = await surface.send(_rendered(with_photo=False))
    assert message_id == 101  # _FakeBot.send_message_id
    assert len(bot.send_photo_calls) == 0
    assert len(bot.send_message_calls) == 1


@pytest.mark.asyncio
async def test_send_with_no_keyboard_passes_none_to_bot() -> None:
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    await surface.send(_rendered(with_photo=False, with_keyboard=False))
    assert bot.send_message_calls[0]["reply_markup"] is None


# ─────────────────────────────────────────────────────────────────────────
# Retry policy (NFR-I6)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds() -> None:
    bot = _FakeBot(failures=[_NetworkError("temporary"), _NetworkError("still temp")])
    surface, sleeps = _build_surface(bot, retry_delays=(0.01, 0.02))
    message_id = await surface.send(_rendered(with_photo=False))
    assert message_id == 101
    assert sleeps == [0.01, 0.02]
    assert len(bot.send_message_calls) == 3


@pytest.mark.asyncio
async def test_all_retries_exhausted_raises_delivery_failed() -> None:
    bot = _FakeBot(
        failures=[_NetworkError("a"), _NetworkError("b"), _NetworkError("c")],
    )
    surface, sleeps = _build_surface(bot, retry_delays=(0.0, 0.0))
    with pytest.raises(TelegramDeliveryFailed):
        await surface.send(_rendered(with_photo=False))
    # Three attempts, two delays in between.
    assert len(bot.send_message_calls) == 3
    assert sleeps == [0.0, 0.0]


@pytest.mark.asyncio
async def test_4xx_error_is_non_retryable() -> None:
    bot = _FakeBot(failures=[_BadRequest("chat not found")])
    surface, sleeps = _build_surface(bot)
    with pytest.raises(TelegramConfigError):
        await surface.send(_rendered(with_photo=False))
    # No retry — sleep never invoked, only one attempt.
    assert len(bot.send_message_calls) == 1
    assert sleeps == []


def test_default_retry_delays_match_documented_pattern() -> None:
    """Doc comment says ~3 attempts in ~20s — default delays are
    [5.0, 15.0]."""
    assert DEFAULT_RETRY_DELAYS == (5.0, 15.0)


# ─────────────────────────────────────────────────────────────────────────
# edit_keyboard
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_keyboard_round_trips() -> None:
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    keyboard = [[InlineButton(text="✓ visto", callback_data="listing:view:abc")]]
    await surface.edit_keyboard(message_id=42, keyboard=keyboard)
    assert len(bot.edit_calls) == 1
    call = bot.edit_calls[0]
    assert call["chat_id"] == 12345
    assert call["message_id"] == 42
    assert call["reply_markup"] is not None


@pytest.mark.asyncio
async def test_edit_keyboard_with_none_passes_none() -> None:
    """Clearing the keyboard — used by the Phase 2 in-flight ack flow."""
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    await surface.edit_keyboard(message_id=42, keyboard=None)
    assert bot.edit_calls[0]["reply_markup"] is None


@pytest.mark.asyncio
async def test_edit_keyboard_translates_4xx_to_config_error() -> None:
    bot = _FakeBot(failures=[_BadRequest("message not found")])
    surface, _ = _build_surface(bot)
    with pytest.raises(TelegramConfigError):
        await surface.edit_keyboard(message_id=42, keyboard=None)


# ─────────────────────────────────────────────────────────────────────────
# Callback parsing (AR20 chat-ID allowlist)
# ─────────────────────────────────────────────────────────────────────────


def test_parse_callback_returns_typed_event_for_known_chat() -> None:
    surface, _ = _build_surface(_FakeBot(), chat_id=12345)
    event = surface.parse_callback(
        chat_id=12345,
        message_id=99,
        callback_query_id="cb-1",
        callback_data="listing:view:abc-uuid",
    )
    assert event is not None
    assert event.verb == "view"
    assert event.callback_data == "listing:view:abc-uuid"
    assert event.chat_id == 12345
    assert event.message_id == 99


def test_parse_callback_drops_unknown_chat_id_silently() -> None:
    """AR20: chat IDs outside the allowlist are silently dropped."""
    surface, _ = _build_surface(_FakeBot(), chat_id=12345)
    event = surface.parse_callback(
        chat_id=999_999_999,  # not the configured operator
        message_id=99,
        callback_query_id="cb-2",
        callback_data="listing:view:abc",
    )
    assert event is None


def test_parse_callback_drops_malformed_callback_data() -> None:
    surface, _ = _build_surface(_FakeBot(), chat_id=12345)
    event = surface.parse_callback(
        chat_id=12345,
        message_id=99,
        callback_query_id="cb-3",
        callback_data="bogus_data_without_separators",
    )
    assert event is None


def test_parse_callback_drops_unknown_verb() -> None:
    surface, _ = _build_surface(_FakeBot(), chat_id=12345)
    event = surface.parse_callback(
        chat_id=12345,
        message_id=99,
        callback_query_id="cb-4",
        callback_data="listing:dance:abc",  # 'dance' isn't a valid verb
    )
    assert event is None


@pytest.mark.parametrize("verb", ["view", "skip", "snooze", "buy"])
def test_parse_callback_accepts_every_documented_verb(verb: str) -> None:
    surface, _ = _build_surface(_FakeBot(), chat_id=12345)
    event = surface.parse_callback(
        chat_id=12345,
        message_id=99,
        callback_query_id="cb-x",
        callback_data=f"listing:{verb}:abc",
    )
    assert event is not None
    assert event.verb == verb


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline — only this package imports telegram.*
# ─────────────────────────────────────────────────────────────────────────


def test_no_other_package_imports_telegram() -> None:
    """NFR-M1: telegram.* allowed only in adapters/telegram_bot/."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / "src" / "salvager"
    for path in src_dir.rglob("*.py"):
        if "telegram_bot" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("telegram"), (
                        f"{path.relative_to(repo_root)}: forbidden telegram import"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("telegram"), (
                    f"{path.relative_to(repo_root)}: forbidden 'from {module} import …'"
                )


# ─────────────────────────────────────────────────────────────────────────
# listen_callbacks — long-poll loop + dispatch
# ─────────────────────────────────────────────────────────────────────────


class _FakeCallbackQuery:
    """Stand-in for ``telegram.CallbackQuery`` carrying only the fields
    the listener reads."""

    def __init__(self, *, query_id: str, chat_id: int, message_id: int, data: str) -> None:
        self.id = query_id
        self.data = data
        # The real CallbackQuery exposes ``message`` (a Message which has
        # ``chat`` and ``message_id``); replicate that shape.
        self.message = _FakeMessageWithChat(chat_id=chat_id, message_id=message_id)


class _FakeMessageWithChat:
    def __init__(self, *, chat_id: int, message_id: int) -> None:
        self.message_id = message_id
        self.chat = _FakeChat(chat_id)


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, *, update_id: int, callback_query: Any = None) -> None:
        self.update_id = update_id
        self.callback_query = callback_query


class _ListenerFakeBot(_FakeBot):
    """Variant that hands out scripted ``get_updates`` batches.

    Each batch is a single list of Updates. After the configured
    batches run out the next call raises ``asyncio.CancelledError`` so
    the listener exits cleanly — matches the production daemon's
    cancel-on-shutdown signal.
    """

    def __init__(self, batches: list[list[_FakeUpdate]]) -> None:
        super().__init__()
        self._batches = list(batches)
        self.get_updates_calls: list[dict[str, Any]] = []
        self.answer_calls: list[str] = []

    async def get_updates(
        self,
        offset: int | None = None,
        limit: int | None = None,
        timeout: int | None = None,
        allowed_updates: list[str] | None = None,
    ) -> list[Any]:
        self.get_updates_calls.append(
            {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates}
        )
        if not self._batches:
            raise asyncio.CancelledError
        return self._batches.pop(0)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        self.answer_calls.append(callback_query_id)


async def _drain_listener(surface: Any, handler: Any) -> None:
    """Run the listener to completion; CancelledError ends the loop."""
    with contextlib.suppress(asyncio.CancelledError):
        await surface.listen_callbacks(handler)


@pytest.mark.asyncio
async def test_listen_callbacks_dispatches_each_callback_to_handler() -> None:
    """The happy path: one Update with a callback_query → handler
    gets the parsed event with the chat / message / verb pulled out
    of the Telegram payload."""
    cb = _FakeCallbackQuery(
        query_id="cq-1",
        chat_id=12345,
        message_id=99,
        data="listing:view:00000000-0000-0000-0000-000000000001",
    )
    bot = _ListenerFakeBot(batches=[[_FakeUpdate(update_id=10, callback_query=cb)]])
    surface, _ = _build_surface(bot, chat_id=12345)

    handled: list[Any] = []

    async def _handler(event: Any) -> None:
        handled.append(event)

    await _drain_listener(surface, _handler)

    assert len(handled) == 1
    assert handled[0].verb == "view"
    assert handled[0].message_id == 99
    assert handled[0].callback_query_id == "cq-1"


@pytest.mark.asyncio
async def test_listen_callbacks_advances_offset_to_update_id_plus_one() -> None:
    """Offset semantics: the next ``get_updates`` must use
    ``last_seen_update_id + 1`` so Telegram doesn't re-send the same
    callback — double-recording the audit row would be a real bug."""
    cb = _FakeCallbackQuery(
        query_id="cq-1",
        chat_id=12345,
        message_id=99,
        data="listing:view:00000000-0000-0000-0000-000000000001",
    )
    bot = _ListenerFakeBot(batches=[[_FakeUpdate(update_id=42, callback_query=cb)]])
    surface, _ = _build_surface(bot, chat_id=12345)

    async def _handler(_event: Any) -> None:
        pass

    await _drain_listener(surface, _handler)

    # Two get_updates calls: first with offset=None, second with 43.
    # (The second call is the one that raises CancelledError to end the loop.)
    assert len(bot.get_updates_calls) == 2
    assert bot.get_updates_calls[0]["offset"] is None
    assert bot.get_updates_calls[1]["offset"] == 43


@pytest.mark.asyncio
async def test_listen_callbacks_acks_every_callback_regardless_of_handler_outcome() -> None:
    """``answer_callback_query`` runs for ALL paths: parse-drop,
    handler success, handler raised. Without this Telegram's loading
    spinner sits on the operator's screen until it times out — that
    looks broken even when the actual effect landed."""
    happy = _FakeCallbackQuery(
        query_id="cq-ok",
        chat_id=12345,
        message_id=1,
        data="listing:view:00000000-0000-0000-0000-000000000001",
    )
    raises_in_handler = _FakeCallbackQuery(
        query_id="cq-boom",
        chat_id=12345,
        message_id=2,
        data="listing:skip:00000000-0000-0000-0000-000000000002",
    )
    wrong_chat = _FakeCallbackQuery(
        query_id="cq-stranger",
        chat_id=99999,  # not on the allowlist
        message_id=3,
        data="listing:view:00000000-0000-0000-0000-000000000003",
    )
    bot = _ListenerFakeBot(
        batches=[
            [
                _FakeUpdate(update_id=1, callback_query=happy),
                _FakeUpdate(update_id=2, callback_query=raises_in_handler),
                _FakeUpdate(update_id=3, callback_query=wrong_chat),
            ]
        ]
    )
    surface, _ = _build_surface(bot, chat_id=12345)

    async def _handler(event: Any) -> None:
        if event.callback_query_id == "cq-boom":
            raise RuntimeError("handler exploded")

    await _drain_listener(surface, _handler)

    assert set(bot.answer_calls) == {"cq-ok", "cq-boom", "cq-stranger"}


@pytest.mark.asyncio
async def test_listen_callbacks_retries_on_transient_get_updates_failure() -> None:
    """A NetworkError on get_updates triggers backoff (via the
    injected sleep) and the next attempt resumes. The handler never
    sees the transient error."""
    cb = _FakeCallbackQuery(
        query_id="cq-1",
        chat_id=12345,
        message_id=99,
        data="listing:view:00000000-0000-0000-0000-000000000001",
    )

    class _FlakeyBot(_ListenerFakeBot):
        def __init__(self) -> None:
            super().__init__(batches=[[_FakeUpdate(update_id=10, callback_query=cb)]])
            self._fail_next = True

        async def get_updates(
            self,
            offset: int | None = None,
            limit: int | None = None,
            timeout: int | None = None,
            allowed_updates: list[str] | None = None,
        ) -> list[Any]:
            if self._fail_next:
                self._fail_next = False
                raise _NetworkError("transient")
            return await super().get_updates(
                offset=offset,
                limit=limit,
                timeout=timeout,
                allowed_updates=allowed_updates,
            )

    bot = _FlakeyBot()
    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    surface, _ = _build_surface(bot, chat_id=12345, sleep=_record_sleep)

    handled: list[Any] = []

    async def _handler(event: Any) -> None:
        handled.append(event)

    await _drain_listener(surface, _handler)

    assert sleeps  # at least one backoff happened
    assert len(handled) == 1  # the callback still got dispatched after retry


@pytest.mark.asyncio
async def test_listen_callbacks_raises_delivery_failed_when_retry_delays_empty() -> None:
    """``retry_delays=()`` opts out of retries (matches ``send()``).

    Before the fix from PR #8, this case crashed the listener with an
    IndexError because the indexing used ``len(()) - 1 == -1`` and
    ``()[-1]`` raises. Behaviour now matches the send path: a
    retryable error with no retries to give surfaces as
    ``TelegramDeliveryFailed``, which the supervisor in ``_serve``
    catches and logs.
    """

    class _AlwaysFlakyBot(_FakeBot):
        async def get_updates(
            self,
            offset: int | None = None,
            limit: int | None = None,
            timeout: int | None = None,
            allowed_updates: list[str] | None = None,
        ) -> list[Any]:
            raise _NetworkError("transient")

    bot = _AlwaysFlakyBot()
    surface, _ = _build_surface(bot, chat_id=12345, retry_delays=())

    async def _handler(_event: Any) -> None:
        pass

    with pytest.raises(TelegramDeliveryFailed):
        await surface.listen_callbacks(_handler)


@pytest.mark.asyncio
async def test_listen_callbacks_raises_config_error_on_non_retryable_failure() -> None:
    """A 4xx-class error (e.g. invalid token) is not a transient
    blip — bubble it up so the daemon surfaces it loudly instead of
    silently retrying forever."""

    class _BadAuthBot(_FakeBot):
        async def get_updates(
            self,
            offset: int | None = None,
            limit: int | None = None,
            timeout: int | None = None,
            allowed_updates: list[str] | None = None,
        ) -> list[Any]:
            raise _BadRequest("invalid bot token")

    bot = _BadAuthBot()
    surface, _ = _build_surface(bot, chat_id=12345)

    async def _handler(_event: Any) -> None:
        pass

    with pytest.raises(TelegramConfigError):
        await surface.listen_callbacks(_handler)


# ─────────────────────────────────────────────────────────────────────────
# edit_alert — body edits (edit-alerts-on-state-change)
# ─────────────────────────────────────────────────────────────────────────


async def test_edit_alert_photo_branch_uses_edit_message_caption() -> None:
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    rendered = _rendered(with_photo=True)

    await surface.edit_alert(4711, rendered, has_photo=True)

    [call] = bot.edit_caption_calls
    assert call["message_id"] == 4711
    assert call["caption"] == rendered.text
    assert call["reply_markup"] is not None  # keyboard always re-sent explicitly
    assert not getattr(bot, "edit_text_calls", [])


async def test_edit_alert_text_branch_uses_edit_message_text() -> None:
    bot = _FakeBot()
    surface, _ = _build_surface(bot)
    rendered = _rendered(with_photo=False)

    await surface.edit_alert(4711, rendered, has_photo=False)

    [call] = bot.edit_text_calls
    assert call["text"] == rendered.text
    assert call["reply_markup"] is not None
    assert not getattr(bot, "edit_caption_calls", [])


async def test_edit_alert_not_modified_is_silent_success() -> None:
    bot = _FakeBot(failures=[_BadRequest("Message is not modified")])
    surface, _ = _build_surface(bot)

    # No exception — identical re-render counts as a successful edit.
    await surface.edit_alert(4711, _rendered(), has_photo=True)


async def test_edit_alert_message_gone_raises_terminal_error() -> None:
    bot = _FakeBot(failures=[_BadRequest("Message to edit not found")])
    surface, _ = _build_surface(bot)

    with pytest.raises(TelegramMessageGone):
        await surface.edit_alert(4711, _rendered(), has_photo=True)


async def test_edit_alert_transient_failure_is_single_attempt() -> None:
    """No in-cycle retry: a transient failure raises immediately (the next
    poll cycle re-diffs and retries) and consumes exactly one bot call."""
    bot = _FakeBot(failures=[_NetworkError("temporary")])
    surface, sleeps = _build_surface(bot)

    with pytest.raises(TelegramDeliveryFailed):
        await surface.edit_alert(4711, _rendered(), has_photo=True)

    assert len(bot.edit_caption_calls) == 1  # exactly one attempt
    assert sleeps == []  # and no retry sleeps


async def test_edit_alert_non_retryable_maps_to_config_error() -> None:
    bot = _FakeBot(failures=[_BadRequest("chat not found")])
    surface, _ = _build_surface(bot)

    with pytest.raises(TelegramConfigError):
        await surface.edit_alert(4711, _rendered(), has_photo=False)


async def test_send_threads_reply_to_message_id() -> None:
    """The big-drop ping is a Telegram reply pointing at the edited alert."""
    bot = _FakeBot()
    surface, _ = _build_surface(bot)

    await surface.send(_rendered(with_photo=False), reply_to_message_id=4711)

    [call] = bot.send_message_calls
    assert call["reply_to_message_id"] == 4711
