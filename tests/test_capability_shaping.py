# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Tests for per-client capability shaping of tool schemas and calls."""

from __future__ import annotations

import typing
from collections.abc import Iterator
from typing import Annotated, Any, cast

import pytest
from fastmcp import Client, FastMCP
from fastmcp.apps import UI_EXTENSION_ID, AppConfig
from fastmcp.tools import Tool
from mcp.client.extension import ClientExtension

from fastmcp_extensions import (
    Capability,
    RequiresCapability,
    mcp_server,
    mcp_tool,
    register_mcp_tools,
)
from fastmcp_extensions.decorators import (
    _REGISTERED_PROMPTS,
    _REGISTERED_PROVIDERS,
    _REGISTERED_RESOURCES,
    _REGISTERED_TOOLS,
    _clear_registrations,
)
from fastmcp_extensions.registration import _get_capability_gated_args
from fastmcp_extensions.tool_traits import ToolTraits, set_tool_traits

MODULE = "test_capability_shaping"


class _UIClientExtension(ClientExtension):
    identifier = UI_EXTENSION_ID


@pytest.mark.unit
def test_requires_capability_normalizes_capabilities_and_requires_one() -> None:
    """RequiresCapability stores capability IDs as a non-empty string set."""
    marker = RequiresCapability(Capability.UI, "custom/capability")
    assert marker.capabilities == frozenset({Capability.UI.value, "custom/capability"})

    with pytest.raises(ValueError, match="at least one capability"):
        RequiresCapability()


@pytest.mark.unit
def test_capability_gated_annotation_fallback_skips_strings() -> None:
    """Unresolved annotations remain strings and are not evaluated as markers."""

    def tool(show_results: bool = False, unresolved: object = None) -> bool:
        return show_results

    tool.__annotations__["show_results"] = (
        "Annotated[bool, RequiresCapability(Capability.UI)]"
    )
    tool.__annotations__["unresolved"] = "UnresolvedAnnotation"

    assert _get_capability_gated_args(tool) == {}


@pytest.mark.unit
def test_capability_gated_annotation_finds_markers_inside_unions() -> None:
    """Markers inside PEP 604 and Optional unions are collected."""

    def pep604(
        value: Annotated[str, RequiresCapability(Capability.UI)] | None = None,
    ) -> str | None:
        return value

    def optional(
        value: str | None = None,
    ) -> str | None:
        return value

    optional.__annotations__["value"] = typing.Optional[
        Annotated[str, RequiresCapability(Capability.UI)]
    ]

    expected = {"value": frozenset({Capability.UI.value})}
    assert _get_capability_gated_args(pep604) == expected
    assert _get_capability_gated_args(optional) == expected


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
        interactive_ui="optional",
    )
    def optional_ui_tool(
        show_results: Annotated[bool, RequiresCapability(Capability.UI)] = False,
    ) -> bool:
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
        assert tool.input_schema["properties"]["show_results"] == {
            "default": False,
            "type": "boolean",
        }

        result = await client.call_tool("optional_ui_tool", {"show_results": True})
        assert result.data is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_excluded_capability_gated_argument_is_absent_for_all_clients() -> None:
    """Explicit exclusion takes precedence over capability gating."""

    @mcp_tool(
        read_only=True,
        app=AppConfig(resource_uri="ui://test/excluded"),
        interactive_ui="optional",
    )
    def excluded_argument_tool(
        show_results: Annotated[bool, RequiresCapability(Capability.UI)] = False,
    ) -> bool:
        return show_results

    app = mcp_server("test", include_standard_tool_filters=True, telemetry=False)
    register_mcp_tools(app, mcp_module=MODULE, exclude_args=["show_results"])

    async with Client(app) as non_ui_client:
        tool = next(
            tool
            for tool in await non_ui_client.list_tools()
            if tool.name == "excluded_argument_tool"
        )
        assert "show_results" not in tool.input_schema["properties"]

    async with Client(app, extensions=[_UIClientExtension()]) as ui_client:
        tool = next(
            tool
            for tool in await ui_client.list_tools()
            if tool.name == "excluded_argument_tool"
        )
        assert "show_results" not in tool.input_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_union_gated_argument_requires_every_capability() -> None:
    """A union-wrapped argument stays hidden unless every capability is present."""

    @mcp_tool(
        read_only=True,
        app=AppConfig(resource_uri="ui://test/union"),
        interactive_ui="optional",
    )
    def union_gated_tool(
        value: Annotated[
            str,
            RequiresCapability(Capability.UI, "io.example/foo"),
        ]
        | None = None,
    ) -> str:
        return value or "default"

    app = mcp_server("test", include_standard_tool_filters=True, telemetry=False)
    register_mcp_tools(app, mcp_module=MODULE)

    async with Client(app) as non_ui_client:
        tool = next(
            tool
            for tool in await non_ui_client.list_tools()
            if tool.name == "union_gated_tool"
        )
        assert "value" not in tool.input_schema["properties"]
        result = await non_ui_client.call_tool("union_gated_tool", {"value": "hidden"})
        assert result.data == "default"

    async with Client(app, extensions=[_UIClientExtension()]) as ui_client:
        tool = next(
            tool
            for tool in await ui_client.list_tools()
            if tool.name == "union_gated_tool"
        )
        assert "value" not in tool.input_schema["properties"]
        result = await ui_client.call_tool("union_gated_tool", {"value": "hidden"})
        assert result.data == "default"


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
def test_optional_interactive_ui_requires_app() -> None:
    """Optional interactive UI requires an AppConfig."""
    with pytest.raises(ValueError, match=r"interactive_ui='optional' requires app="):

        @mcp_tool(interactive_ui="optional")
        def missing_app() -> str:
            return "no app"


@pytest.mark.unit
def test_interactive_ui_true_requires_app() -> None:
    """Required interactive UI requires an AppConfig."""
    with pytest.raises(ValueError, match=r"interactive_ui=True requires app="):

        @mcp_tool(interactive_ui=True)
        def missing_app() -> str:
            return "no app"


@pytest.mark.unit
def test_interactive_ui_rejects_unknown_string() -> None:
    """Interactive UI only accepts bools or the optional value."""
    with pytest.raises(
        ValueError, match=r"interactive_ui must be a bool or 'optional'"
    ):

        @mcp_tool(interactive_ui=cast(Any, "sometimes"))
        def unknown_interactive_ui() -> str:
            return "unknown"


@pytest.mark.unit
def test_capability_gated_annotation_must_exist_in_signature() -> None:
    """Registration rejects capability-gated arguments absent from the signature."""

    @mcp_tool()
    def missing_argument() -> str:
        return "missing"

    missing_argument.__annotations__["missing"] = Annotated[
        bool,
        RequiresCapability(Capability.UI),
    ]
    app = mcp_server("test", telemetry=False)
    with pytest.raises(ValueError, match=r"parameter 'missing'.*does not exist"):
        register_mcp_tools(app, mcp_module=MODULE)


@pytest.mark.unit
def test_capability_gated_argument_must_have_a_default() -> None:
    """Registration rejects capability-gated arguments without defaults."""

    @mcp_tool()
    def argument_without_default(
        required_argument: Annotated[bool, RequiresCapability(Capability.UI)],
    ) -> bool:
        return required_argument

    app = mcp_server("test", telemetry=False)
    with pytest.raises(ValueError, match=r"parameter 'required_argument'.*default"):
        register_mcp_tools(app, mcp_module=MODULE)


@pytest.mark.parametrize("invalid_argument", ["required", "missing"])
@pytest.mark.parametrize("operation", ["list", "call"])
@pytest.mark.asyncio
async def test_direct_added_tool_rejects_invalid_gated_arguments(
    invalid_argument: str,
    operation: str,
) -> None:
    """Middleware validates traits for tools registered outside register_mcp_tools."""
    if invalid_argument == "required":

        def direct_tool(show_results: bool) -> bool:
            return show_results

        argument_name = "show_results"
        call_arguments = {"show_results": True}
    else:

        def direct_tool() -> bool:
            return False

        argument_name = "missing"
        call_arguments = {}

    app = mcp_server("test", telemetry=False)
    app.add_tool(Tool.from_function(direct_tool, name="direct_tool"))
    set_tool_traits(
        app,
        "direct_tool",
        ToolTraits(
            capability_gated_args={
                argument_name: frozenset({Capability.UI.value}),
            }
        ),
    )

    with pytest.raises(
        ValueError,
        match=rf"parameter '{argument_name}'.*tool 'direct_tool'",
    ):
        if operation == "list":
            await app.list_tools()
        else:
            await app.call_tool("direct_tool", call_arguments)
