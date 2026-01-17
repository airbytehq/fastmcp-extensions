# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP server description and measurement utilities.

This module provides utilities for measuring the size of MCP tool lists,
which is useful for tracking context truncation issues when AI agents call list_tools.

Usage:
    Create a module in your MCP server project that can be called with -m syntax:

    ```python
    # my_mcp_server/describe.py
    from my_mcp_server.server import app
    from fastmcp_extensions.utils.describe_server import run_measurement

    if __name__ == "__main__":
        run_measurement(app, server_name="my-mcp-server")
    ```

    Then add a poe task:
    ```toml
    [tool.poe.tasks.mcp-describe-server]
    cmd = "python -m my_mcp_server.describe"
    help = "Describe MCP server tool list"
    ```

    Run with: `poe mcp-describe-server`

Output includes:
    - Tool count
    - Total characters (names + descriptions + schemas)
    - Average characters per tool
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Client

if TYPE_CHECKING:
    from fastmcp import FastMCP


@dataclass
class ToolListMeasurement:
    """Measurement results for an MCP tool list."""

    tool_count: int
    total_characters: int
    average_chars_per_tool: int
    server_name: str | None = None

    def __str__(self) -> str:
        """Return a human-readable string representation."""
        lines = []
        if self.server_name:
            lines.append(f"MCP Server: {self.server_name}")
        lines.append(f"Tool count: {self.tool_count}")
        lines.append(f"Total characters: {self.total_characters:,}")
        lines.append(f"Average chars per tool: {self.average_chars_per_tool:,}")
        return "\n".join(lines)


async def measure_tool_list(app: FastMCP) -> tuple[int, int]:
    """Measure the tool list size from the MCP server.

    This function connects to the MCP server and measures the character count
    of the tool list, including tool names, descriptions, and input schemas.

    Args:
        app: The FastMCP app instance

    Returns:
        Tuple of (tool_count, total_character_count)
    """
    async with Client(app) as client:
        tools = await client.list_tools()

        tool_count = len(tools)
        total_chars = 0

        for tool in tools:
            total_chars += len(tool.name)

            if tool.description:
                total_chars += len(tool.description)

            if tool.inputSchema:
                total_chars += len(str(tool.inputSchema))

        return tool_count, total_chars


async def measure_tool_list_detailed(
    app: FastMCP,
    server_name: str | None = None,
) -> ToolListMeasurement:
    """Measure the tool list size with detailed results.

    Args:
        app: The FastMCP app instance
        server_name: Optional name of the server for reporting

    Returns:
        ToolListMeasurement with detailed results
    """
    tool_count, total_chars = await measure_tool_list(app)

    return ToolListMeasurement(
        tool_count=tool_count,
        total_characters=total_chars,
        average_chars_per_tool=total_chars // tool_count if tool_count > 0 else 0,
        server_name=server_name,
    )


def run_measurement(app: FastMCP, server_name: str | None = None) -> None:
    """Run tool list measurement and print results.

    This is a convenience function for CLI measurement scripts.

    Args:
        app: The FastMCP app instance
        server_name: Optional name of the server for reporting
    """
    measurement = asyncio.run(measure_tool_list_detailed(app, server_name))
    print(str(measurement))


async def get_tool_details(app: FastMCP) -> list[dict[str, Any]]:
    """Get detailed information about each tool.

    Args:
        app: The FastMCP app instance

    Returns:
        List of dictionaries with tool details including name, description length,
        and schema length for each tool.
    """
    async with Client(app) as client:
        tools = await client.list_tools()

        details = []
        for tool in tools:
            name_len = len(tool.name)
            desc_len = len(tool.description) if tool.description else 0
            schema_len = len(str(tool.inputSchema)) if tool.inputSchema else 0

            details.append(
                {
                    "name": tool.name,
                    "name_length": name_len,
                    "description_length": desc_len,
                    "schema_length": schema_len,
                    "total_length": name_len + desc_len + schema_len,
                }
            )

        return details
