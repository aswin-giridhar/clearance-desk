"""Runtime access to ClickHouse through the OFFICIAL mcp-clickhouse server.

The ClickHouse track requires the partner service to be reached via the official
MCP server, not a direct database driver. This module owns one long-lived stdio
session to `mcp-clickhouse` and executes every clearance query through its
`run_query` tool.

Two practical notes:
  * Cold start is ~16s (the server does a PyPI version check on boot), so the
    session is opened once at application startup and reused, never per request.
  * The server does not expose ClickHouse session settings, so the read-overflow
    guard is appended to each statement as a SETTINGS clause instead. Without it
    the demo cluster silently truncates and returns partial answers.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# The demo user is readonly, which rejects most SETTINGS changes (error 164).
# read_overflow_mode is accepted; max_execution_time is not. Verified empirically.
SETTINGS_CLAUSE = "SETTINGS read_overflow_mode='throw'"

SERVER_ENV = {
    "CLICKHOUSE_HOST": os.getenv("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com"),
    "CLICKHOUSE_PORT": os.getenv("CLICKHOUSE_PORT", "8443"),
    "CLICKHOUSE_USER": os.getenv("CLICKHOUSE_USER", "demo"),
    "CLICKHOUSE_PASSWORD": os.getenv("CLICKHOUSE_PASSWORD", ""),
    "CLICKHOUSE_SECURE": "true",
}


class MCPClickHouse:
    """A persistent stdio session to mcp-clickhouse."""

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._ctx: Any = None
        self._session_ctx: Any = None
        self.tools: list[str] = []
        self.ready = False

    async def start(self) -> None:
        params = StdioServerParameters(
            command="python3",
            args=["-m", "mcp_clickhouse.main"],
            env={**os.environ, **SERVER_ENV},
        )
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        self.tools = [t.name for t in (await self._session.list_tools()).tools]
        self.ready = True

    async def stop(self) -> None:
        try:
            if self._session_ctx:
                await self._session_ctx.__aexit__(None, None, None)
            if self._ctx:
                await self._ctx.__aexit__(None, None, None)
        finally:
            self.ready = False

    async def run_query(self, sql: str) -> list[dict]:
        """Execute SQL via the MCP server's run_query tool."""
        if not self.ready or self._session is None:
            raise RuntimeError("MCP session not started")
        statement = f"{sql.rstrip().rstrip(';')} {SETTINGS_CLAUSE}"
        result = await self._session.call_tool("run_query", {"query": statement})
        parts = [c.text for c in result.content if getattr(c, "text", None)]
        raw = "\n".join(parts)
        # The server returns an error string rather than raising, so inspect it.
        if "max_rows_to_read" in raw or "code: 158" in raw:
            raise RuntimeError(
                "TRUNCATED: this query would exceed the cluster read limit, so any "
                "answer would be partial. Narrow it using the table's sort key."
            )
        # mcp-clickhouse returns errors as text rather than raising, so any other
        # failure must be surfaced too -- an error string parsed as data would be
        # far worse than a visible exception.
        if "Query execution failed" in raw or "DB::Exception" in raw:
            raise RuntimeError(f"ClickHouse error via MCP: {raw[:200]}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [{"raw": raw}]


_singleton: MCPClickHouse | None = None


async def get_mcp() -> MCPClickHouse:
    global _singleton
    if _singleton is None or not _singleton.ready:
        _singleton = MCPClickHouse()
        await _singleton.start()
    return _singleton
