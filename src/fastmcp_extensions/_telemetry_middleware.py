# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Telemetry middleware for MCP tool call instrumentation.

Intercepts every `tools/call` invocation and records structured telemetry:

- `tool_name`, `timestamp`, `duration_ms`, `success`/`failure`, `error_type`
- `package_version` (when a `package_name` is provided)

Three telemetry sinks, each independently toggled:

1. **Structured JSON log** -- always on (Python `logging`, `INFO` level).
2. **Sentry breadcrumb** -- enabled when a `sentry_dsn` is supplied.
   Requires `sentry-sdk` (install via `pip install fastmcp-extensions[telemetry]`).
3. **Segment analytics event** -- enabled when a `segment_write_key` is supplied.
   Requires `analytics-python` (install via `pip install fastmcp-extensions[telemetry]`).

Usage:

```python
from fastmcp_extensions import mcp_server
from fastmcp_extensions._telemetry_middleware import ToolCallTelemetryMiddleware

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

import importlib.metadata as md
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

if TYPE_CHECKING:
    from mcp import types as mt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency imports (guarded)
# ---------------------------------------------------------------------------

try:
    import sentry_sdk

    _HAS_SENTRY = True
except ImportError:  # pragma: no cover
    sentry_sdk = None  # type: ignore[assignment]
    _HAS_SENTRY = False

try:
    from segment import analytics as _segment_analytics  # type: ignore[import-untyped]

    _HAS_SEGMENT = True
except ImportError:  # pragma: no cover
    _segment_analytics = None  # type: ignore[assignment]
    _HAS_SEGMENT = False


# ---------------------------------------------------------------------------
# Telemetry record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallTelemetryRecord:
    """Immutable record of a single MCP tool invocation."""

    tool_name: str
    timestamp: str
    duration_ms: float
    success: bool
    error_type: str | None
    package_version: str

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict suitable for logging / analytics."""
        return {
            "tool_name": self.tool_name,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error_type": self.error_type,
            "package_version": self.package_version,
        }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ToolCallTelemetryMiddleware(Middleware):
    """Middleware that records telemetry for every MCP tool invocation.

    Captured fields per call:

    - `tool_name` -- the MCP tool that was invoked
    - `timestamp` -- ISO-8601 UTC timestamp of the call start
    - `duration_ms` -- wall-clock execution time in milliseconds
    - `success` -- whether the call completed without raising
    - `error_type` -- the exception class name on failure (`None` on success)
    - `package_version` -- the installed version of `package_name`

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

        Sentry and Segment sinks are configured here -- if the corresponding
        SDK is not installed the sink is silently skipped and a debug log is
        emitted.

        Args:
            package_name: Python distribution name whose version is stamped on
                every record. Pass `None` to omit the field (defaults to
                `"unknown"`).
            sentry_dsn: Sentry DSN string. When provided **and** `sentry-sdk`
                is installed, the middleware initialises Sentry (if not already
                done) and adds a breadcrumb per tool call. Pass `None` to
                disable.
            segment_write_key: Segment write key. When provided **and**
                `analytics-python` is installed, the middleware configures
                Segment analytics and emits a `mcp_tool_call` event per tool
                call. Pass `None` to disable.
            segment_user_id: The `user_id` stamped on Segment events.
                Defaults to `"mcp-server"`.
        """
        # Version cache
        self._package_version = self._resolve_version(package_name)

        # Sentry setup
        self._sentry_enabled = False
        if sentry_dsn is not None:
            if _HAS_SENTRY:
                self._init_sentry(sentry_dsn, package_name)
                self._sentry_enabled = True
            else:
                logger.debug(
                    "sentry_dsn provided but sentry-sdk is not installed; "
                    "Sentry telemetry disabled"
                )

        # Segment setup
        self._segment_enabled = False
        self._segment_user_id = segment_user_id
        if segment_write_key is not None:
            if _HAS_SEGMENT:
                self._init_segment(segment_write_key)
                self._segment_enabled = True
            else:
                logger.debug(
                    "segment_write_key provided but analytics-python is not "
                    "installed; Segment telemetry disabled"
                )

    # -- hook ---------------------------------------------------------------

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
            record = ToolCallTelemetryRecord(
                tool_name=tool_name,
                timestamp=timestamp.isoformat(),
                duration_ms=duration_ms,
                success=success,
                error_type=error_type,
                package_version=self._package_version,
            )
            self._emit(record)

        return result

    # -- emit to sinks ------------------------------------------------------

    def _emit(self, record: ToolCallTelemetryRecord) -> None:
        """Dispatch a telemetry record to all enabled sinks."""
        self._emit_log(record)
        if self._sentry_enabled:
            self._emit_sentry_breadcrumb(record)
        if self._segment_enabled:
            self._emit_segment_event(record)

    @staticmethod
    def _emit_log(record: ToolCallTelemetryRecord) -> None:
        """Emit a structured JSON log line."""
        logger.info(
            "MCP tool call: %s (%.1fms, %s)",
            record.tool_name,
            record.duration_ms,
            "ok" if record.success else f"error={record.error_type}",
            extra={"mcp_tool_call": record.to_dict()},
        )

    @staticmethod
    def _emit_sentry_breadcrumb(record: ToolCallTelemetryRecord) -> None:
        """Add a Sentry breadcrumb for this tool call."""
        sentry_sdk.add_breadcrumb(
            category="mcp.tool_call",
            message=(
                f"{record.tool_name} -> {'ok' if record.success else record.error_type}"
            ),
            level="info" if record.success else "error",
            data=record.to_dict(),
        )

    def _emit_segment_event(self, record: ToolCallTelemetryRecord) -> None:
        """Track the tool call as a Segment analytics event."""
        _segment_analytics.track(
            self._segment_user_id,
            "mcp_tool_call",
            record.to_dict(),
        )

    # -- init helpers -------------------------------------------------------

    @staticmethod
    def _resolve_version(package_name: str | None) -> str:
        """Look up the installed version for `package_name`."""
        if package_name is None:
            return "unknown"
        try:
            return md.version(package_name)
        except md.PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _init_sentry(dsn: str, package_name: str | None) -> None:
        """Initialise Sentry if it has not already been initialised."""
        if sentry_sdk.is_initialized():
            return
        release = f"{package_name}@{md.version(package_name)}" if package_name else None
        sentry_sdk.init(
            dsn=dsn,
            release=release,
            traces_sample_rate=0.0,
            send_default_pii=False,
        )

    @staticmethod
    def _init_segment(write_key: str) -> None:
        """Configure the Segment analytics client."""
        _segment_analytics.write_key = write_key
        _segment_analytics.send = True

        def _on_error(error: Exception, _items: object) -> None:
            logger.debug("Segment tracking error", exc_info=error)

        _segment_analytics.on_error = _on_error
