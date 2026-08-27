# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Telemetry middleware for MCP tool call instrumentation.

Intercepts every `tools/call` invocation and records structured telemetry:

- `tool_name`, `timestamp`, `duration_ms`, `success`/`failure`, `error_type`
- `package_version` (when a `package_name` is provided)
- Optional attribution properties supplied through `extra_properties`

Three telemetry sinks, each independently toggled:

1. **Structured JSON log** - always on (Python `logging`, `INFO` level).
2. **Sentry breadcrumb** - enabled when a `sentry_dsn` is supplied.
3. **Segment analytics event** - enabled when a `segment_write_key` is supplied.

`mcp_server()` registers this middleware automatically from a `TelemetryConfig`;
see `fastmcp_extensions.server` for the user-facing configuration docs.
Registration goes through `register_tool_call_telemetry()`, which is idempotent:
it skips the app when an instance is already installed, so an app built with
`telemetry=False` can be instrumented later without duplicating log lines.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastmcp.server.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools import ToolResult

from fastmcp_extensions._attribution import _AnonymizedAttribution
from fastmcp_extensions._telemetry import (
    TelemetryConfig,
    TelemetryRecord,
    TelemetrySinks,
    resolve_extra_properties,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
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
    - `extra` - optional attribution properties

    Telemetry is emitted to up to three sinks:

    1. **Structured JSON log** at `INFO` level (always on).
    2. **Sentry breadcrumb** (`mcp_tool_call` category) when `sentry_dsn` is set.
    3. **Segment event** (`mcp_tool_call`) when `segment_write_key` is set.

    Example:

    ```python
    app = mcp_server(
        name="my-server",
        package_name="my-package",
        telemetry=TelemetryConfig(
            sentry_dsn="https://...@sentry.io/...",
            segment_write_key="hnWfMdE...",
            extra_properties={"is_hosted_mcp": True},
        ),
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
        extra_properties: (
            Mapping[str, object] | Callable[[], Mapping[str, object]] | None
        ) = None,
        plaintext_endpoint_domains: Sequence[str] = (),
        anonymization_salt_fallback: Callable[[], str | None] | None = None,
        caller_ip_fallback: bool = False,
        anonymized_attribution: bool = True,
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
        self._extra_properties = extra_properties
        self._attribution = (
            _AnonymizedAttribution(
                plaintext_endpoint_domains=plaintext_endpoint_domains,
                anonymization_salt_fallback=anonymization_salt_fallback,
                caller_ip_fallback=caller_ip_fallback,
            )
            if anonymized_attribution
            else None
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
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
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
                extra={
                    **resolve_extra_properties(self._attribution),
                    **resolve_extra_properties(self._extra_properties),
                },
            )
            self._sinks.emit(record)

        return result


def register_tool_call_telemetry(app: FastMCP, config: TelemetryConfig) -> None:
    """Register tool-call telemetry on `app` unless it is already present."""
    if not config.enabled or any(
        isinstance(middleware, ToolCallTelemetryMiddleware)
        for middleware in app.middleware
    ):
        return

    app.add_middleware(
        ToolCallTelemetryMiddleware(
            package_name=config.package_name,
            sentry_dsn=config.sentry_dsn,
            segment_write_key=config.segment_write_key,
            segment_user_id=config.segment_user_id,
            extra_properties=config.extra_properties,
            plaintext_endpoint_domains=config.plaintext_endpoint_domains,
            anonymization_salt_fallback=config.anonymization_salt_fallback,
            caller_ip_fallback=config.caller_ip_fallback,
            anonymized_attribution=config.anonymized_attribution,
        )
    )
