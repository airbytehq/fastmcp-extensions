# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP tool testing utilities.

This module provides utilities for testing MCP tools directly with JSON arguments,
supporting both stdio and HTTP transports.

Usage (stdio transport):
    python -m fastmcp_extensions.utils.testing --app <module:app> <tool_name> '<json_args>'

    Example:
        python -m fastmcp_extensions.utils.testing --app my_mcp_server.server:app list_tools '{}'

    Poe task configuration:
        [tool.poe.tasks.mcp-tool-test]
        cmd = "python -m fastmcp_extensions.utils.testing --app my_mcp_server.server:app"
        help = "Test MCP tools with JSON arguments"

Usage (HTTP transport with --app):
    python -m fastmcp_extensions.utils.testing --http --app <module:app> [tool_name] ['<json_args>']

    Example:
        python -m fastmcp_extensions.utils.testing --http --app my_mcp_server.server:app
        python -m fastmcp_extensions.utils.testing --http --app my_mcp_server.server:app get_version '{}'

    Poe task configuration:
        [tool.poe.tasks.mcp-tool-test-http]
        cmd = "python -m fastmcp_extensions.utils.testing --http --app my_mcp_server.server:app"
        help = "Test MCP tools over HTTP transport"

Usage (HTTP transport with --cmd, for subprocess mode):
    python -m fastmcp_extensions.utils.testing --http --cmd '<server_command>' [tool_name] ['<json_args>']

    Example:
        python -m fastmcp_extensions.utils.testing --http --cmd 'uv run my-mcp-http'
        python -m fastmcp_extensions.utils.testing --http --cmd 'uv run my-mcp-http' get_version '{}'
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
    """Run a tool test over HTTP transport using a subprocess.

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


async def run_http_tool_test_with_app(
    app: FastMCP,
    port: int | None = None,
    tool_name: str | None = None,
    args: dict[str, Any] | None = None,
) -> int:
    """Run a tool test over HTTP transport using the app directly.

    This function starts the HTTP server from the app instance,
    verifies it's working, optionally calls a specific tool, then shuts down.

    Args:
        app: The FastMCP app instance
        port: Port to use (if None, finds a free port)
        tool_name: Optional tool name to call
        args: Optional arguments for the tool

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    import os
    import threading

    if port is None:
        port = find_free_port()

    url = f"http://127.0.0.1:{port}/mcp"
    os.environ["MCP_HTTP_PORT"] = str(port)

    print(f"Starting HTTP server on port {port}...", file=sys.stderr)

    server_error: Exception | None = None

    def run_server() -> None:
        nonlocal server_error
        try:
            import uvicorn

            uvicorn.run(
                app.http_app(),
                host="127.0.0.1",
                port=port,
                log_level="error",
            )
        except Exception as e:
            server_error = e

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    try:
        if not await wait_for_server(url):
            if server_error:
                print(f"Server error: {server_error}", file=sys.stderr)
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
        pass


__all__ = [
    "call_mcp_tool",
    "find_free_port",
    "list_mcp_tools",
    "run_http_tool_test",
    "run_http_tool_test_with_app",
    "run_tool_test",
    "wait_for_server",
]
