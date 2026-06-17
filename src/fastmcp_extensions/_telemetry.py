# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Shared telemetry primitives for MCP and CLI instrumentation.

This module provides the reusable core for telemetry recording and emission
that is shared between the MCP middleware (`_telemetry_middleware`) and the
CLI harness (`_cli`).

Three telemetry sinks, each independently toggled:

1. **Structured JSON log** - always on (Python `logging`, `INFO` level).
2. **Sentry breadcrumb** - enabled when a `sentry_dsn` is supplied.
3. **Segment analytics event** - enabled when a `segment_write_key` is supplied.
"""

from __future__ import annotations

import importlib.metadata as md
import logging
from dataclasses import dataclass

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
class TelemetryRecord:
    """Immutable record of a single instrumented invocation (MCP tool or CLI command)."""

    invocation_type: str
    name: str
    timestamp: str
    duration_ms: float
    success: bool
    error_type: str | None
    package_version: str

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict suitable for logging / analytics."""
        return {
            "invocation_type": self.invocation_type,
            "name": self.name,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error_type": self.error_type,
            "package_version": self.package_version,
        }


# ---------------------------------------------------------------------------
# Telemetry sinks
# ---------------------------------------------------------------------------


class TelemetrySinks:
    """Manages Sentry and Segment initialisation and event emission.

    Consumers create an instance with optional DSN / write key. Sinks that
    cannot be enabled (missing SDK or `None` key) are silently skipped.
    """

    def __init__(
        self,
        *,
        package_name: str | None = None,
        sentry_dsn: str | None = None,
        segment_write_key: str | None = None,
        segment_user_id: str = "mcp-server",
    ) -> None:
        """Initialise sinks.

        Args:
            package_name: Python distribution name whose version is stamped on
                every record.
            sentry_dsn: Sentry DSN string. Pass `None` to disable.
            segment_write_key: Segment write key. Pass `None` to disable.
            segment_user_id: The `user_id` stamped on Segment events.
        """
        self.package_version = resolve_version(package_name)

        # Sentry
        self.sentry_enabled = False
        if sentry_dsn is not None:
            if _HAS_SENTRY:
                _init_sentry(sentry_dsn, package_name)
                self.sentry_enabled = True
            else:
                logger.debug(
                    "sentry_dsn provided but sentry-sdk is not installed; "
                    "Sentry telemetry disabled"
                )

        # Segment
        self.segment_enabled = False
        self._segment_user_id = segment_user_id
        if segment_write_key is not None:
            if _HAS_SEGMENT:
                _init_segment(segment_write_key)
                self.segment_enabled = True
            else:
                logger.debug(
                    "segment_write_key provided but segment-analytics-python is not "
                    "installed; Segment telemetry disabled"
                )

    def emit(self, record: TelemetryRecord) -> None:
        """Dispatch a telemetry record to all enabled sinks."""
        emit_log(record)
        if self.sentry_enabled:
            emit_sentry_breadcrumb(record)
        if self.segment_enabled:
            self._emit_segment_event(record)

    def capture_exception(self, exc: BaseException) -> None:
        """Send an exception to Sentry (if enabled)."""
        if self.sentry_enabled:
            sentry_sdk.capture_exception(exc)

    def _emit_segment_event(self, record: TelemetryRecord) -> None:
        """Track the invocation as a Segment analytics event."""
        _segment_analytics.track(
            self._segment_user_id,
            record.invocation_type,
            record.to_dict(),
        )


# ---------------------------------------------------------------------------
# Standalone sink helpers
# ---------------------------------------------------------------------------


def emit_log(record: TelemetryRecord) -> None:
    """Emit a structured JSON log line."""
    logger.info(
        "%s: %s (%.1fms, %s)",
        record.invocation_type,
        record.name,
        record.duration_ms,
        "ok" if record.success else f"error={record.error_type}",
        extra={"telemetry": record.to_dict()},
    )


def emit_sentry_breadcrumb(record: TelemetryRecord) -> None:
    """Add a Sentry breadcrumb for this invocation."""
    sentry_sdk.add_breadcrumb(
        category=record.invocation_type,
        message=f"{record.name} -> {'ok' if record.success else record.error_type}",
        level="info" if record.success else "error",
        data=record.to_dict(),
    )


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------


def resolve_version(package_name: str | None) -> str:
    """Look up the installed version for `package_name`."""
    if package_name is None:
        return "unknown"
    try:
        return md.version(package_name)
    except md.PackageNotFoundError:
        return "unknown"


def _init_sentry(dsn: str, package_name: str | None) -> None:
    """Initialise Sentry if it has not already been initialised."""
    if sentry_sdk.is_initialized():
        return
    release = (
        f"{package_name}@{resolve_version(package_name)}" if package_name else None
    )
    sentry_sdk.init(
        dsn=dsn,
        release=release,
        traces_sample_rate=0.0,
        send_default_pii=False,
    )


def _init_segment(write_key: str) -> None:
    """Configure the Segment analytics client."""
    _segment_analytics.write_key = write_key
    _segment_analytics.send = True

    def _on_error(error: Exception, _items: object) -> None:
        logger.debug("Segment tracking error", exc_info=error)

    _segment_analytics.on_error = _on_error
