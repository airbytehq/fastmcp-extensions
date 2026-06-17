# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for the shared telemetry primitives."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from fastmcp_extensions._telemetry import (
    TelemetryRecord,
    TelemetrySinks,
    resolve_version,
)

# ---------------------------------------------------------------------------
# resolve_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "package_name,expected",
    [
        pytest.param(None, "unknown", id="none_returns_unknown"),
        pytest.param("not-a-real-package-xyz", "unknown", id="missing_returns_unknown"),
    ],
)
def test_resolve_version_edge_cases(package_name: str | None, expected: str) -> None:
    assert resolve_version(package_name) == expected


def test_resolve_version_installed_package() -> None:
    version = resolve_version("fastmcp-extensions")
    assert version != "unknown"
    assert "." in version


# ---------------------------------------------------------------------------
# TelemetryRecord
# ---------------------------------------------------------------------------


def test_telemetry_record_to_dict() -> None:
    record = TelemetryRecord(
        invocation_type="test_call",
        name="my_fn",
        timestamp="2025-01-01T00:00:00+00:00",
        duration_ms=10.5,
        success=True,
        error_type=None,
        package_version="0.1.0",
    )
    d = record.to_dict()
    assert d["invocation_type"] == "test_call"
    assert d["name"] == "my_fn"
    assert d["success"] is True


def test_telemetry_record_is_immutable() -> None:
    record = TelemetryRecord(
        invocation_type="x",
        name="y",
        timestamp="t",
        duration_ms=0,
        success=True,
        error_type=None,
        package_version="v",
    )
    with pytest.raises(AttributeError):
        record.name = "z"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TelemetrySinks
# ---------------------------------------------------------------------------


def test_sinks_defaults_no_sentry_no_segment() -> None:
    sinks = TelemetrySinks()
    assert sinks.sentry_enabled is False
    assert sinks.segment_enabled is False
    assert sinks.package_version == "unknown"


def test_sinks_with_sentry(caplog: pytest.LogCaptureFixture) -> None:
    with patch("fastmcp_extensions._telemetry.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        sinks = TelemetrySinks(sentry_dsn="https://fake@sentry.io/1")
    assert sinks.sentry_enabled is True


def test_sinks_emit_log(caplog: pytest.LogCaptureFixture) -> None:
    sinks = TelemetrySinks()
    record = TelemetryRecord(
        invocation_type="test",
        name="fn",
        timestamp="t",
        duration_ms=5.0,
        success=True,
        error_type=None,
        package_version="v",
    )
    with caplog.at_level(logging.INFO, logger="fastmcp_extensions._telemetry"):
        sinks.emit(record)
    assert "fn" in caplog.text


def test_sinks_capture_exception_calls_sentry() -> None:
    with patch("fastmcp_extensions._telemetry.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        sinks = TelemetrySinks(sentry_dsn="https://fake@sentry.io/1")
        exc = ValueError("test")
        sinks.capture_exception(exc)
        mock_sentry.capture_exception.assert_called_once_with(exc)


def test_sinks_capture_exception_noop_without_sentry() -> None:
    sinks = TelemetrySinks()
    sinks.capture_exception(ValueError("test"))  # should not raise
