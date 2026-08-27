# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for the shared telemetry primitives."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from unittest.mock import patch

import pytest

from fastmcp_extensions._telemetry import (
    TelemetryConfig,
    TelemetryRecord,
    TelemetrySinks,
    resolve_extra_properties,
    resolve_version,
    telemetry_opted_out,
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


def test_telemetry_record_includes_extra_without_overwriting_core_fields() -> None:
    record = TelemetryRecord(
        invocation_type="test_call",
        name="my_fn",
        timestamp="2025-01-01T00:00:00+00:00",
        duration_ms=10.5,
        success=True,
        error_type=None,
        package_version="0.1.0",
        extra={"name": "wrong", "custom_property": "value"},
    )

    assert record.to_dict() == {
        "invocation_type": "test_call",
        "name": "my_fn",
        "timestamp": "2025-01-01T00:00:00+00:00",
        "duration_ms": 10.5,
        "success": True,
        "error_type": None,
        "package_version": "0.1.0",
        "custom_property": "value",
    }


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
        record.name = "z"  # ty: ignore[invalid-assignment]  # The test intentionally mutates a frozen record to verify immutability.


# ---------------------------------------------------------------------------
# TelemetrySinks
# ---------------------------------------------------------------------------


def test_telemetry_config_is_frozen() -> None:
    config = TelemetryConfig()
    with pytest.raises(AttributeError):
        config.enabled = False  # ty: ignore[invalid-assignment]  # The test intentionally mutates a frozen config.


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param(None, False, id="unset"),
        pytest.param("", False, id="empty"),
        pytest.param("0", False, id="zero"),
        pytest.param("FALSE", False, id="false"),
        pytest.param("no", False, id="no"),
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param("yes", True, id="yes"),
    ],
)
def test_telemetry_opted_out(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    expected: bool,
) -> None:
    if value is None:
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    else:
        monkeypatch.setenv("DO_NOT_TRACK", value)

    assert telemetry_opted_out() is expected


@pytest.mark.parametrize(
    "spec,expected",
    [
        pytest.param(None, {}, id="none"),
        pytest.param({"source": "static"}, {"source": "static"}, id="static"),
        pytest.param(
            lambda: {"source": "callable"},
            {"source": "callable"},
            id="callable",
        ),
    ],
)
def test_resolve_extra_properties(
    spec: Mapping[str, object] | Callable[[], Mapping[str, object]] | None,
    expected: Mapping[str, object],
) -> None:
    resolved = resolve_extra_properties(spec)
    assert resolved == expected
    if isinstance(spec, Mapping):
        assert resolved is spec


def test_resolve_extra_properties_ignores_provider_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raise_error() -> dict[str, object]:
        raise RuntimeError("boom")

    with caplog.at_level(logging.DEBUG):
        assert resolve_extra_properties(raise_error) == {}
    assert "Failed to resolve telemetry extra properties" in caplog.text


def test_sinks_defaults_no_sentry_no_segment() -> None:
    sinks = TelemetrySinks()
    assert sinks.sentry_enabled is False
    assert sinks.segment_enabled is False
    assert sinks.package_version == "unknown"


def test_sinks_with_sentry(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
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


def test_sinks_capture_exception_calls_sentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    with patch("fastmcp_extensions._telemetry.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        sinks = TelemetrySinks(sentry_dsn="https://fake@sentry.io/1")
        exc = ValueError("test")
        sinks.capture_exception(exc)
        mock_sentry.capture_exception.assert_called_once_with(exc)


def test_sinks_capture_exception_noop_without_sentry() -> None:
    sinks = TelemetrySinks()
    sinks.capture_exception(ValueError("test"))  # should not raise


def test_sinks_do_not_initialize_external_sinks_when_opted_out(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    with patch("fastmcp_extensions._telemetry._init_sentry") as init_sentry, patch(
        "fastmcp_extensions._telemetry._init_segment"
    ) as init_segment:
        sinks = TelemetrySinks(
            sentry_dsn="https://fake@sentry.io/1",
            segment_write_key="fake-key",
        )

    assert sinks.sentry_enabled is False
    assert sinks.segment_enabled is False
    init_sentry.assert_not_called()
    init_segment.assert_not_called()

    record = TelemetryRecord(
        invocation_type="test",
        name="fn",
        timestamp="t",
        duration_ms=5.0,
        success=True,
        error_type=None,
        package_version="v",
    )
    with caplog.at_level(logging.INFO):
        sinks.emit(record)
    assert "fn" in caplog.text
