# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Telemetry middleware for MCP tool call instrumentation.

Intercepts every `tools/call` invocation and records structured telemetry:

- `tool_name`, `timestamp`, `duration_ms`, `success`/`failure`, `error_type`
- `package_version` (when a `package_name` is provided)

Three telemetry sinks, each independently toggled:

1. **Structured JSON log** - always on (Python `logging`, `INFO` level).
2. **Sentry breadcrumb** - enabled when a `sentry_dsn` is supplied.
   Requires `sentry-sdk` (install via `pip install fastmcp-extensions[telemetry]`).
3. **Segment analytics event** - enabled when a `segment_write_key` is supplied.
   Requires `analytics-python` (install via `pip install fastmcp-extensions[telemetry]`).

Usage:

```python
from fastmcp_extensions import mcp_server, ToolCallTelemetryMiddleware

app = mcp_server(name="my-server", package_name="my-package")
app.add_middleware(
    ToolCallTelemetryMiddleware(
        package_name="my-package",
        sentry_dsn="https://...@sentry.io/...",
        segment_write_key="hnWfMdE...",
    )
)
```
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from fastmcp_extensions._telemetry import TelemetryRecord, TelemetrySinks

if TYPE_CHECKING:
    from mcp import types as mt

# Re-export for backward compatibility
ToolCallTelemetryRecord = TelemetryRecord


class ToolCallTelemetryMiddleware(Middleware):
    """Middleware that records telemetry for every MCP tool invocation.

    Captured fields per call:

    - `tool_name` - the MCP tool that was invoked
    - `timestamp` - ISO-8601 UTC timestamp of the call start
    - `duration_ms` - wall-clock execution time in milliseconds
    - `success` - whether the call completed without raising
    - `error_type` - the exception class name on failure (`None` on success)
    - `package_version` - the installed version of `package_name`

    Telemetry is emitted to up to three sinks:

    1. **Structured JSON log** at `INFO` level (always on).
    2. **Sentry breadcrumb** (`mcp.tool_call` category) when `sentry_dsn` is set.
    3. **Segment event** (`mcp_tool_call`) when `segment_write_key` is set.

    Example:

    ```python
    app.add_middleware(
        ToolCallTelemetryMiddleware(
            package_name="my-package",
            sentry_dsn="https://...@sentry.io/...",
            segment_write_key="hnWfMdE...",
        )
    )
    ```
    """

    def __init__(
        self,
        *,
        package_name: str | None = None,
        sentry_dsn: str | None = None,
        segment_write_key: str | None = None,
        segment_user_id: str = "mcp-server",
    ) -> None:
        """Initialize the telemetry middleware.

        Sentry and Segment sinks are configured here - if the corresponding
        SDK is not installed the sink is silently skipped and a debug log is
        emitted.
        """
        self._sinks = TelemetrySinks(
            package_name=package_name,
            sentry_dsn=sentry_dsn,
            segment_write_key=segment_write_key,
            segment_user_id=segment_user_id,
        )

    @property
    def _sentry_enabled(self) -> bool:
        return self._sinks.sentry_enabled

    @property
    def _segment_enabled(self) -> bool:
        return self._sinks.segment_enabled

    @property
    def _package_version(self) -> str:
        return self._sinks.package_version

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: Callable[[MiddlewareContext[mt.CallToolRequestParams]], ToolResult],
    ) -> ToolResult:
        """Wrap tool execution with telemetry collection."""
        tool_name: str = context.message.name
        timestamp = datetime.now(tz=timezone.utc)
        start = time.monotonic()

        success = True
        error_type: str | None = None

        try:
            result = await call_next(context)
        except Exception as exc:
            success = False
            error_type = type(exc).__name__
            raise
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            record = TelemetryRecord(
                invocation_type="mcp_tool_call",
                name=tool_name,
                timestamp=timestamp.isoformat(),
                duration_ms=duration_ms,
                success=success,
                error_type=error_type,
                package_version=self._sinks.package_version,
            )
            self._sinks.emit(record)

        return result
