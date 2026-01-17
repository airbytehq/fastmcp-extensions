# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP tool testing utilities.

This module provides utilities for testing MCP tools directly with JSON arguments,
supporting both stdio and HTTP transports.

Usage (stdio transport):
    Create a module in your MCP server project that can be called with -m syntax:

    ```python
    # my_mcp_server/tool_test.py
    import sys
    from my_mcp_server.server import app
    from fastmcp_extensions.utils.testing import run_tool_test

    if __name__ == "__main__":
        if len(sys.argv) < 3:
            print("Usage: python -m my_mcp_server.tool_test <tool_name> '<json_args>'")
            sys.exit(1)
        run_tool_test(app, sys.argv[1], sys.argv[2])
    ```

    Then add a poe task:
    ```toml
    [tool.poe.tasks.mcp-tool-test]
    cmd = "python -m my_mcp_server.tool_test"
    help = "Test MCP tools with JSON arguments"
    ```

    Run with: `poe mcp-tool-test <tool_name> '<json_args>'`

Usage (HTTP transport):
    Create a module for HTTP testing:

    ```python
    # my_mcp_server/tool_test_http.py
    import asyncio
    import sys
    from fastmcp_extensions.utils.testing import run_http_tool_test

    HTTP_SERVER_COMMAND = ["uv", "run", "my-mcp-server-http"]

    if __name__ == "__main__":
        tool_name = sys.argv[1] if len(sys.argv) > 1 else None
        json_args = sys.argv[2] if len(sys.argv) > 2 else "{}"
        args = json.loads(json_args) if tool_name else None
        sys.exit(
            asyncio.run(
                run_http_tool_test(
                    HTTP_SERVER_COMMAND,
                    tool_name=tool_name,
                    args=args,
                )
            )
        )
    ```

    Then add a poe task:
    ```toml
    [tool.poe.tasks.mcp-tool-test-http]
    cmd = "python -m my_mcp_server.tool_test_http"
    help = "Test MCP tools over HTTP transport"
    ```

    Run with:
    - Smoke test: `poe mcp-tool-test-http`
    - Test specific tool: `poe mcp-tool-test-http <tool_name> '<json_args>'`
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from fastmcp import Client

if TYPE_CHECKING:
    from fastmcp import FastMCP

SERVER_STARTUP_TIMEOUT = 10.0
SERVER_SHUTDOWN_TIMEOUT = 5.0
POLL_INTERVAL = 0.2


async def call_mcp_tool(app: FastMCP, tool_name: str, args: dict[str, Any]) -> object:
    """Call an MCP tool using the FastMCP client.

    Args:
        app: The FastMCP app instance
        tool_name: Name of the tool to call
        args: Arguments to pass to the tool

    Returns:
        The result from the tool call
    """
    async with Client(app) as client:
        return await client.call_tool(tool_name, args)


async def list_mcp_tools(app: FastMCP) -> list[Any]:
    """List all available MCP tools.

    Args:
        app: The FastMCP app instance

    Returns:
        List of available tools
    """
    async with Client(app) as client:
        return await client.list_tools()


def find_free_port() -> int:
    """Find an available port on localhost.

    Returns:
        An available port number
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_server(url: str, timeout: float = SERVER_STARTUP_TIMEOUT) -> bool:
    """Wait for the MCP server to be ready by attempting to list tools.

    Args:
        url: The URL of the MCP server
        timeout: Maximum time to wait in seconds

    Returns:
        True if server is ready, False if timeout
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with Client(url) as client:
                await client.list_tools()
                return True
        except Exception:
            await asyncio.sleep(POLL_INTERVAL)
    return False


def run_tool_test(
    app: FastMCP,
    tool_name: str,
    json_args: str,
) -> None:
    """Run a tool test with JSON arguments and print the result.

    This is a convenience function for CLI tool testing scripts.

    Args:
        app: The FastMCP app instance
        tool_name: Name of the tool to call
        json_args: JSON string of arguments to pass to the tool
    """
    args: dict[str, Any] = json.loads(json_args)
    result = asyncio.run(call_mcp_tool(app, tool_name, args))

    if hasattr(result, "text"):
        print(result.text)
    else:
        print(str(result))


async def run_http_tool_test(
    http_server_command: list[str],
    port: int | None = None,
    tool_name: str | None = None,
    args: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run a tool test over HTTP transport.

    This function spawns an HTTP server, verifies it's working,
    optionally calls a specific tool, then shuts down the server.

    Args:
        http_server_command: Command to start the HTTP server (e.g., ["uv", "run", "my-mcp-http"])
        port: Port to use (if None, finds a free port)
        tool_name: Optional tool name to call
        args: Optional arguments for the tool
        env: Optional environment variables for the server process

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    import os

    if port is None:
        port = find_free_port()

    url = f"http://127.0.0.1:{port}/mcp"

    server_env = os.environ.copy()
    if env:
        server_env.update(env)
    server_env["MCP_HTTP_PORT"] = str(port)

    print(f"Starting HTTP server on port {port}...", file=sys.stderr)

    proc = subprocess.Popen(
        http_server_command,
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not await wait_for_server(url):
            print(f"Server failed to start on port {port}", file=sys.stderr)
            return 1

        async with Client(url) as client:
            tools = await client.list_tools()
            print(f"HTTP transport OK - {len(tools)} tools available")

            if tool_name:
                print(f"Calling tool: {tool_name}", file=sys.stderr)
                result = await client.call_tool(tool_name, args or {})

                if hasattr(result, "text"):
                    print(result.text)
                else:
                    print(str(result))

        return 0

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=SERVER_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
