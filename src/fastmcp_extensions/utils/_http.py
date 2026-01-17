# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Generic HTTP utilities for MCP server testing."""

from __future__ import annotations

import asyncio
import socket

from fastmcp import Client

SERVER_STARTUP_TIMEOUT = 10.0
POLL_INTERVAL = 0.2


def find_free_port() -> int:
    """Find an available port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_server(url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> bool:
    """Wait for the MCP server to be ready by attempting to list tools."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with Client(url) as client:
                await client.list_tools()
                return True
        except Exception:
            await asyncio.sleep(POLL_INTERVAL)
    return False
