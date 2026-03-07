"""stdio bridge that proxies MCP tool calls to a remote Streamable HTTP server.

This module exists for MCP hosts that only know how to launch local stdio
servers via `command` / `args`, but do not yet support remote MCP URLs.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import mcp.server.stdio
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

SERVER_NAME = "credit-card-recommendation-remote-bridge"
SERVER_VERSION = "0.1.0"
REMOTE_MCP_URL_ENV = "REMOTE_MCP_URL"

bridge_server = Server(SERVER_NAME)
T = TypeVar("T")


def get_remote_mcp_url() -> str:
    """Read the target remote MCP URL from the environment."""

    remote_url = os.getenv(REMOTE_MCP_URL_ENV, "").strip()
    if not remote_url:
        raise RuntimeError(
            f"{REMOTE_MCP_URL_ENV} is required, for example "
            "'https://credit-card-recom.onrender.com/mcp'."
        )
    return remote_url


async def with_remote_session(
    operation: Callable[[ClientSession], Awaitable[T]],
) -> T:
    """Open a short-lived remote MCP session and run an operation through it."""

    remote_url = get_remote_mcp_url()
    async with streamable_http_client(remote_url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await operation(session)


@bridge_server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Mirror the remote server's tool list."""

    async def operation(session: ClientSession) -> list[types.Tool]:
        result = await session.list_tools()
        return result.tools

    return await with_remote_session(operation)


@bridge_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Forward tool calls to the remote server and return the result unchanged."""

    async def operation(session: ClientSession) -> types.CallToolResult:
        return await session.call_tool(name, arguments)

    return await with_remote_session(operation)


def create_initialization_options() -> InitializationOptions:
    """Build MCP initialization metadata for the local bridge."""

    return InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=bridge_server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


async def run_stdio_bridge() -> None:
    """Run the bridge over stdio for local MCP hosts."""

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await bridge_server.run(
            read_stream,
            write_stream,
            create_initialization_options(),
        )


def main() -> None:
    """CLI entry point."""

    asyncio.run(run_stdio_bridge())


if __name__ == "__main__":
    main()
