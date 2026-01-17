#!/usr/bin/env python3
# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Measure the size of the MCP tool list output.

Usage:
    poe mcp-measure-tools

This is a template script. Copy and modify it for your MCP server by:
1. Importing your FastMCP app instance
2. Updating the call to run_measurement() with your app and server name

Example for a custom MCP server:
    from my_mcp_server.server import app
    from fastmcp_extensions.measurement import run_measurement

    if __name__ == "__main__":
        run_measurement(app, server_name="my-mcp-server")
"""

import sys


def main() -> None:
    """Main entry point for the MCP tool list measurement template."""
    print(
        "This is a template script. To use it:\n"
        "1. Copy this file to your MCP server project\n"
        "2. Import your FastMCP app instance\n"
        "3. Call run_measurement(app, server_name='your-server-name')\n"
        "\n"
        "Example:\n"
        "    from my_mcp_server.server import app\n"
        "    from fastmcp_extensions.measurement import run_measurement\n"
        "    run_measurement(app, server_name='my-mcp-server')",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
