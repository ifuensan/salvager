""":class:`HermesMcpClient` — the stdio MCP transport for Hermes.

Wraps the canonical async ``stdio_client → ClientSession`` pattern
from the :mod:`mcp` SDK into a single async context manager so the
rest of the daemon doesn't see two nested ``async with`` blocks at
every call site.

Lifecycle
---------
::

    async with HermesMcpClient() as client:
        tools = await client.list_tools()
        result = await client.call_tool("memory_get", {"key": "abc"})

On ``__aenter__`` the adapter:

  1. Builds :class:`StdioServerParameters` (default: ``hermes mcp serve``).
  2. Spawns the subprocess via :func:`stdio_client`.
  3. Opens a :class:`ClientSession` over its stdin/stdout pipes.
  4. Sends the MCP ``initialize`` handshake.

Any failure in steps 1-4 closes the partially-opened resources and
raises :class:`HermesUnavailable`. Callers degrade gracefully — the
Wallapop two-path orchestrator drops to the API path, the LLM cache
misses straight through to direct evaluation.

Error translation
-----------------
The MCP spec returns server-side tool failures in-band (a
``CallToolResult`` with ``isError=True``) so an orchestrating LLM can
recover. Our consumers are deterministic Python — we translate the
in-band error into a raised :class:`HermesToolError`. Transport-level
problems (broken pipe, subprocess died, JSON-RPC error) raise
:class:`HermesUnavailable`.

Test seam
---------
The two factories (``_stdio_factory`` and ``_session_factory``) are
constructor parameters so unit tests can drop in async-context-manager
fakes that record calls without spawning anything. The default
factories use the real :mod:`mcp` SDK; production code never overrides.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, Tool

from hardware_hunter.domain.errors import HermesToolError, HermesUnavailable
from hardware_hunter.observability.logging import get_logger

#: Defaults to invoke ``hermes mcp serve`` on the co-located VM (the
#: project's deployment topology — see the
#: ``project_hermes_deployment`` memory note). Tests inject fakes.
_DEFAULT_COMMAND = "hermes"
_DEFAULT_ARGS: tuple[str, ...] = ("mcp", "serve")


class HermesMcpClient:
    """Single-process MCP client owning a ``hermes mcp serve`` subprocess.

    Reusable across multiple ``async with`` cycles; each entry spawns
    a fresh subprocess. The instance itself holds no I/O resources
    outside the context manager — exit cleans everything up.
    """

    def __init__(
        self,
        *,
        command: str = _DEFAULT_COMMAND,
        args: tuple[str, ...] = _DEFAULT_ARGS,
        env: Mapping[str, str] | None = None,
        _stdio_factory: Any = stdio_client,
        _session_factory: Any = ClientSession,
    ) -> None:
        self._command = command
        self._args = list(args)
        self._env = dict(env) if env is not None else None
        self._stdio_factory = _stdio_factory
        self._session_factory = _session_factory
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._log = get_logger("adapter.hermes_mcp")

    # ─────────────────────────────────────────────────────────────────
    # Async context manager
    # ─────────────────────────────────────────────────────────────────

    async def __aenter__(self) -> HermesMcpClient:
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self._command,
                args=self._args,
                env=self._env,
            )
            read, write = await stack.enter_async_context(self._stdio_factory(params))
            session = await stack.enter_async_context(self._session_factory(read, write))
            await session.initialize()
        except (FileNotFoundError, OSError, McpError) as exc:
            await stack.aclose()
            self._log.error(
                "hermes_unavailable",
                extra={
                    "phase": "initialize",
                    "error_class": exc.__class__.__name__,
                },
            )
            raise HermesUnavailable(
                f"Hermes MCP transport failed during initialize: {exc}"
            ) from exc
        except BaseException:
            await stack.aclose()
            raise

        self._stack = stack
        self._session = session
        self._log.info(
            "hermes_session_opened",
            extra={"command": self._command, "cli_args": self._args},
        )
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            await stack.aclose()
            self._log.info("hermes_session_closed", extra={})

    # ─────────────────────────────────────────────────────────────────
    # MCP — list_tools / call_tool
    # ─────────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[Tool]:
        """Return every MCP tool ``hermes mcp serve`` exposes.

        The set varies based on Hermes' own config (which MCP servers
        it has wired, which tools it has enabled per-platform). The
        domain-specific consumers (cache, TinyFish, subagent) probe
        for the tools they need and raise :class:`HermesUnavailable`
        when a required tool isn't present.
        """
        session = self._require_session()
        try:
            result = await session.list_tools()
        except (McpError, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            self._log.error(
                "hermes_unavailable",
                extra={"phase": "list_tools", "error_class": exc.__class__.__name__},
            )
            raise HermesUnavailable(f"list_tools failed: {exc}") from exc
        return list(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> CallToolResult:
        """Invoke an MCP tool by name; raise on transport or tool error.

        The returned :class:`CallToolResult` carries either text content
        (``result.content[i].text``) or structured content
        (``result.structured_content``) depending on the tool's
        declared schema. The consumer extracts whichever it expects.
        """
        session = self._require_session()
        args_dict = dict(arguments) if arguments is not None else {}
        try:
            result = await session.call_tool(name, arguments=args_dict)
        except (McpError, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            self._log.error(
                "hermes_unavailable",
                extra={
                    "phase": "call_tool",
                    "tool_name": name,
                    "error_class": exc.__class__.__name__,
                },
            )
            raise HermesUnavailable(f"call_tool({name!r}) failed: {exc}") from exc

        if result.isError:
            error_detail = _render_error_message(result)
            self._log.warning(
                "hermes_tool_error",
                extra={"tool_name": name, "error_detail": error_detail},
            )
            raise HermesToolError(name, error_detail)
        return result

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise HermesUnavailable(
                "HermesMcpClient is not entered. Use `async with HermesMcpClient() as client:`"
            )
        return self._session


def _render_error_message(result: CallToolResult) -> str:
    """Extract a human-readable error message from a CallToolResult.

    MCP tool errors land in ``result.content[i]`` as TextContent
    blocks. We join the text fields to produce a single string for
    the log line + exception message; structured fields, if present,
    are stringified as a fallback.
    """
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    if parts:
        return " | ".join(parts)
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return str(structured)
    return "<no error detail>"


__all__ = ["HermesMcpClient"]
