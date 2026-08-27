# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Shared telemetry primitives for MCP and CLI instrumentation.

This module provides the reusable core for telemetry recording and emission
that is shared between the MCP middleware (`_telemetry_middleware`) and the
CLI harness (`_cli`).

Three telemetry sinks, each independently toggled:

1. **Structured JSON log** - always on (Python `logging`, `INFO` level).
2. **Sentry breadcrumb** - enabled when a `sentry_dsn` is supplied.
3. **Segment analytics event** - enabled when a `segment_write_key` is supplied.

Set `DO_NOT_TRACK` to a non-empty value other than `0`, `false`, or `no` to
disable Sentry and Segment while keeping structured logs enabled.
"""

from __future__ import annotations

import importlib.metadata as md
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

import sentry_sdk
from segment import analytics as _segment_analytics

logger = logging.getLogger(__name__)


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
    extra: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict suitable for logging / analytics."""
        core = {
            "invocation_type": self.invocation_type,
            "name": self.name,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error_type": self.error_type,
            "package_version": self.package_version,
        }
        return core | {
            key: value for key, value in self.extra.items() if key not in core
        }


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Configuration for automatic MCP tool-call telemetry."""

    enabled: bool = True
    package_name: str | None = None
    sentry_dsn: str | None = None
    segment_write_key: str | None = None
    segment_user_id: str = "mcp-server"
    extra_properties: (
        Mapping[str, object] | Callable[[], Mapping[str, object]] | None
    ) = None
    known_public_mcp_domains: Sequence[str] = ()
    anonymization_salt: str | Callable[[], str | None] | None = None
    caller_ip_fallback: bool = False
    anonymized_attribution: bool = True


# ---------------------------------------------------------------------------
# Telemetry sinks
# ---------------------------------------------------------------------------


class TelemetrySinks:
    """Manages Sentry and Segment initialisation and event emission.

    Consumers create an instance with optional DSN / write key. Sinks whose
    key is `None` are skipped. Setting `DO_NOT_TRACK` disables Sentry and
    Segment initialization while leaving structured logging enabled.
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
        self.segment_enabled = False
        self._segment_user_id = segment_user_id
        if telemetry_opted_out():
            return

        if sentry_dsn is not None:
            _init_sentry(sentry_dsn, package_name)
            self.sentry_enabled = True

        # Segment
        if segment_write_key is not None:
            _init_segment(segment_write_key)
            self.segment_enabled = True

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


def resolve_extra_properties(
    spec: Mapping[str, object] | Callable[[], Mapping[str, object]] | None,
) -> Mapping[str, object]:
    """Resolve static or dynamic telemetry attribution properties."""
    if spec is None:
        return {}
    if isinstance(spec, Mapping):
        return spec
    provider = cast(Callable[[], Mapping[str, object]], spec)
    try:
        return provider()
    except Exception:
        # Telemetry must never break a tool call, so ignore provider failures.
        logger.debug("Failed to resolve telemetry extra properties", exc_info=True)
        return {}


def telemetry_opted_out() -> bool:
    """Return whether external telemetry sinks are disabled by the environment."""
    value = os.environ.get("DO_NOT_TRACK", "")
    return bool(value) and value.lower() not in {"0", "false", "no"}


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
