# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for `tool_group` / `mutation_class` on tool-call telemetry."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from fastmcp_extensions import (
    MutationClass,
    ToolCallTelemetryMiddleware,
    mcp_provider,
    mcp_tool,
    register_mcp_tools,
    tool_telemetry_properties,
)
from fastmcp_extensions.decorators import _clear_registrations

MODULE = "test_tool_telemetry_properties"


@pytest.fixture(autouse=True)
def _isolated_registrations() -> Iterator[None]:
    _clear_registrations()
    yield
    _clear_registrations()


@pytest.mark.parametrize(
    "annotations,expected",
    [
        pytest.param({"readOnlyHint": True}, MutationClass.READ, id="read"),
        pytest.param(
            {"readOnlyHint": True, "destructiveHint": True},
            MutationClass.READ,
            id="read_only_wins_over_destructive",
        ),
        pytest.param(
            {"readOnlyHint": False, "destructiveHint": False},
            MutationClass.MUTATE,
            id="mutate",
        ),
        pytest.param(
            {"readOnlyHint": False, "destructiveHint": True},
            MutationClass.DESTRUCTIVE,
            id="destructive",
        ),
        pytest.param(
            {"readOnlyHint": False}, MutationClass.UNKNOWN, id="missing_destructive"
        ),
        pytest.param(
            {"destructiveHint": True}, MutationClass.UNKNOWN, id="missing_read_only"
        ),
        pytest.param({"title": "x"}, MutationClass.UNKNOWN, id="no_hints"),
        pytest.param(None, MutationClass.UNKNOWN, id="no_annotations"),
    ],
)
def test_mutation_class_from_annotations(
    annotations: dict[str, object] | None, expected: MutationClass
) -> None:
    parsed = (
        None if annotations is None else ToolAnnotations.model_validate(annotations)
    )
    assert MutationClass.from_annotations(parsed) is expected


def _instrumented_app() -> tuple[FastMCP, MagicMock]:
    @mcp_tool(read_only=True)
    def read_tool() -> str:
        return "ok"

    @mcp_tool()
    def mutate_tool() -> str:
        return "ok"

    @mcp_tool(destructive=True)
    def destructive_tool() -> str:
        return "ok"

    @mcp_tool(destructive=True)
    def failing_destructive_tool() -> str:
        raise ValueError("boom")

    class DestructiveProvider(Provider):
        async def _list_tools(self) -> list[Tool]:
            def provider_tool() -> str:
                return "ok"

            return [Tool.from_function(provider_tool, name="provider_tool")]

    @mcp_provider(annotations={"readOnlyHint": False, "destructiveHint": True})
    def destructive_provider() -> Provider:
        return DestructiveProvider()

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module=MODULE)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def plain_annotated_tool() -> str:
        return "ok"

    @app.tool
    def plain_tool() -> str:
        return "ok"

    middleware = ToolCallTelemetryMiddleware()
    emit = MagicMock()
    middleware._sinks.emit = emit
    app.add_middleware(middleware)
    return app, emit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,tool_group,mutation_class",
    [
        pytest.param("read_tool", MODULE, "read", id="mcp_tool_read"),
        pytest.param("mutate_tool", MODULE, "mutate", id="mcp_tool_mutate"),
        pytest.param(
            "destructive_tool", MODULE, "destructive", id="mcp_tool_destructive"
        ),
        pytest.param("provider_tool", MODULE, "destructive", id="provider_tool"),
        pytest.param("plain_annotated_tool", None, "read", id="plain_annotated"),
        pytest.param("plain_tool", None, "unknown", id="plain_unannotated"),
    ],
)
async def test_tool_call_event_carries_tool_group_and_mutation_class(
    tool_name: str, tool_group: str | None, mutation_class: str
) -> None:
    app, emit = _instrumented_app()

    async with Client(app) as client:
        await client.call_tool(tool_name, {})

    properties = emit.call_args.args[0].to_dict()
    assert properties["name"] == tool_name
    assert properties["invocation_type"] == "mcp_tool_call"
    assert properties["success"] is True
    assert properties["tool_group"] == tool_group
    assert properties["mutation_class"] == mutation_class


@pytest.mark.asyncio
async def test_failed_tool_call_still_carries_tool_properties() -> None:
    app, emit = _instrumented_app()

    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("failing_destructive_tool", {})

    properties = emit.call_args.args[0].to_dict()
    assert properties["success"] is False
    assert properties["tool_group"] == MODULE
    assert properties["mutation_class"] == "destructive"


@pytest.mark.asyncio
async def test_unknown_tool_reports_unknown_mutation_class() -> None:
    app, emit = _instrumented_app()

    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("does_not_exist", {})

    properties = emit.call_args.args[0].to_dict()
    assert properties["name"] == "does_not_exist"
    assert properties["tool_group"] is None
    assert properties["mutation_class"] == "unknown"


@pytest.mark.asyncio
async def test_extra_properties_override_tool_properties() -> None:
    @mcp_tool(read_only=True)
    def read_tool() -> str:
        return "ok"

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module=MODULE)
    middleware = ToolCallTelemetryMiddleware(
        extra_properties={"tool_group": "custom-group"}
    )
    emit = MagicMock()
    middleware._sinks.emit = emit
    app.add_middleware(middleware)

    async with Client(app) as client:
        await client.call_tool("read_tool", {})

    properties = emit.call_args.args[0].to_dict()
    assert properties["tool_group"] == "custom-group"
    assert properties["mutation_class"] == "read"


def test_tool_telemetry_properties_helper() -> None:
    @mcp_tool(destructive=True)
    def destructive_tool() -> str:
        return "ok"

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module=MODULE)

    assert tool_telemetry_properties(app, "destructive_tool") == {
        "tool_group": MODULE,
        "mutation_class": "destructive",
    }
    assert tool_telemetry_properties(app, "missing") == {
        "tool_group": None,
        "mutation_class": "unknown",
    }
