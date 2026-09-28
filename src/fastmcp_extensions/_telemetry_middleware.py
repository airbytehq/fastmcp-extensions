# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Telemetry middleware for MCP tool call instrumentation.

Intercepts every `tools/call` invocation and records structured telemetry:

- `tool_name`, `timestamp`, `duration_ms`, `success`/`failure`, `error_type`
- `tool_group` (the tool's `mcp_module`) and `mutation_class`
  (`read` / `mutate` / `destructive` / `unknown`)
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

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools import ToolResult
from fastmcp.utilities.versions import VersionSpec

from fastmcp_extensions._attribution import _AnonymizedAttribution
from fastmcp_extensions._telemetry import (
    DEFAULT_SEGMENT_USER_ID,
    TelemetryConfig,
    TelemetryRecord,
    TelemetrySinks,
    resolve_extra_properties,
)
from fastmcp_extensions.tool_traits import MutationClass, get_tool_traits

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp import types as mt

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
ToolCallTelemetryRecord = TelemetryRecord


def tool_telemetry_properties(app: FastMCP, tool_name: str) -> dict[str, str | None]:
    """Return the `tool_group` and `mutation_class` recorded for a tool.

    Reads the in-process traits registry. `register_mcp_tools` records both
    values and `mcp_provider` records only the group, since a provider tool's
    hints can differ per version. Other tools report a `None` group and an
    `unknown` mutation class; the telemetry middleware classifies those from
    the tool each call resolves to.
    """
    traits = get_tool_traits(app, tool_name)
    return {
        "tool_group": traits.mcp_module,
        "mutation_class": (traits.mutation_class or MutationClass.UNKNOWN).value,
    }


def _requested_version(meta: Mapping[str, object] | None) -> VersionSpec | None:
    """Return the tool version a `tools/call` requested through `_meta.fastmcp`."""
    fastmcp_meta = (meta or {}).get("fastmcp")
    if not isinstance(fastmcp_meta, Mapping):
        return None
    version = fastmcp_meta.get("version")
    if isinstance(version, str):
        return VersionSpec(eq=version)
    if isinstance(version, Mapping):
        bounds = (version.get("gte"), version.get("lt"), version.get("eq"))
        if all(bound is None or isinstance(bound, str) for bound in bounds):
            gte, lt, eq = bounds
            return VersionSpec(gte=gte, lt=lt, eq=eq)
    return None


async def _resolve_tool_properties(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> dict[str, str | None]:
    """Resolve tool properties for one invocation.

    Tools without a registration-time class are classified from the tool this
    call resolves to, so concurrent calls to different versions of a tool
    never share a result.
    """
    properties: dict[str, str | None] = {
        "tool_group": None,
        "mutation_class": MutationClass.UNKNOWN.value,
    }
    try:
        if context.fastmcp_context is None:
            return properties
        app = context.fastmcp_context.fastmcp
        tool_name = context.message.name
        properties = tool_telemetry_properties(app, tool_name)
        if get_tool_traits(app, tool_name).mutation_class is None:
            tool = await app.get_tool(
                tool_name, version=_requested_version(context.message.meta)
            )
            if tool is not None:
                properties["mutation_class"] = MutationClass.from_annotations(
                    tool.annotations
                ).value
    except Exception:
        # Telemetry must never break a tool call.
        logger.debug("Failed to resolve tool telemetry properties", exc_info=True)
    return properties


class ToolCallTelemetryMiddleware(Middleware):
    """Middleware that records telemetry for every MCP tool invocation.

    Captured fields per call:

    - `tool_name` - the MCP tool that was invoked
    - `timestamp` - ISO-8601 UTC timestamp of the call start
    - `duration_ms` - wall-clock execution time in milliseconds
    - `success` - whether the call completed without raising
    - `error_type` - the exception class name on failure (`None` on success)
    - `tool_group` - the tool's `mcp_module` (`None` when not registered
      through fastmcp-extensions)
    - `mutation_class` - `read`, `mutate`, `destructive`, or `unknown`,
      from the tool's `readOnlyHint` / `destructiveHint`
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
        segment_user_id: str | Callable[[], str | None] = DEFAULT_SEGMENT_USER_ID,
        segment_anonymous_id: str | Callable[[], str | None] | None = None,
        extra_properties: (
            Mapping[str, object] | Callable[[], Mapping[str, object]] | None
        ) = None,
        known_public_mcp_domains: Sequence[str] = (),
        anonymization_salt: str | Callable[[], str | None] | None = None,
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
            segment_anonymous_id=segment_anonymous_id,
        )
        self._extra_properties = extra_properties
        self._attribution = (
            _AnonymizedAttribution(
                known_public_mcp_domains=known_public_mcp_domains,
                anonymization_salt=anonymization_salt,
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
        tool_properties = await _resolve_tool_properties(context)
        start = time.monotonic()

        success = True
        error_type: str | None = None

        try:
            result = await call_next(context)
        except Exception as exc:
            success = False
            # FastMCP 3.4+ wraps tool exceptions in `ToolError`; report the cause
            # so telemetry records the real failure type, not the wrapper.
            telemetry_error = (
                exc.__cause__ if isinstance(exc, ToolError) and exc.__cause__ else exc
            )
            error_type = type(telemetry_error).__name__
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
                    **tool_properties,
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
            segment_anonymous_id=config.segment_anonymous_id,
            extra_properties=config.extra_properties,
            known_public_mcp_domains=config.known_public_mcp_domains,
            anonymization_salt=config.anonymization_salt,
            anonymized_attribution=config.anonymized_attribution,
        )
    )
