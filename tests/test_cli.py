# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for the CLI telemetry harness and output helpers."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from fastmcp_extensions.cli import (
    App,
    TelemetryApp,
    cli_app,
    exit_with_error,
    print_error,
    print_json,
    print_success,
    print_table,
    print_warning,
)

# ---------------------------------------------------------------------------
# App subclass
# ---------------------------------------------------------------------------


def test_app_disables_version_flags_by_default() -> None:
    a = App(name="test")
    assert list(a.version_flags) == []


def test_app_allows_explicit_version_flags() -> None:
    a = App(name="test", version="1.0.0", version_flags=["--version"])
    assert "--version" in a.version_flags


# ---------------------------------------------------------------------------
# cli_app factory
# ---------------------------------------------------------------------------


def test_cli_app_creates_telemetry_app() -> None:
    app = cli_app(name="test-cli")
    assert isinstance(app, TelemetryApp)
    assert "test-cli" in app.name


def test_cli_app_resolves_package_version() -> None:
    app = cli_app(name="test", package_name="fastmcp-extensions")
    assert app.version is not None
    assert app.version != "unknown"


def test_cli_app_unknown_package() -> None:
    app = cli_app(name="test", package_name="definitely-not-installed-pkg")
    assert app.version is None


def test_cli_app_with_help_and_urls() -> None:
    app = cli_app(
        name="test",
        help_text="My CLI tool.",
        docs_url="https://docs.example.com",
        repo_url="https://github.com/example/repo",
    )
    assert "My CLI tool." in app.help
    assert "docs.example.com" in app.help
    assert "github.com/example/repo" in app.help


# ---------------------------------------------------------------------------
# TelemetryApp command wrapping
# ---------------------------------------------------------------------------


def test_command_wraps_function_with_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = cli_app(name="test")

    @app.command
    def greet(name: str = "world") -> str:
        return f"Hello, {name}!"

    with caplog.at_level(
        logging.INFO, logger="fastmcp_extensions._telemetry"
    ), pytest.raises(SystemExit):
        app(["greet"])

    assert "greet" in caplog.text
    assert "cli_command" in caplog.text


def test_command_telemetry_records_failure(caplog: pytest.LogCaptureFixture) -> None:
    app = cli_app(name="test")

    @app.command
    def fail_cmd() -> None:
        raise RuntimeError("intentional")

    with caplog.at_level(
        logging.INFO, logger="fastmcp_extensions._telemetry"
    ), pytest.raises(RuntimeError, match="intentional"):
        app(["fail-cmd"])

    assert "error=RuntimeError" in caplog.text


def test_command_passes_through_sub_apps() -> None:
    """Sub-apps (App instances) should pass through without wrapping."""
    app = cli_app(name="root")
    sub = App(name="sub", help="Sub-command group.")
    app.command(sub)

    @sub.command
    def sub_cmd() -> None:
        pass

    with pytest.raises(SystemExit):
        app(["sub", "sub-cmd"])


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def test_print_json(capsys: pytest.CaptureFixture[str]) -> None:
    print_json({"key": "value"})
    captured = capsys.readouterr()
    assert '"key"' in captured.out
    assert '"value"' in captured.out


def test_print_error(capsys: pytest.CaptureFixture[str]) -> None:
    print_error("something broke")
    captured = capsys.readouterr()
    assert "something broke" in captured.err


def test_print_success(capsys: pytest.CaptureFixture[str]) -> None:
    print_success("it worked")
    captured = capsys.readouterr()
    assert "it worked" in captured.out


def test_print_warning(capsys: pytest.CaptureFixture[str]) -> None:
    print_warning("watch out")
    captured = capsys.readouterr()
    assert "watch out" in captured.out


def test_exit_with_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        exit_with_error("fatal", code=2)
    assert exc_info.value.code == 2


def test_print_table(capsys: pytest.CaptureFixture[str]) -> None:
    print_table("Test Table", ["Col A", "Col B"], [["1", "2"], ["3", "4"]])
    captured = capsys.readouterr()
    assert "Test Table" in captured.out


# ---------------------------------------------------------------------------
# Sentry capture_exception on CLI error
# ---------------------------------------------------------------------------


def test_cli_command_captures_sentry_exception() -> None:
    with patch("fastmcp_extensions._telemetry.sentry_sdk") as mock_sentry:
        mock_sentry.is_initialized.return_value = True
        app = cli_app(
            name="test",
            sentry_dsn="https://fake@sentry.io/1",
        )

        @app.command
        def boom() -> None:
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            app(["boom"])

        mock_sentry.capture_exception.assert_called_once()


# ---------------------------------------------------------------------------
# Segment event on CLI command
# ---------------------------------------------------------------------------


def test_cli_command_emits_segment_event() -> None:
    mock_analytics = MagicMock()
    with patch("fastmcp_extensions._telemetry._segment_analytics", mock_analytics):
        app = cli_app(
            name="test",
            segment_write_key="fake-key",
        )

        @app.command
        def ping() -> None:
            pass

        with pytest.raises(SystemExit):
            app(["ping"])

        mock_analytics.track.assert_called_once()
        args = mock_analytics.track.call_args
        assert args[0][0] == "cli-user"
        assert args[0][1] == "cli_command"
