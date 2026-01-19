# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the ToolFilterMiddleware."""

from collections.abc import Sequence
from unittest.mock import MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext
from mcp.types import Tool, ToolAnnotations

from fastmcp_extensions import ToolFilterFn, ToolFilterMiddleware


def _create_mock_tool(
    name: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
) -> Tool:
    """Create a mock Tool object for testing."""
    annotations = ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
    )
    return Tool(
        name=name,
        description=f"Test tool: {name}",
        inputSchema={"type": "object", "properties": {}},
        annotations=annotations,
    )


def _create_mock_context(
    method: str,
    message: MagicMock | None = None,
) -> MiddlewareContext:
    """Create a mock MiddlewareContext for testing."""
    context = MagicMock(spec=MiddlewareContext)
    context.method = method
    context.message = message or MagicMock()
    return context


@pytest.mark.unit
def test_tool_filter_middleware_init() -> None:
    """Test that ToolFilterMiddleware initializes correctly."""
    app = FastMCP("test-server")

    def filter_fn(tool: Tool, app: FastMCP) -> bool:
        return True

    middleware = ToolFilterMiddleware(app, tool_filter=filter_fn)
    assert middleware._app is app
    assert middleware._tool_filter is filter_fn


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_list_tools_filters_tools() -> None:
    """Test that on_list_tools filters tools based on the filter function."""
    app = FastMCP("test-server")

    # Create tools - one read-only, one not
    read_only_tool = _create_mock_tool("read_tool", read_only=True)
    write_tool = _create_mock_tool("write_tool", read_only=False)
    all_tools = [read_only_tool, write_tool]

    # Filter function that only allows read-only tools
    def readonly_filter(tool: Tool, app: FastMCP) -> bool:
        if tool.annotations is None:
            return False
        return getattr(tool.annotations, "readOnlyHint", False)

    middleware = ToolFilterMiddleware(app, tool_filter=readonly_filter)

    # Mock the call_next to return all tools
    async def mock_call_next(ctx: MiddlewareContext) -> Sequence[Tool]:
        return all_tools

    context = _create_mock_context("tools/list")
    result = await middleware.on_list_tools(context, mock_call_next)

    # Should only return the read-only tool
    assert len(result) == 1
    assert result[0].name == "read_tool"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_list_tools_returns_all_when_filter_allows_all() -> None:
    """Test that on_list_tools returns all tools when filter allows all."""
    app = FastMCP("test-server")

    tool1 = _create_mock_tool("tool1")
    tool2 = _create_mock_tool("tool2")
    all_tools = [tool1, tool2]

    # Filter function that allows all tools
    def allow_all(tool: Tool, app: FastMCP) -> bool:
        return True

    middleware = ToolFilterMiddleware(app, tool_filter=allow_all)

    async def mock_call_next(ctx: MiddlewareContext) -> Sequence[Tool]:
        return all_tools

    context = _create_mock_context("tools/list")
    result = await middleware.on_list_tools(context, mock_call_next)

    assert len(result) == 2
    assert {t.name for t in result} == {"tool1", "tool2"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_list_tools_returns_empty_when_filter_denies_all() -> None:
    """Test that on_list_tools returns empty list when filter denies all."""
    app = FastMCP("test-server")

    tool1 = _create_mock_tool("tool1")
    tool2 = _create_mock_tool("tool2")
    all_tools = [tool1, tool2]

    # Filter function that denies all tools
    def deny_all(tool: Tool, app: FastMCP) -> bool:
        return False

    middleware = ToolFilterMiddleware(app, tool_filter=deny_all)

    async def mock_call_next(ctx: MiddlewareContext) -> Sequence[Tool]:
        return all_tools

    context = _create_mock_context("tools/list")
    result = await middleware.on_list_tools(context, mock_call_next)

    assert len(result) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_call_tool_allows_visible_tools() -> None:
    """Test that on_call_tool allows calls to visible tools."""
    app = FastMCP("test-server")

    # Register a tool with the app
    @app.tool()
    def allowed_tool() -> str:
        return "success"

    # Filter function that allows all tools
    def allow_all(tool: Tool, app: FastMCP) -> bool:
        return True

    middleware = ToolFilterMiddleware(app, tool_filter=allow_all)

    # Create context with tool name
    message = MagicMock()
    message.name = "allowed_tool"
    context = _create_mock_context("tools/call", message)

    # Mock call_next to return a result
    expected_result = MagicMock()

    async def mock_call_next(ctx: MiddlewareContext) -> MagicMock:
        return expected_result

    result = await middleware.on_call_tool(context, mock_call_next)
    assert result is expected_result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_call_tool_denies_filtered_tools() -> None:
    """Test that on_call_tool raises error for filtered tools."""
    app = FastMCP("test-server")

    # Register a tool with the app
    @app.tool()
    def filtered_tool() -> str:
        return "should not be called"

    # Filter function that denies all tools
    def deny_all(tool: Tool, app: FastMCP) -> bool:
        return False

    middleware = ToolFilterMiddleware(app, tool_filter=deny_all)

    # Create context with tool name
    message = MagicMock()
    message.name = "filtered_tool"
    context = _create_mock_context("tools/call", message)

    async def mock_call_next(ctx: MiddlewareContext) -> MagicMock:
        return MagicMock()

    with pytest.raises(ValueError, match="not available"):
        await middleware.on_call_tool(context, mock_call_next)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_call_tool_allows_unknown_tools() -> None:
    """Test that on_call_tool allows calls to unknown tools (not in tool manager)."""
    app = FastMCP("test-server")

    # Don't register any tools - the tool won't be found in the manager

    # Filter function that denies all tools
    def deny_all(tool: Tool, app: FastMCP) -> bool:
        return False

    middleware = ToolFilterMiddleware(app, tool_filter=deny_all)

    # Create context with unknown tool name
    message = MagicMock()
    message.name = "unknown_tool"
    context = _create_mock_context("tools/call", message)

    expected_result = MagicMock()

    async def mock_call_next(ctx: MiddlewareContext) -> MagicMock:
        return expected_result

    # Should allow the call since the tool isn't found (let FastMCP handle the error)
    result = await middleware.on_call_tool(context, mock_call_next)
    assert result is expected_result


@pytest.mark.unit
def test_tool_filter_fn_type_alias_exported() -> None:
    """Test that ToolFilterFn type alias is properly exported."""

    # Verify it's a callable type
    assert ToolFilterFn is not None


@pytest.mark.unit
def test_tool_filter_middleware_exported() -> None:
    """Test that ToolFilterMiddleware is properly exported."""
    from fastmcp_extensions import ToolFilterMiddleware

    assert ToolFilterMiddleware is not None


@pytest.mark.parametrize(
    "tool_name,read_only,expected_visible",
    [
        pytest.param("read_tool", True, True, id="readonly_tool_visible"),
        pytest.param("write_tool", False, False, id="write_tool_hidden"),
        pytest.param("another_read", True, True, id="another_readonly_visible"),
    ],
)
@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_list_tools_parametrized(
    tool_name: str,
    read_only: bool,
    expected_visible: bool,
) -> None:
    """Test on_list_tools with various tool configurations."""
    app = FastMCP("test-server")

    tool = _create_mock_tool(tool_name, read_only=read_only)

    # Filter function that only allows read-only tools
    def readonly_filter(tool: Tool, app: FastMCP) -> bool:
        if tool.annotations is None:
            return False
        return getattr(tool.annotations, "readOnlyHint", False)

    middleware = ToolFilterMiddleware(app, tool_filter=readonly_filter)

    async def mock_call_next(ctx: MiddlewareContext) -> Sequence[Tool]:
        return [tool]

    context = _create_mock_context("tools/list")
    result = await middleware.on_list_tools(context, mock_call_next)

    if expected_visible:
        assert len(result) == 1
        assert result[0].name == tool_name
    else:
        assert len(result) == 0
