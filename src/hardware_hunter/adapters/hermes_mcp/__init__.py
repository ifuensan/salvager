"""Hermes MCP transport adapter — prerequisite for Stories 3.5 / 3.10 / 3.14.

The :class:`HermesMcpClient` is the only place in the project allowed
to import :mod:`mcp` (the Model Context Protocol Python SDK). It owns
the stdio subprocess that runs ``hermes mcp serve`` and surfaces a
typed :meth:`HermesMcpClient.call_tool` / :meth:`HermesMcpClient.list_tools`
API for the domain-specific consumers that land in:

  - ``adapters/wallapop_tinyfish/`` (Story 3.5) — TinyFish search/fetch
    via tools Hermes registers as MCP servers internally
  - ``adapters/llm_cache_hermes/`` (Story 3.10) — per-listing-URL
    eval cache backed by Hermes' SQLite memory tools
  - ``orchestration/poll_loop.py`` (Story 3.14) — Hermes subagent
    fan-out for concurrent LLM evaluation

Each consumer is its own adapter and depends on this one for transport.
"""

from hardware_hunter.adapters.hermes_mcp.client import HermesMcpClient

__all__ = ["HermesMcpClient"]
