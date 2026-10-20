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

from collections.abc import Sequence
from copy import deepcopy

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool as FastMCPTool
from fastmcp.tools import ToolResult
from mcp import types as mt
from mcp.types import Tool

from fastmcp_extensions.tool_filters import (
    ToolFilterFn,
    ToolUnavailableError,
    available_capabilities,
)
from fastmcp_extensions.tool_traits import Capability, get_tool_traits


def _to_mcp_tool(tool: FastMCPTool | Tool) -> Tool:
    """Normalize FastMCP tools to the MCP type expected by `ToolFilterFn`."""
    if isinstance(tool, FastMCPTool):
        return tool.to_mcp_tool()
    return tool


def _validate_capability_gated_args(
    tool: FastMCPTool,
    argument_names: Sequence[str],
) -> None:
    """Validate that capability-gated arguments are optional schema properties."""
    properties = tool.parameters.get("properties")
    required = tool.parameters.get("required") or []
    for name in argument_names:
        if (
            not isinstance(properties, dict)
            or name not in properties
            or name in required
        ):
            raise ValueError(
                "CapabilityShapingMiddleware: cannot capability-gate parameter "
                f"'{name}' of tool {tool.name!r}: it must exist in the tool schema "
                "and declare a default value."
            )


class CapabilityShapingMiddleware(Middleware):
    """Shape tool schemas and calls for the current client's capabilities."""

    def __init__(self, app: FastMCP) -> None:
        """Initialize capability shaping.

        Args:
            app: FastMCP application whose tool traits and capability resolvers
                are used for shaping.
        """
        self._app = app

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[FastMCPTool]],
    ) -> Sequence[FastMCPTool]:
        """Hide unsupported arguments and optional UI metadata from the schema."""
        tools = await call_next(context)
        traits_by_name = {
            tool.name: get_tool_traits(self._app, tool.name) for tool in tools
        }
        for tool in tools:
            traits = traits_by_name[tool.name]
            if traits.capability_gated_args:
                _validate_capability_gated_args(
                    tool,
                    tuple(traits.capability_gated_args),
                )
        needs_capabilities = any(
            traits.ui_optional
            or any(
                required_capabilities
                for required_capabilities in traits.capability_gated_args.values()
            )
            for traits in traits_by_name.values()
        )
        available = available_capabilities(self._app) if needs_capabilities else set()

        shaped_tools: list[FastMCPTool] = []
        for tool in tools:
            traits = traits_by_name[tool.name]
            if not traits.ui_optional and not traits.capability_gated_args:
                shaped_tools.append(tool)
                continue

            parameters = deepcopy(tool.parameters)
            properties = parameters.get("properties")
            required = parameters.get("required")
            for name, required_capabilities in traits.capability_gated_args.items():
                if required_capabilities <= available:
                    continue
                if isinstance(properties, dict):
                    properties.pop(name, None)
                if isinstance(required, list):
                    parameters["required"] = [item for item in required if item != name]
                    required = parameters["required"]

            meta = tool.meta
            if traits.ui_optional and Capability.UI not in available:
                meta = deepcopy(meta or {})
                meta.pop("ui", None)
                meta.pop("ui/resourceUri", None)
                if not meta:
                    meta = None

            shaped_tools.append(
                tool.model_copy(update={"parameters": parameters, "meta": meta})
            )
        return shaped_tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Ignore unavailable gated arguments so their declared defaults apply."""
        traits = get_tool_traits(self._app, context.message.name)
        if not traits.capability_gated_args:
            return await call_next(context)

        tool = await self._app.get_tool(context.message.name)
        if tool is not None:
            _validate_capability_gated_args(
                tool,
                tuple(traits.capability_gated_args),
            )
        if not any(traits.capability_gated_args.values()):
            return await call_next(context)

        available = available_capabilities(self._app)
        arguments = dict(context.message.arguments or {})
        original_argument_count = len(arguments)
        for name, required_capabilities in traits.capability_gated_args.items():
            if not required_capabilities <= available:
                arguments.pop(name, None)
        if len(arguments) == original_argument_count:
            return await call_next(context)

        message = context.message.model_copy(update={"arguments": arguments})
        return await call_next(context.copy(message=message))


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
                return getattr(annotations, "read_only_hint", False)
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
        call_next: CallNext[mt.ListToolsRequest, Sequence[FastMCPTool]],
    ) -> Sequence[FastMCPTool]:
        """Filter the tool list based on the filter function.

        Args:
            context: The middleware context.
            call_next: The next handler in the chain.

        Returns:
            Filtered sequence of tools.
        """
        tools = await call_next(context)
        return [
            tool for tool in tools if self._tool_filter(_to_mcp_tool(tool), self._app)
        ]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Deny calls to filtered tools.

        Args:
            context: The middleware context.
            call_next: The next handler in the chain.

        Returns:
            The tool result if allowed.

        Raises:
            ToolUnavailableError: If the tool is filtered out.
        """
        tool_name = context.message.name

        # Look up the tool to check if it should be filtered
        tool = await self._get_tool_by_name(tool_name)
        if tool is not None and not self._tool_filter(tool, self._app):
            raise ToolUnavailableError(
                f"Tool '{tool_name}' is not available. "
                "It may be restricted based on your current session configuration."
            )

        return await call_next(context)

    async def _get_tool_by_name(self, name: str) -> Tool | None:
        """Look up a tool by name from the app.

        Args:
            name: The tool name to look up.

        Returns:
            The Tool object if found, None otherwise.
        """
        fast_tool = await self._app.get_tool(name)
        if fast_tool is None:
            return None

        return fast_tool.to_mcp_tool()
