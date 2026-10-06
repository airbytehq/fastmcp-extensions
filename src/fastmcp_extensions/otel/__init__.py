# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""OpenTelemetry tracing for MCP tool calls (requires the `[otel]` extra).

Turn it on with `TelemetryConfig(tool_tracing=True)` or a `ToolTracingConfig`.
See the README section "Tool-Call Tracing" for the exported attributes.
"""

from fastmcp_extensions.otel.middleware import (
    ToolCallOtelMiddleware,
    add_trace_attributes,
    capture_tool_spans,
    register_tool_call_tracing,
    trace_plan,
)
from fastmcp_extensions.otel.models import ToolTracingConfig, TraceArg

__all__ = [
    "ToolCallOtelMiddleware",
    "ToolTracingConfig",
    "TraceArg",
    "add_trace_attributes",
    "capture_tool_spans",
    "register_tool_call_tracing",
    "trace_plan",
]
