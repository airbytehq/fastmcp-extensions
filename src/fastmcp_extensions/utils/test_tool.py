# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP tool testing utilities.

This module provides utilities for testing MCP tools directly with JSON arguments,
supporting both stdio and HTTP transports.

Usage (stdio transport):
    python -m fastmcp_extensions.utils.test_tool --app <module:app> <tool_name> '<json_args>'

    Example:
        python -m fastmcp_extensions.utils.test_tool --app my_mcp_server.server:app list_tools '{}'

    Poe task configuration:
        [tool.poe.tasks.mcp-tool-test]
        cmd = "python -m fastmcp_extensions.utils.test_tool --app my_mcp_server.server:app"
        help = "Test MCP tools with JSON arguments"

Usage (HTTP transport with --app):
    python -m fastmcp_extensions.utils.test_tool --http --app <module:app> [tool_name] ['<json_args>']

    Example:
        python -m fastmcp_extensions.utils.test_tool --http --app my_mcp_server.server:app
        python -m fastmcp_extensions.utils.test_tool --http --app my_mcp_server.server:app get_version '{}'

    Poe task configuration:
        [tool.poe.tasks.mcp-tool-test-http]
        cmd = "python -m fastmcp_extensions.utils.test_tool --http --app my_mcp_server.server:app"
        help = "Test MCP tools over HTTP transport"

Usage (HTTP transport with --cmd, for subprocess mode):
    python -m fastmcp_extensions.utils.test_tool --http --cmd '<server_command>' [tool_name] ['<json_args>']

    Example:
        python -m fastmcp_extensions.utils.test_tool --http --cmd 'uv run my-mcp-http'
        python -m fastmcp_extensions.utils.test_tool --http --cmd 'uv run my-mcp-http' get_version '{}'
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shlex
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from fastmcp_extensions.utils._http import find_free_port, wait_for_server

if TYPE_CHECKING:
    from fastmcp import FastMCP

SERVER_SHUTDOWN_TIMEOUT = 5.0


async def call_mcp_tool(app: FastMCP, tool_name: str, args: dict[str, Any]) -> object:
    """Call an MCP tool using the FastMCP client."""
    async with Client(app) as client:
        return await client.call_tool(tool_name, args)


async def list_mcp_tools(app: FastMCP) -> list[Any]:
    """List all available MCP tools."""
    async with Client(app) as client:
        return await client.list_tools()


def run_tool_test(
    app: FastMCP,
    tool_name: str,
    json_args: str,
) -> None:
    """Run a tool test with JSON arguments and print the result."""
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
    """Run a tool test over HTTP transport using a subprocess."""
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
    """Run a tool test over HTTP transport using the app directly."""
    import uvicorn

    if port is None:
        port = find_free_port()

    url = f"http://127.0.0.1:{port}/mcp"
    os.environ["MCP_HTTP_PORT"] = str(port)

    print(f"Starting HTTP server on port {port}...", file=sys.stderr)

    server_error: Exception | None = None

    def run_server() -> None:
        nonlocal server_error
        try:
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


def _import_app(app_path: str) -> object:
    """Import an app from a module:attribute path."""
    if ":" not in app_path:
        msg = f"Invalid app path '{app_path}'. Expected format: 'module.path:attribute'"
        raise ValueError(msg)

    module_path, attr_name = app_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)


def main() -> None:
    """Main entry point for the MCP tool testing CLI."""
    parser = argparse.ArgumentParser(
        description="Test MCP tools with JSON arguments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--app",
        help="App module path in format 'module.path:attribute' (for both stdio and HTTP transport)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP transport instead of stdio",
    )
    parser.add_argument(
        "--cmd",
        help="HTTP server command (alternative to --app for HTTP mode, spawns subprocess)",
    )
    parser.add_argument(
        "tool_name",
        nargs="?",
        help="Name of the tool to call (optional for HTTP smoke test)",
    )
    parser.add_argument(
        "json_args",
        nargs="?",
        default="{}",
        help="JSON string of arguments to pass to the tool",
    )

    cli_args = parser.parse_args()

    if cli_args.http:
        # HTTP transport mode
        if not cli_args.cmd and not cli_args.app:
            parser.error("--app or --cmd is required for HTTP transport mode")

        tool_args = json.loads(cli_args.json_args) if cli_args.tool_name else None

        if cli_args.cmd:
            # Use subprocess mode with explicit command
            http_command = shlex.split(cli_args.cmd)
            exit_code = asyncio.run(
                run_http_tool_test(
                    http_server_command=http_command,
                    tool_name=cli_args.tool_name,
                    args=tool_args,
                )
            )
            sys.exit(exit_code)
        else:
            # Use app mode - run HTTP server directly from app
            app = _import_app(cli_args.app)
            exit_code = asyncio.run(
                run_http_tool_test_with_app(
                    app=app,
                    tool_name=cli_args.tool_name,
                    args=tool_args,
                )
            )
            sys.exit(exit_code)
    else:
        # Stdio transport mode
        if not cli_args.app:
            parser.error("--app is required for stdio transport mode")
        if not cli_args.tool_name:
            parser.error("tool_name is required for stdio transport mode")

        app = _import_app(cli_args.app)
        run_tool_test(app, cli_args.tool_name, cli_args.json_args)


__all__ = [
    "call_mcp_tool",
    "find_free_port",
    "list_mcp_tools",
    "run_http_tool_test",
    "run_http_tool_test_with_app",
    "run_tool_test",
    "wait_for_server",
]

if __name__ == "__main__":
    main()
