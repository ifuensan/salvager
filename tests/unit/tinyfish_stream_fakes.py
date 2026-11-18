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
    ``next_response`` — or raises ``next_exception``, or hangs forever."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_response: AgentRunResponse | None = None
        self.next_exception: BaseException | None = None
        self.steps = 9
        self.hang = False
        self.progress_emitted = 0

    def stream(self, *, goal: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"goal": goal, "url": url, **kwargs})
        return contextlib.aclosing(self._events())

    async def _events(self) -> AsyncIterator[Any]:
        if self.next_exception is not None:
            raise self.next_exception
        run_id = (self.next_response.run_id if self.next_response else None) or "run-hung"
        yield StartedEvent(type=EventType.STARTED, runId=run_id, timestamp=_EVENT_TS)
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
    def __init__(self) -> None:
        self.agent = FakeAgent()
        self.closed = False
        self.cancelled: list[str] = []

    async def _request(self, method: str, path: str, **kwargs: Any) -> None:
        assert method == "POST"
        self.cancelled.append(path)

    async def close(self) -> None:
        self.closed = True
