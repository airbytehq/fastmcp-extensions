# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for ToolCallTelemetryMiddleware."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from fastmcp_extensions import TelemetryConfig
from fastmcp_extensions._telemetry_middleware import (
    ToolCallTelemetryMiddleware,
    ToolCallTelemetryRecord,
    register_tool_call_telemetry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(tool_name: str = "test_tool") -> MiddlewareContext:
    """Build a mock `MiddlewareContext` with the given tool name."""
    ctx = MagicMock(spec=MiddlewareContext)
    ctx.message = MagicMock()
    ctx.message.name = tool_name
    return ctx


def _make_tool_result(text: str = "ok") -> ToolResult:
    """Build a minimal `ToolResult` for test assertions."""
    return ToolResult(content=text)


# ---------------------------------------------------------------------------
# TelemetryRecord (via ToolCallTelemetryRecord alias)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "success,error_type",
    [
        pytest.param(True, None, id="success"),
        pytest.param(False, "ValueError", id="failure"),
    ],
)
def test_record_to_dict(success: bool, error_type: str | None) -> None:
    record = ToolCallTelemetryRecord(
        invocation_type="mcp_tool_call",
        name="my_tool",
        timestamp="2025-01-01T00:00:00+00:00",
        duration_ms=42.5,
        success=success,
        error_type=error_type,
        package_version="1.2.3",
    )
    d = record.to_dict()
    assert d["name"] == "my_tool"
    assert d["invocation_type"] == "mcp_tool_call"
    assert d["duration_ms"] == 42.5
    assert d["success"] is success
    assert d["error_type"] == error_type
    assert d["package_version"] == "1.2.3"


# ---------------------------------------------------------------------------
# Middleware - init
# ---------------------------------------------------------------------------


def test_init_defaults() -> None:
    mw = ToolCallTelemetryMiddleware()
    assert mw._sentry_enabled is False
    assert mw._segment_enabled is False
    assert mw._package_version == "unknown"


def test_init_with_package_name() -> None:
    mw = ToolCallTelemetryMiddleware(package_name="fastmcp-extensions")
    assert mw._package_version != "unknown"


def test_init_with_sentry() -> None:
    with patch("fastmcp_extensions._telemetry.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        mw = ToolCallTelemetryMiddleware(sentry_dsn="https://fake@sentry.io/1")
    assert mw._sentry_enabled is True


def test_init_with_segment() -> None:
    mock_analytics = MagicMock()
    with patch("fastmcp_extensions._telemetry._segment_analytics", mock_analytics):
        mw = ToolCallTelemetryMiddleware(segment_write_key="fake-key")
    assert mw._segment_enabled is True


# ---------------------------------------------------------------------------
# Middleware - on_call_tool (success path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_call_tool_success(caplog: pytest.LogCaptureFixture) -> None:
    mw = ToolCallTelemetryMiddleware()
    ctx = _make_context("list_items")
    expected_result = _make_tool_result("items")

    async def call_next(c: MiddlewareContext) -> ToolResult:
        return expected_result

    with caplog.at_level(logging.INFO):
        result = await mw.on_call_tool(ctx, call_next)

    assert result is expected_result
    assert "list_items" in caplog.text
    assert "ok" in caplog.text


# ---------------------------------------------------------------------------
# Middleware - on_call_tool (failure path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_call_tool_failure(caplog: pytest.LogCaptureFixture) -> None:
    mw = ToolCallTelemetryMiddleware()
    ctx = _make_context("bad_tool")

    async def call_next(c: MiddlewareContext) -> ToolResult:
        raise ValueError("boom")

    with caplog.at_level(logging.INFO), pytest.raises(ValueError, match="boom"):
        await mw.on_call_tool(ctx, call_next)

    assert "bad_tool" in caplog.text
    assert "error=ValueError" in caplog.text


@pytest.mark.asyncio
async def test_extra_properties_are_added_to_telemetry_record() -> None:
    mw = ToolCallTelemetryMiddleware(
        extra_properties={"is_hosted_mcp": True, "name": "wrong"}
    )
    emit = MagicMock()
    mw._sinks.emit = emit

    async def call_next(c: MiddlewareContext) -> ToolResult:
        return _make_tool_result()

    await mw.on_call_tool(_make_context(), call_next)

    record = emit.call_args.args[0]
    assert record.to_dict()["is_hosted_mcp"] is True
    assert record.to_dict()["name"] == "test_tool"


@pytest.mark.asyncio
async def test_extra_properties_callable_is_evaluated_for_each_call() -> None:
    calls = 0

    def extra_properties() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"call_number": calls}

    mw = ToolCallTelemetryMiddleware(extra_properties=extra_properties)
    emit = MagicMock()
    mw._sinks.emit = emit

    async def call_next(c: MiddlewareContext) -> ToolResult:
        return _make_tool_result()

    await mw.on_call_tool(_make_context(), call_next)
    await mw.on_call_tool(_make_context(), call_next)

    assert calls == 2
    assert [call.args[0].to_dict()["call_number"] for call in emit.call_args_list] == [
        1,
        2,
    ]


@pytest.mark.asyncio
async def test_extra_properties_failure_does_not_break_tool_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def extra_properties() -> dict[str, object]:
        raise RuntimeError("boom")

    mw = ToolCallTelemetryMiddleware(extra_properties=extra_properties)
    emit = MagicMock()
    mw._sinks.emit = emit

    async def call_next(c: MiddlewareContext) -> ToolResult:
        return _make_tool_result()

    with caplog.at_level(logging.DEBUG):
        result = await mw.on_call_tool(_make_context(), call_next)

    assert result.content[0].text == "ok"
    assert emit.call_count == 1
    assert emit.call_args.args[0].extra == {}
    assert "Failed to resolve telemetry extra properties" in caplog.text


def test_register_tool_call_telemetry_is_idempotent() -> None:
    app = FastMCP("test-server")
    config = TelemetryConfig()

    assert register_tool_call_telemetry(app, config) is None
    assert register_tool_call_telemetry(app, config) is None
    assert (
        sum(
            isinstance(middleware, ToolCallTelemetryMiddleware)
            for middleware in app.middleware
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Middleware - Sentry breadcrumb sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentry_breadcrumb_emitted() -> None:
    with patch("fastmcp_extensions._telemetry.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        mw = ToolCallTelemetryMiddleware(sentry_dsn="https://fake@sentry.io/1")

        ctx = _make_context("sentry_tool")

        async def call_next(c: MiddlewareContext) -> ToolResult:
            return _make_tool_result()

        await mw.on_call_tool(ctx, call_next)
        mock_sentry.add_breadcrumb.assert_called_once()
        call_kwargs = mock_sentry.add_breadcrumb.call_args
        assert call_kwargs.kwargs["category"] == "mcp_tool_call"


# ---------------------------------------------------------------------------
# Middleware - Segment event sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_segment_event_emitted() -> None:
    mock_analytics = MagicMock()
    with patch(
        "fastmcp_extensions._telemetry._segment_analytics",
        mock_analytics,
    ):
        mw = ToolCallTelemetryMiddleware(segment_write_key="fake-key")

        ctx = _make_context("segment_tool")

        async def call_next(c: MiddlewareContext) -> ToolResult:
            return _make_tool_result()

        await mw.on_call_tool(ctx, call_next)
        mock_analytics.track.assert_called_once()
        args = mock_analytics.track.call_args
        assert args[0][0] == "mcp-server"
        assert args[0][1] == "mcp_tool_call"
