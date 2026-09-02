"""
Persistent MCP client.

The MCP session is owned by one long-lived background task. Request handlers
never enter or exit the session's async context themselves - they just await
``call()``. That matters because the stdio transport and ``ClientSession`` open
nested async context managers and task groups: entering them in a FastAPI
startup hook and exiting them in a shutdown hook runs the exit in a different
task and raises. One owner task, opened and closed in the same place, avoids the
whole class of problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT_S = 45.0
CALL_TIMEOUT_S = 30.0


class MCPUnavailable(RuntimeError):
    """The knowledge server could not be reached."""


class MCPToolClient:
    """Owns one stdio MCP session and serialises tool calls onto it."""

    def __init__(self, server_module: str = "mcp_server.server") -> None:
        self.server_module = server_module
        self.session: ClientSession | None = None
        self.tools: list[str] = []
        self.error: str | None = None
        self.call_count = 0
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def params(self) -> StdioServerParameters:
        env = {**os.environ, "PYTHONPATH": str(BACKEND_ROOT), "PYTHONIOENCODING": "utf-8"}
        return StdioServerParameters(
            command=sys.executable, args=["-m", self.server_module],
            cwd=str(BACKEND_ROOT), env=env,
        )

    @property
    def connected(self) -> bool:
        return self.session is not None and self.error is None

    async def start(self) -> None:
        """Launch the server subprocess and wait until it has handshaked."""
        self._task = asyncio.create_task(self._run(), name="mcp-session")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=STARTUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.error = f"MCP server did not start within {STARTUP_TIMEOUT_S:.0f}s"
        if self.error:
            log.error("MCP server unavailable: %s", self.error)
        else:
            log.info("MCP server ready with tools: %s", ", ".join(self.tools))

    async def _run(self) -> None:
        """Own the session for its whole lifetime, in a single task."""
        try:
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = [t.name for t in listed.tools]
                    self.session = session
                    self._ready.set()
                    await self._shutdown.wait()
        except NotImplementedError:
            # Windows' SelectorEventLoop cannot spawn subprocesses, and raises a
            # bare NotImplementedError with no message. Uvicorn switches to that
            # loop whenever reload is enabled, so this is the failure a developer
            # is most likely to hit - it deserves an answer, not a blank error.
            self.error = self._loop_hint()
            self.session = None
            self._ready.set()
        except Exception as exc:  # noqa: BLE001 - surfaced via /health
            self.error = f"{type(exc).__name__}: {exc}"
            self.session = None
            self._ready.set()

    @staticmethod
    def _loop_hint() -> str:
        loop_name = type(asyncio.get_event_loop_policy()).__name__
        if sys.platform != "win32":
            return ("This event loop does not support subprocesses, so the MCP "
                    "server could not be launched.")
        return (
            f"Cannot start the MCP server: this asyncio event loop "
            f"({loop_name}) does not support subprocesses on Windows. Uvicorn "
            "switches to it whenever reload is enabled. Fix: start the backend "
            "with 'python run.py', which passes loop=\"none\" and keeps the "
            "Proactor loop. If you are calling uvicorn from the command line, "
            "drop --reload (the CLI does not accept --loop none; only "
            "uvicorn.run(loop=\"none\") does)."
        )

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=10)
        self.session = None

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and decode its JSON payload."""
        if not self.connected:
            raise MCPUnavailable(self.error or "MCP session is not connected")

        # The session multiplexes by request id, but serialising keeps ordering
        # in the pipeline trace deterministic, which matters for auditability.
        async with self._lock:
            self.call_count += 1
            try:
                result = await asyncio.wait_for(
                    self.session.call_tool(tool, arguments), timeout=CALL_TIMEOUT_S
                )
            except asyncio.TimeoutError as exc:
                raise MCPUnavailable(f"Tool '{tool}' timed out after {CALL_TIMEOUT_S}s") from exc

        if getattr(result, "isError", False):
            raise MCPUnavailable(f"Tool '{tool}' reported an error: {_text(result)}")

        payload = _text(result)
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCPUnavailable(
                f"Tool '{tool}' returned a non-JSON payload: {payload[:200]}") from exc

    def status(self) -> dict[str, Any]:
        return {
            "transport": "stdio",
            "server_module": self.server_module,
            "connected": self.connected,
            "tools": self.tools,
            "tool_calls_served": self.call_count,
            "detail": self.error or "ready",
        }


def _text(result: Any) -> str:
    """Concatenate the text blocks of a tool result."""
    parts = []
    for block in getattr(result, "content", []) or []:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif getattr(block, "text", None):
            parts.append(block.text)
    return "".join(parts)
