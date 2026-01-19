# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Internal module for tool filtering implementation.

This is a private module that provides the internal implementation for
per-request tool filtering. Users should not import from this module directly.

For tool filtering, use the `mcp_server()` function with `tool_filters` or
`include_standard_tool_filters` parameters instead.

See Also:
    - FastMCP middleware documentation: https://gofastmcp.com/servers/middleware
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp import types as mt
from mcp.types import Tool

from fastmcp_extensions.tool_filters import ToolFilterFn


class ToolFilterMiddleware(Middleware):
    """Middleware that filters tools on a per-request basis.

    This middleware intercepts tool listing and tool calls to filter
    which tools are visible and callable based on a user-provided
    filter function. The filter function has access to the FastMCP
    app, allowing it to use get_mcp_config() to access request-specific
    configuration values.

    Args:
        app: The FastMCP application instance.
        tool_filter: A callable that takes (Tool, FastMCP) and returns
            True if the tool should be visible, False to hide it.

    Example:
        ```python
        def readonly_filter(tool: Tool, app: FastMCP) -> bool:
            if get_mcp_config(app, "readonly_mode") == "1":
                annotations = tool.annotations
                if annotations is None:
                    return False
                return getattr(annotations, "readOnlyHint", False)
            return True


        middleware = ToolFilterMiddleware(app, tool_filter=readonly_filter)
        app.add_middleware(middleware)
        ```
    """

    def __init__(
        self,
        app: FastMCP,
        *,
        tool_filter: ToolFilterFn,
    ) -> None:
        """Initialize the middleware.

        Args:
            app: The FastMCP application instance.
            tool_filter: A callable that determines tool visibility.
        """
        self._app = app
        self._tool_filter = tool_filter

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: Callable[[MiddlewareContext[mt.ListToolsRequest]], Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Filter the tool list based on the filter function.

        Args:
            context: The middleware context.
            call_next: The next handler in the chain.

        Returns:
            Filtered sequence of tools.
        """
        tools = await call_next(context)
        return [tool for tool in tools if self._tool_filter(tool, self._app)]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: Callable[[MiddlewareContext[mt.CallToolRequestParams]], ToolResult],
    ) -> ToolResult:
        """Deny calls to filtered tools.

        Args:
            context: The middleware context.
            call_next: The next handler in the chain.

        Returns:
            The tool result if allowed.

        Raises:
            ValueError: If the tool is filtered out.
        """
        tool_name = context.message.name

        # Look up the tool to check if it should be filtered
        tool = self._get_tool_by_name(tool_name)
        if tool is not None and not self._tool_filter(tool, self._app):
            raise ValueError(
                f"Tool '{tool_name}' is not available. "
                "It may be restricted based on your current session configuration."
            )

        return await call_next(context)

    def _get_tool_by_name(self, name: str) -> Tool | None:
        """Look up a tool by name from the app's tool manager.

        Args:
            name: The tool name to look up.

        Returns:
            The Tool object if found, None otherwise.
        """
        # Access FastMCP's internal tool manager to get tool info
        tool_manager = getattr(self._app, "_tool_manager", None)
        if tool_manager is None:
            return None

        # Access the private _tools dict (the public methods are async)
        tools = getattr(tool_manager, "_tools", {})
        fast_tool = tools.get(name)
        if fast_tool is None:
            return None

        # Convert FastTool to MCP Tool type
        return fast_tool.to_mcp_tool()
