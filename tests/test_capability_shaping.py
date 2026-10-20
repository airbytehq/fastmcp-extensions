# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Tests for per-client capability shaping of tool schemas and calls."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastmcp import Client, FastMCP
from fastmcp.apps import UI_EXTENSION_ID, AppConfig
from fastmcp.tools import Tool
from mcp.client.extension import ClientExtension

from fastmcp_extensions import Capability, mcp_server, mcp_tool, register_mcp_tools
from fastmcp_extensions.decorators import (
    _REGISTERED_PROMPTS,
    _REGISTERED_PROVIDERS,
    _REGISTERED_RESOURCES,
    _REGISTERED_TOOLS,
    _clear_registrations,
)
from fastmcp_extensions.tool_traits import ToolTraits, set_tool_traits

MODULE = "test_capability_shaping"


class _UIClientExtension(ClientExtension):
    identifier = UI_EXTENSION_ID


@pytest.fixture(autouse=True)
def _isolated_registrations() -> Iterator[None]:
    registrations = (
        list(_REGISTERED_TOOLS),
        list(_REGISTERED_PROVIDERS),
        list(_REGISTERED_RESOURCES),
        list(_REGISTERED_PROMPTS),
    )
    _clear_registrations()
    yield
    _clear_registrations()
    _REGISTERED_TOOLS.extend(registrations[0])
    _REGISTERED_PROVIDERS.extend(registrations[1])
    _REGISTERED_RESOURCES.extend(registrations[2])
    _REGISTERED_PROMPTS.extend(registrations[3])


def _optional_ui_app() -> FastMCP:
    @mcp_tool(
        read_only=True,
        app=AppConfig(resource_uri="ui://test/view"),
        ui_optional=True,
        capability_gated_args={"show_results": [Capability.UI]},
    )
    def optional_ui_tool(show_results: bool = False) -> bool:
        """Return whether results should be shown."""
        return show_results

    app = mcp_server("test", include_standard_tool_filters=True, telemetry=False)
    register_mcp_tools(app, mcp_module=MODULE)
    return app


@pytest.mark.asyncio
@pytest.mark.unit
async def test_non_ui_client_sees_text_tool_without_ui_argument() -> None:
    """Non-UI clients see the tool, but not its UI-specific argument or metadata."""
    app = _optional_ui_app()

    async with Client(app) as client:
        tools = await client.list_tools()
        tool = next(tool for tool in tools if tool.name == "optional_ui_tool")
        assert "show_results" not in tool.input_schema["properties"]
        assert not (tool.meta or {}).get("ui")

        result = await client.call_tool("optional_ui_tool", {"show_results": True})
        assert result.data is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ui_client_sees_ui_argument_and_metadata() -> None:
    """UI clients see the UI marker and can pass the capability-gated argument."""
    app = _optional_ui_app()

    async with Client(app, extensions=[_UIClientExtension()]) as client:
        tools = await client.list_tools()
        tool = next(tool for tool in tools if tool.name == "optional_ui_tool")
        assert "show_results" in tool.input_schema["properties"]
        assert tool.meta["ui"]["resourceUri"] == "ui://test/view"

        result = await client.call_tool("optional_ui_tool", {"show_results": True})
        assert result.data is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_app_tool_without_ui_optional_remains_ui_gated() -> None:
    """App tools without ui_optional remain hidden from non-UI clients."""

    @mcp_tool(app=AppConfig(resource_uri="ui://test/required-view"))
    def required_ui_tool() -> str:
        """Return UI data."""
        return "view"

    app = mcp_server("test", include_standard_tool_filters=True, telemetry=False)
    register_mcp_tools(app, mcp_module=MODULE)

    async with Client(app) as non_ui_client:
        assert "required_ui_tool" not in {
            tool.name for tool in await non_ui_client.list_tools()
        }

    async with Client(app, extensions=[_UIClientExtension()]) as ui_client:
        tools = await ui_client.list_tools()
        tool = next(tool for tool in tools if tool.name == "required_ui_tool")
        assert tool.meta["ui"]["resourceUri"] == "ui://test/required-view"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_traits_on_directly_added_tool_shape_ui_per_client() -> None:
    """Traits set after app.add_tool also drive per-client shaping."""

    def direct_ui_tool(show_results: bool = False) -> bool:
        return show_results

    tool = Tool.from_function(
        direct_ui_tool,
        name="direct_ui_tool",
        meta={"ui": {"resourceUri": "ui://test/view"}},
    )
    app = mcp_server("test", include_standard_tool_filters=True, telemetry=False)
    app.add_tool(tool)
    set_tool_traits(
        app,
        "direct_ui_tool",
        ToolTraits(
            ui_optional=True,
            capability_gated_args={"show_results": frozenset({Capability.UI})},
        ),
    )

    async with Client(app) as non_ui_client:
        listed_tool = next(
            tool
            for tool in await non_ui_client.list_tools()
            if tool.name == "direct_ui_tool"
        )
        assert "show_results" not in listed_tool.input_schema["properties"]
        assert not (listed_tool.meta or {}).get("ui")
        result = await non_ui_client.call_tool("direct_ui_tool", {"show_results": True})
        assert result.data is False

    async with Client(app, extensions=[_UIClientExtension()]) as ui_client:
        listed_tool = next(
            tool
            for tool in await ui_client.list_tools()
            if tool.name == "direct_ui_tool"
        )
        assert "show_results" in listed_tool.input_schema["properties"]
        assert listed_tool.meta["ui"]["resourceUri"] == "ui://test/view"
        result = await ui_client.call_tool("direct_ui_tool", {"show_results": True})
        assert result.data is True


@pytest.mark.unit
def test_ui_optional_requires_app() -> None:
    """ui_optional requires an AppConfig."""
    with pytest.raises(ValueError, match="ui_optional=True requires app="):

        @mcp_tool(ui_optional=True)
        def missing_app() -> str:
            return "no app"


@pytest.mark.unit
def test_ui_optional_cannot_be_combined_with_interactive_ui() -> None:
    """ui_optional and interactive_ui describe incompatible UI requirements."""
    with pytest.raises(ValueError, match="cannot be combined"):

        @mcp_tool(
            ui_optional=True,
            interactive_ui=True,
            app=AppConfig(resource_uri="ui://test/view"),
        )
        def conflicting_ui_tool() -> str:
            return "conflict"


@pytest.mark.unit
def test_capability_gated_argument_must_exist() -> None:
    """Registration rejects capability-gated arguments absent from the signature."""

    @mcp_tool(capability_gated_args={"missing": [Capability.UI]})
    def missing_argument() -> str:
        return "missing"

    app = mcp_server("test", telemetry=False)
    with pytest.raises(ValueError, match=r"parameter 'missing'.*does not exist"):
        register_mcp_tools(app, mcp_module=MODULE)


@pytest.mark.unit
def test_capability_gated_argument_must_have_a_default() -> None:
    """Registration rejects capability-gated arguments without defaults."""

    @mcp_tool(capability_gated_args={"required_argument": [Capability.UI]})
    def argument_without_default(required_argument: bool) -> bool:
        return required_argument

    app = mcp_server("test", telemetry=False)
    with pytest.raises(ValueError, match=r"parameter 'required_argument'.*default"):
        register_mcp_tools(app, mcp_module=MODULE)
