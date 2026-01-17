# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""CLI entry point for MCP tool testing.

Usage (stdio transport):
    python -m fastmcp_extensions.utils.testing --app <module:app> <tool_name> '<json_args>'

Usage (HTTP transport with --app):
    python -m fastmcp_extensions.utils.testing --http --app <module:app> [tool_name] ['<json_args>']

Usage (HTTP transport with --cmd):
    python -m fastmcp_extensions.utils.testing --http --cmd '<server_command>' [tool_name] ['<json_args>']

Examples:
    # Stdio transport
    python -m fastmcp_extensions.utils.testing --app my_server.server:app list_tools '{}'

    # HTTP transport with --app (runs app.run_http() directly)
    python -m fastmcp_extensions.utils.testing --http --app my_server.server:app
    python -m fastmcp_extensions.utils.testing --http --app my_server.server:app get_version '{}'

    # HTTP transport with --cmd (spawns server subprocess)
    python -m fastmcp_extensions.utils.testing --http --cmd 'uv run my-mcp-http'
    python -m fastmcp_extensions.utils.testing --http --cmd 'uv run my-mcp-http' get_version '{}'
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import shlex
import sys

from fastmcp_extensions.utils.testing import run_http_tool_test, run_tool_test


def _import_app(app_path: str) -> object:
    """Import an app from a module:attribute path.

    Args:
        app_path: Path in format 'module.path:attribute' (e.g., 'my_server.server:app')

    Returns:
        The imported app object
    """
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

    args = parser.parse_args()

    if args.http:
        # HTTP transport mode
        if not args.cmd and not args.app:
            parser.error("--app or --cmd is required for HTTP transport mode")

        tool_args = json.loads(args.json_args) if args.tool_name else None

        if args.cmd:
            # Use subprocess mode with explicit command
            http_command = shlex.split(args.cmd)
            exit_code = asyncio.run(
                run_http_tool_test(
                    http_server_command=http_command,
                    tool_name=args.tool_name,
                    args=tool_args,
                )
            )
            sys.exit(exit_code)
        else:
            # Use app mode - run HTTP server directly from app
            from fastmcp_extensions.utils.testing import run_http_tool_test_with_app

            app = _import_app(args.app)
            exit_code = asyncio.run(
                run_http_tool_test_with_app(
                    app=app,
                    tool_name=args.tool_name,
                    args=tool_args,
                )
            )
            sys.exit(exit_code)
    else:
        # Stdio transport mode
        if not args.app:
            parser.error("--app is required for stdio transport mode")
        if not args.tool_name:
            parser.error("tool_name is required for stdio transport mode")

        app = _import_app(args.app)
        run_tool_test(app, args.tool_name, args.json_args)


if __name__ == "__main__":
    main()
