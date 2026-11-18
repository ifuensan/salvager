"""Shared fake TinyFish client for the buy- and offer-flow tests.

The flows drive the agent over ``client.agent.stream`` and cancel runaway
runs through ``client._request``; this fake scripts the SSE event
sequence and records every cancel. Not a test module (no ``test_``
prefix), so pytest imports it only through the tests that use it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
from tinyfish import (
    AgentRunResponse,
    CompleteEvent,
    EventType,
    ProgressEvent,
    StartedEvent,
)

_EVENT_TS = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


class FakeAgent:
    """Streams STARTED, ``steps`` PROGRESS events, then COMPLETE built from
    ``next_response`` — or raises ``next_exception`` (before STARTED, or
    after it with ``fail_mid_stream``), or hangs forever (``hang``, or
    only while being closed with ``hang_on_close``)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_response: AgentRunResponse | None = None
        self.next_exception: BaseException | None = None
        self.steps = 9
        self.hang = False
        self.hang_on_close = False
        self.fail_mid_stream = False
        self.progress_emitted = 0

    def stream(self, *, goal: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"goal": goal, "url": url, **kwargs})
        return contextlib.aclosing(self._events())

    async def _events(self) -> AsyncIterator[Any]:
        try:
            async for event in self._script():
                yield event
        finally:
            if self.hang_on_close:
                await asyncio.Event().wait()

    async def _script(self) -> AsyncIterator[Any]:
        if self.next_exception is not None and not self.fail_mid_stream:
            raise self.next_exception
        run_id = (self.next_response.run_id if self.next_response else None) or "run-hung"
        yield StartedEvent(type=EventType.STARTED, runId=run_id, timestamp=_EVENT_TS)
        if self.next_exception is not None:
            raise self.next_exception
        for i in range(self.steps):
            self.progress_emitted += 1
            yield ProgressEvent(
                type=EventType.PROGRESS, runId=run_id, purpose=f"step {i}", timestamp=_EVENT_TS
            )
        if self.hang:
            await asyncio.Event().wait()
        assert self.next_response is not None, "test forgot to preload a response"
        yield CompleteEvent(
            type=EventType.COMPLETE,
            runId=run_id,
            status=self.next_response.status,
            timestamp=_EVENT_TS,
            result=self.next_response.result,
            error=self.next_response.error,
        )


class FakeClient:
    """Records every cancel POST and answers it with ``cancel_status``."""

    def __init__(self) -> None:
        self.agent = FakeAgent()
        self.closed = False
        self.cancelled: list[str] = []
        self.cancel_status = "CANCELLED"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        assert method == "POST"
        self.cancelled.append(path)
        return httpx.Response(
            200,
            json={"run_id": "r", "status": self.cancel_status},
            request=httpx.Request(method, f"https://agent.tinyfish.ai{path}"),
        )

    async def close(self) -> None:
        self.closed = True
