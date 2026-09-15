# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for concise user-facing MCP tool errors."""

from __future__ import annotations

import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from fastmcp_extensions import (
    ToolCallTelemetryMiddleware,
    UserFacingErrorMiddleware,
    mcp_server,
)


class MyError(Exception):
    """An exception used by the middleware tests."""


class MyChildError(MyError):
    """A subclass used to verify exception matching."""


async def _call_tool(server: FastMCP, name: str = "raise_error") -> None:
    async with Client(server) as client:
        await client.call_tool(name)


@pytest.mark.asyncio
async def test_configured_error_uses_concise_tool_error() -> None:
    server = FastMCP(
        "test",
        middleware=[UserFacingErrorMiddleware((MyError,))],
    )

    @server.tool
    def raise_error() -> None:
        raise MyError("bad")

    with pytest.raises(ToolError) as raised:
        await _call_tool(server)

    assert str(raised.value) == "bad"
    assert "Error calling tool" not in str(raised.value)


@pytest.mark.asyncio
async def test_subclass_of_configured_error_is_converted() -> None:
    server = FastMCP(
        "test",
        middleware=[UserFacingErrorMiddleware((MyError,))],
    )

    @server.tool
    def raise_error() -> None:
        raise MyChildError("child failure")

    with pytest.raises(ToolError, match="child failure"):
        await _call_tool(server)


@pytest.mark.asyncio
async def test_unconfigured_error_uses_fastmcp_default_handling() -> None:
    server = FastMCP(
        "test",
        middleware=[UserFacingErrorMiddleware((MyError,))],
    )

    @server.tool
    def raise_error() -> None:
        raise RuntimeError("unexpected failure")

    with pytest.raises(ToolError) as raised:
        await _call_tool(server)

    assert "Error calling tool" in str(raised.value)


@pytest.mark.asyncio
async def test_custom_formatter_is_applied() -> None:
    server = FastMCP(
        "test",
        middleware=[
            UserFacingErrorMiddleware(
                (MyError,),
                formatter=lambda error: f"formatted: {error}",
            )
        ],
    )

    @server.tool
    def raise_error() -> None:
        raise MyError("bad")

    with pytest.raises(ToolError) as raised:
        await _call_tool(server)

    assert str(raised.value) == "formatted: bad"


def test_empty_error_types_are_rejected() -> None:
    with pytest.raises(ValueError, match="error_types"):
        UserFacingErrorMiddleware(())


@pytest.mark.asyncio
async def test_mcp_server_registers_user_facing_errors() -> None:
    server = mcp_server(
        "test",
        telemetry=False,
        user_facing_errors=[MyError],
    )

    @server.tool
    def raise_error() -> None:
        raise MyError("from factory")

    with pytest.raises(ToolError, match="from factory"):
        await _call_tool(server)

    assert any(
        isinstance(middleware, UserFacingErrorMiddleware)
        for middleware in server.middleware
    )


@pytest.mark.asyncio
async def test_user_facing_errors_wrap_telemetry_and_preserve_original_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = mcp_server(
        "test",
        user_facing_errors=[MyError],
    )

    @server.tool
    def raise_error() -> None:
        raise MyError("telemetry failure")

    user_facing_index = next(
        index
        for index, middleware in enumerate(server.middleware)
        if isinstance(middleware, UserFacingErrorMiddleware)
    )
    telemetry_index = next(
        index
        for index, middleware in enumerate(server.middleware)
        if isinstance(middleware, ToolCallTelemetryMiddleware)
    )
    assert user_facing_index < telemetry_index

    with caplog.at_level(
        logging.INFO, logger="fastmcp_extensions._telemetry"
    ), pytest.raises(ToolError, match="telemetry failure"):
        await _call_tool(server)

    assert "error=MyError" in caplog.text
