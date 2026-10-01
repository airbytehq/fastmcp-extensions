# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for deployment-defined tool capabilities."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastmcp import Client, FastMCP
from mcp.shared.exceptions import MCPError

from fastmcp_extensions import (
    Capability,
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
from fastmcp_extensions.tool_filters import capability_filter

MODULE = "test_custom_capabilities"
CUSTOM_CAPABILITY = "io.example/custom"


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_custom_capability_resolver_gates_tool_and_receives_app() -> None:
    """Custom capabilities are available only when their resolver returns true."""
    resolver_apps: list[FastMCP] = []
    available = False

    def resolver(app: FastMCP) -> bool:
        resolver_apps.append(app)
        return available

    @mcp_tool(required_capabilities=[CUSTOM_CAPABILITY])
    def custom_tool() -> str:
        """A custom-capability tool."""
        return "ok"

    app = mcp_server(
        "test",
        include_standard_tool_filters=True,
        capability_resolvers={CUSTOM_CAPABILITY: resolver},
        telemetry=False,
    )
    register_mcp_tools(app, mcp_module=MODULE)
    tool = await app.get_tool("custom_tool")
    assert tool is not None

    assert capability_filter(tool, app) is False
    assert resolver_apps == [app]

    available = True
    assert capability_filter(tool, app) is True
    assert resolver_apps == [app, app]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_custom_capability_without_resolver_is_unavailable() -> None:
    """An unknown required capability without a resolver stays unavailable."""

    @mcp_tool(required_capabilities=[CUSTOM_CAPABILITY])
    def custom_tool() -> str:
        """A custom-capability tool."""
        return "ok"

    app = mcp_server("test", include_standard_tool_filters=True, telemetry=False)
    register_mcp_tools(app, mcp_module=MODULE)
    tool = await app.get_tool("custom_tool")
    assert tool is not None

    assert capability_filter(tool, app) is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_raising_custom_capability_resolver_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resolver errors do not escape or make a custom capability available."""

    def resolver(_app: FastMCP) -> bool:
        raise RuntimeError("sensitive resolver detail")

    @mcp_tool(required_capabilities=[CUSTOM_CAPABILITY])
    def custom_tool() -> str:
        """A custom-capability tool."""
        return "ok"

    app = mcp_server(
        "test",
        include_standard_tool_filters=True,
        capability_resolvers={CUSTOM_CAPABILITY: resolver},
        telemetry=False,
    )
    register_mcp_tools(app, mcp_module=MODULE)
    tool = await app.get_tool("custom_tool")
    assert tool is not None

    with caplog.at_level(logging.WARNING):
        assert capability_filter(tool, app) is False
    assert CUSTOM_CAPABILITY in caplog.text
    assert "RuntimeError" in caplog.text
    assert "sensitive resolver detail" not in caplog.text


@pytest.mark.unit
def test_mcp_server_rejects_builtin_and_invalid_capability_resolver_keys() -> None:
    """Built-in capabilities cannot be overridden and keys must be non-empty strings."""
    with pytest.raises(ValueError, match="cannot override built-in"):
        mcp_server(
            "test",
            capability_resolvers={
                Capability.CLIENT_FILESYSTEM.value: lambda _app: True
            },
        )

    for key in ("", "   ", cast(Any, 1)):
        with pytest.raises(ValueError, match="non-empty strings"):
            mcp_server(
                "test",
                capability_resolvers=cast(Any, {key: lambda _app: True}),
            )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_tools_without_capabilities_do_not_invoke_resolvers() -> None:
    """Tools without requirements bypass custom capability resolvers."""
    resolver_calls = 0

    def resolver(_app: FastMCP) -> bool:
        nonlocal resolver_calls
        resolver_calls += 1
        return True

    @mcp_tool()
    def ordinary_tool() -> str:
        """A tool without capability requirements."""
        return "ok"

    app = mcp_server(
        "test",
        include_standard_tool_filters=True,
        capability_resolvers={CUSTOM_CAPABILITY: resolver},
        telemetry=False,
    )
    register_mcp_tools(app, mcp_module=MODULE)
    tool = await app.get_tool("ordinary_tool")
    assert tool is not None

    assert capability_filter(tool, app) is True
    assert resolver_calls == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_client_filters_custom_capability_list_and_calls() -> None:
    """Client requests see and can call the tool after its resolver enables it."""
    available = False

    def resolver(_app: FastMCP) -> bool:
        return available

    @mcp_tool(required_capabilities=[CUSTOM_CAPABILITY])
    def custom_tool() -> str:
        """A custom-capability tool."""
        return "ok"

    app = mcp_server(
        "test",
        include_standard_tool_filters=True,
        capability_resolvers={CUSTOM_CAPABILITY: resolver},
        telemetry=False,
    )
    register_mcp_tools(app, mcp_module=MODULE)

    async with Client(app) as client:
        tools = await client.list_tools()
        assert "custom_tool" not in {tool.name for tool in tools}
        with pytest.raises(MCPError):
            await client.call_tool("custom_tool", {})

        available = True
        tools = await client.list_tools()
        assert "custom_tool" in {tool.name for tool in tools}
        result = await client.call_tool("custom_tool", {})
        assert result.content[0].text == "ok"
