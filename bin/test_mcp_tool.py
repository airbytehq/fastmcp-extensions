#!/usr/bin/env python3
# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""One-liner CLI tool for testing MCP tools directly with JSON arguments.

Usage:
    poe mcp-tool-test <tool_name> '<json_args>'

This is a template script. Copy and modify it for your MCP server by:
1. Importing your FastMCP app instance
2. Updating the call to run_tool_test() with your app

Example for a custom MCP server:
    from my_mcp_server.server import app
    from fastmcp_extensions.testing import run_tool_test

    if __name__ == "__main__":
        if len(sys.argv) < 3:
            print("Usage: python test_mcp_tool.py <tool_name> '<json_args>'")
            sys.exit(1)
        run_tool_test(app, sys.argv[1], sys.argv[2])
"""

import sys


def main() -> None:
    """Main entry point for the MCP tool tester template."""
    print(
        "This is a template script. To use it:\n"
        "1. Copy this file to your MCP server project\n"
        "2. Import your FastMCP app instance\n"
        "3. Call run_tool_test(app, tool_name, json_args)\n"
        "\n"
        "Example:\n"
        "    from my_mcp_server.server import app\n"
        "    from fastmcp_extensions.testing import run_tool_test\n"
        "    run_tool_test(app, sys.argv[1], sys.argv[2])",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
