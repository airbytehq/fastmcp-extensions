#!/usr/bin/env python3
# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""One-step stateless test for MCP HTTP transport.

Usage:
    poe mcp-tool-test-http                           # Smoke test only
    poe mcp-tool-test-http <tool_name> '<json_args>' # Test specific tool

This is a template script. Copy and modify it for your MCP server by:
1. Updating the HTTP_SERVER_COMMAND to match your server's entry point
2. Optionally customizing environment variables

Example for a custom MCP server:
    HTTP_SERVER_COMMAND = ["uv", "run", "my-mcp-server-http"]
"""

import sys


def main() -> None:
    """Main entry point for the HTTP tool tester template."""
    print(
        "This is a template script. To use it:\n"
        "1. Copy this file to your MCP server project\n"
        "2. Update HTTP_SERVER_COMMAND to match your server\n"
        "3. Import and use run_http_tool_test from fastmcp_extensions.testing\n"
        "\n"
        "Example:\n"
        "    import asyncio\n"
        "    from fastmcp_extensions.testing import run_http_tool_test\n"
        "    HTTP_SERVER_COMMAND = ['uv', 'run', 'my-mcp-server-http']\n"
        "    sys.exit(asyncio.run(run_http_tool_test(HTTP_SERVER_COMMAND)))",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
