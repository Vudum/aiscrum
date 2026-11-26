"""
mcp_client.py - the MCP CLIENT that lives inside the app server.

It does not decide anything. It (1) asks the board server which tools exist
(ListTools) and (2) carries tool requests from the agents to the server
(CallTool), then brings the result back.

Transport:
  MCP_TRANSPORT=stdio  (default) start board_server.py as a separate process
  MCP_TRANSPORT=memory run the server inside the app (for hosts that block
                       subprocesses, e.g. some shared hosting)
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE_DIR = Path(__file__).resolve().parent
TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()


class BoardClient:
    def __init__(self, session: ClientSession):
        self.session = session
        self.calls: list[dict] = []  # every tool call, for the "behind the scenes" log

    async def list_tools(self) -> list[dict]:
        result = await self.session.list_tools()           # -> ListToolsRequest
        return [{"name": t.name, "description": t.description or "",
                 "input_schema": t.inputSchema} for t in result.tools]

    async def call(self, agent: str, name: str, args: dict) -> str:
        result = await self.session.call_tool(name, args)  # -> CallToolRequest
        text = "".join(getattr(c, "text", "") for c in result.content) or "OK"
        if result.isError and not text.startswith("ERROR"):
            text = "ERROR: " + text
        self.calls.append({"agent": agent, "tool": name, "args": args, "result": text})
        return text


@asynccontextmanager
async def board_session(project_file: str):
    if TRANSPORT == "memory":
        import board_server
        from mcp.shared.memory import create_connected_server_and_client_session

        board_server.PROJECT_FILE_OVERRIDE = str(project_file)
        async with create_connected_server_and_client_session(
                board_server.mcp._mcp_server) as session:
            yield BoardClient(session)
        return

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(BASE_DIR / "board_server.py")],
        env={**os.environ, "SCRUMGYM_PROJECT_FILE": str(project_file),
             "PYTHONPATH": str(BASE_DIR)},
        cwd=str(BASE_DIR),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield BoardClient(session)


def run_actions(project_file: str, actions: list) -> list[dict]:
    """Run a list of (agent, tool, args) through MCP. Used for rule-based work."""
    if not actions:
        return []

    async def go():
        async with board_session(project_file) as client:
            for agent, tool, args in actions:
                await client.call(agent, tool, args)
            return client.calls

    return asyncio.run(go())
