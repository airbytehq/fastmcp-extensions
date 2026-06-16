# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for ToolCallTelemetryMiddleware."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult

from fastmcp_extensions._telemetry_middleware import (
    ToolCallTelemetryMiddleware,
    ToolCallTelemetryRecord,
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
# ToolCallTelemetryRecord
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
        tool_name="my_tool",
        timestamp="2025-01-01T00:00:00+00:00",
        duration_ms=42.5,
        success=success,
        error_type=error_type,
        package_version="1.2.3",
    )
    d = record.to_dict()
    assert d["tool_name"] == "my_tool"
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


def test_init_sentry_without_sdk(caplog: pytest.LogCaptureFixture) -> None:
    with patch("fastmcp_extensions._telemetry_middleware._HAS_SENTRY", False):
        with caplog.at_level(logging.DEBUG):
            mw = ToolCallTelemetryMiddleware(sentry_dsn="https://fake@sentry.io/1")
        assert mw._sentry_enabled is False
        assert "sentry-sdk is not installed" in caplog.text


def test_init_segment_without_sdk(caplog: pytest.LogCaptureFixture) -> None:
    with patch("fastmcp_extensions._telemetry_middleware._HAS_SEGMENT", False):
        with caplog.at_level(logging.DEBUG):
            mw = ToolCallTelemetryMiddleware(segment_write_key="fake-key")
        assert mw._segment_enabled is False
        assert "analytics-python is not installed" in caplog.text


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


# ---------------------------------------------------------------------------
# Middleware - Sentry breadcrumb sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentry_breadcrumb_emitted() -> None:
    with patch("fastmcp_extensions._telemetry_middleware.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        with patch("fastmcp_extensions._telemetry_middleware._HAS_SENTRY", True):
            mw = ToolCallTelemetryMiddleware(sentry_dsn="https://fake@sentry.io/1")

        ctx = _make_context("sentry_tool")

        async def call_next(c: MiddlewareContext) -> ToolResult:
            return _make_tool_result()

        await mw.on_call_tool(ctx, call_next)
        mock_sentry.add_breadcrumb.assert_called_once()
        call_kwargs = mock_sentry.add_breadcrumb.call_args
        assert call_kwargs.kwargs["category"] == "mcp.tool_call"


# ---------------------------------------------------------------------------
# Middleware - Segment event sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_segment_event_emitted() -> None:
    mock_analytics = MagicMock()
    with patch(
        "fastmcp_extensions._telemetry_middleware._segment_analytics",
        mock_analytics,
    ):
        with patch("fastmcp_extensions._telemetry_middleware._HAS_SEGMENT", True):
            mw = ToolCallTelemetryMiddleware(segment_write_key="fake-key")

        ctx = _make_context("segment_tool")

        async def call_next(c: MiddlewareContext) -> ToolResult:
            return _make_tool_result()

        await mw.on_call_tool(ctx, call_next)
        mock_analytics.track.assert_called_once()
        args = mock_analytics.track.call_args
        assert args[0][0] == "mcp-server"
        assert args[0][1] == "mcp_tool_call"
