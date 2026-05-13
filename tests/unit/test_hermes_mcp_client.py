"""Tests for :class:`HermesMcpClient` — Hermes MCP transport adapter.

The MCP SDK is mocked at the constructor seam (``_stdio_factory`` +
``_session_factory``). No subprocess is spawned; no JSON-RPC bytes
flow. The fakes record calls and return preloaded responses, so the
adapter's translation logic (initialize → context-manager lifecycle;
``isError=True`` → :class:`HermesToolError`; transport exceptions →
:class:`HermesUnavailable`) is what these tests cover.

The Hermes-side integration smoke test lives elsewhere and runs
against a live ``hermes mcp serve`` once the VM is wired into CI.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, ListToolsResult, TextContent, Tool

from hardware_hunter.adapters.hermes_mcp import HermesMcpClient
from hardware_hunter.domain.errors import HermesToolError, HermesUnavailable

# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────


class _FakeStdioContext:
    """Stand-in for the ``stdio_client(params)`` async context manager.

    Yields a placeholder ``(read, write)`` tuple — the fake session
    ignores them, so they don't need to be real streams.
    """

    def __init__(
        self,
        params: Any,
        *,
        raise_on_enter: BaseException | None = None,
    ) -> None:
        self.params = params
        self._raise_on_enter = raise_on_enter
        self.aenter_count = 0
        self.aexit_count = 0

    async def __aenter__(self) -> tuple[object, object]:
        self.aenter_count += 1
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return (object(), object())

    async def __aexit__(self, *args: object) -> None:
        self.aexit_count += 1


class _FakeSession:
    """Stand-in for ``ClientSession(read, write)``.

    The instance is its own async context manager. Tests preload
    ``initialize_raises`` / ``list_tools_response`` /
    ``call_tool_responses`` to drive the adapter through happy and
    error paths.
    """

    def __init__(self, _read: object, _write: object) -> None:
        self.initialize_calls = 0
        self.initialize_raises: BaseException | None = None
        self.list_tools_response: ListToolsResult | BaseException | None = None
        self.call_tool_responses: dict[str, CallToolResult | BaseException] = {}
        self.call_tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.aenter_count = 0
        self.aexit_count = 0

    async def __aenter__(self) -> _FakeSession:
        self.aenter_count += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.aexit_count += 1

    async def initialize(self) -> None:
        self.initialize_calls += 1
        if self.initialize_raises is not None:
            raise self.initialize_raises

    async def list_tools(self) -> ListToolsResult:
        resp = self.list_tools_response
        if isinstance(resp, BaseException):
            raise resp
        if resp is None:
            return ListToolsResult(tools=[])
        return resp

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        self.call_tool_calls.append((name, dict(arguments or {})))
        resp = self.call_tool_responses.get(name)
        if isinstance(resp, BaseException):
            raise resp
        if resp is None:
            return CallToolResult(content=[], isError=False)
        return resp


def _stdio_factory(
    *,
    raise_on_enter: BaseException | None = None,
) -> tuple[list[_FakeStdioContext], Any]:
    """Build a stdio factory closure that records every invocation."""
    contexts: list[_FakeStdioContext] = []

    def _factory(params: Any) -> _FakeStdioContext:
        ctx = _FakeStdioContext(params, raise_on_enter=raise_on_enter)
        contexts.append(ctx)
        return ctx

    return contexts, _factory


def _session_factory() -> tuple[list[_FakeSession], Any]:
    """Build a session factory closure that records every constructed session."""
    sessions: list[_FakeSession] = []

    def _factory(read: object, write: object) -> _FakeSession:
        session = _FakeSession(read, write)
        sessions.append(session)
        return session

    return sessions, _factory


# ─────────────────────────────────────────────────────────────────────────
# Happy path — initialize, list_tools, call_tool, close
# ─────────────────────────────────────────────────────────────────────────


async def test_aenter_spawns_subprocess_and_initializes() -> None:
    stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()

    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        # The stdio + session ctx managers entered; initialize() was called.
        assert len(stdio_ctx) == 1
        assert stdio_ctx[0].aenter_count == 1
        assert len(sessions) == 1
        assert sessions[0].initialize_calls == 1
        assert client is not None

    # And both exited on `async with` exit.
    assert stdio_ctx[0].aexit_count == 1
    assert sessions[0].aexit_count == 1


async def test_default_stdio_command_targets_hermes_mcp_serve() -> None:
    stdio_ctx, stdio = _stdio_factory()
    _sessions, session = _session_factory()

    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session):
        pass

    params = stdio_ctx[0].params
    assert params.command == "hermes"
    assert params.args == ["mcp", "serve"]


async def test_constructor_args_override_defaults() -> None:
    stdio_ctx, stdio = _stdio_factory()
    _sessions, session = _session_factory()

    async with HermesMcpClient(
        command="/opt/hermes/bin/hermes",
        args=("mcp", "serve", "--verbose"),
        env={"HERMES_ACCEPT_HOOKS": "1"},
        _stdio_factory=stdio,
        _session_factory=session,
    ):
        pass

    params = stdio_ctx[0].params
    assert params.command == "/opt/hermes/bin/hermes"
    assert params.args == ["mcp", "serve", "--verbose"]
    assert params.env == {"HERMES_ACCEPT_HOOKS": "1"}


async def test_list_tools_returns_session_tools() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    expected_tools = [
        Tool(name="memory_get", description="read", inputSchema={"type": "object"}),
        Tool(name="memory_set", description="write", inputSchema={"type": "object"}),
    ]

    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].list_tools_response = ListToolsResult(tools=expected_tools)
        tools = await client.list_tools()

    assert [t.name for t in tools] == ["memory_get", "memory_set"]


async def test_call_tool_returns_session_result_on_success() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    expected = CallToolResult(
        content=[TextContent(type="text", text='{"hit": true, "value": "..."}')],
        isError=False,
    )

    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].call_tool_responses["memory_get"] = expected
        result = await client.call_tool("memory_get", {"key": "abc"})

    assert result is expected
    assert sessions[0].call_tool_calls == [("memory_get", {"key": "abc"})]


async def test_call_tool_default_arguments_is_empty_dict() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        await client.call_tool("memory_clear")

    assert sessions[0].call_tool_calls == [("memory_clear", {})]


# ─────────────────────────────────────────────────────────────────────────
# Failure paths — translated to HermesUnavailable / HermesToolError
# ─────────────────────────────────────────────────────────────────────────


async def test_aenter_translates_missing_hermes_binary_to_unavailable() -> None:
    _, stdio = _stdio_factory(raise_on_enter=FileNotFoundError("hermes not in PATH"))
    _, session = _session_factory()
    with pytest.raises(HermesUnavailable, match="initialize"):
        async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session):
            pytest.fail("__aenter__ should have raised")


async def test_aenter_translates_initialize_mcp_error_to_unavailable() -> None:
    stdio_ctx, stdio = _stdio_factory()
    sessions, _session = _session_factory()

    class _PreloadSession(_FakeSession):
        def __init__(self, read: object, write: object) -> None:
            super().__init__(read, write)
            self.initialize_raises = McpError(ErrorData(code=-32603, message="initialize crashed"))

    def _bad_session_factory(read: object, write: object) -> _PreloadSession:
        s = _PreloadSession(read, write)
        sessions.append(s)
        return s

    with pytest.raises(HermesUnavailable, match="initialize"):
        async with HermesMcpClient(_stdio_factory=stdio, _session_factory=_bad_session_factory):
            pytest.fail("__aenter__ should have raised")
    # Resources got cleaned up despite the failure.
    assert stdio_ctx[0].aexit_count == 1


async def test_list_tools_translates_broken_pipe_to_unavailable() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].list_tools_response = anyio.BrokenResourceError("pipe broke mid-call")
        with pytest.raises(HermesUnavailable, match="list_tools"):
            await client.list_tools()


async def test_call_tool_translates_closed_resource_to_unavailable() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].call_tool_responses["bad"] = anyio.ClosedResourceError(
            "session closed mid-call"
        )
        with pytest.raises(HermesUnavailable, match="bad"):
            await client.call_tool("bad")


async def test_call_tool_translates_is_error_result_to_hermes_tool_error() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].call_tool_responses["memory_get"] = CallToolResult(
            content=[TextContent(type="text", text="cache key not found")],
            isError=True,
        )
        with pytest.raises(HermesToolError) as exc_info:
            await client.call_tool("memory_get", {"key": "abc"})

    assert exc_info.value.tool_name == "memory_get"
    assert "cache key not found" in str(exc_info.value)


async def test_tool_error_rendering_falls_back_to_structured_content() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].call_tool_responses["x"] = CallToolResult(
            content=[],
            isError=True,
            structuredContent={"code": "ENOENT", "detail": "no such cache key"},
        )
        with pytest.raises(HermesToolError) as exc_info:
            await client.call_tool("x")

    assert "ENOENT" in str(exc_info.value)


async def test_tool_error_with_empty_payload_renders_placeholder() -> None:
    _stdio_ctx, stdio = _stdio_factory()
    sessions, session = _session_factory()
    async with HermesMcpClient(_stdio_factory=stdio, _session_factory=session) as client:
        sessions[0].call_tool_responses["x"] = CallToolResult(content=[], isError=True)
        with pytest.raises(HermesToolError) as exc_info:
            await client.call_tool("x")

    assert "<no error detail>" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────
# Misuse: calling outside the async-with
# ─────────────────────────────────────────────────────────────────────────


async def test_call_tool_before_aenter_raises_unavailable() -> None:
    _, stdio = _stdio_factory()
    _, session = _session_factory()
    client = HermesMcpClient(_stdio_factory=stdio, _session_factory=session)
    with pytest.raises(HermesUnavailable, match="not entered"):
        await client.call_tool("memory_get")


async def test_list_tools_before_aenter_raises_unavailable() -> None:
    _, stdio = _stdio_factory()
    _, session = _session_factory()
    client = HermesMcpClient(_stdio_factory=stdio, _session_factory=session)
    with pytest.raises(HermesUnavailable, match="not entered"):
        await client.list_tools()


async def test_call_tool_after_aexit_raises_unavailable() -> None:
    _, stdio = _stdio_factory()
    _, session = _session_factory()
    client = HermesMcpClient(_stdio_factory=stdio, _session_factory=session)
    async with client:
        pass
    with pytest.raises(HermesUnavailable, match="not entered"):
        await client.call_tool("memory_get")


# ─────────────────────────────────────────────────────────────────────────
# Adapter discipline — mcp is allowed only inside this adapter
# ─────────────────────────────────────────────────────────────────────────


def test_only_hermes_mcp_adapter_imports_mcp() -> None:
    """Walks every src/ module outside ``adapters/`` and refuses any
    import of :mod:`mcp`. The deny-list lint covers this in CI; the
    test pins the invariant in the unit suite too so a regression
    breaks both gates."""
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "hardware_hunter"
    adapters_root = src_root / "adapters"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if adapters_root in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "mcp" or alias.name.startswith("mcp."):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "mcp" or module.startswith("mcp."):
                    offenders.append(f"{path}: from {module} import ...")
    assert not offenders, "mcp imports leaked outside adapters/:\n  " + "\n  ".join(offenders)
