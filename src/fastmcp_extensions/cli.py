# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Reusable CLI scaffolding with built-in telemetry.

Provides a `cli_app` factory that creates a Cyclopts `App` with automatic
Sentry / Segment telemetry on every command invocation, plus a set of
Rich-based output helpers.

Usage:

```python
from fastmcp_extensions.cli import cli_app

app = cli_app(
    name="my-tool",
    package_name="my-package",
    sentry_dsn="https://...@sentry.io/...",
    segment_write_key="hnWfMdE...",
)

@app.command
def greet(name: str) -> None:
    print_success(f"Hello, {name}!")
```
"""

from __future__ import annotations

import functools
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, NoReturn

from cyclopts import App as _CycloptsApp

from fastmcp_extensions._telemetry import (
    TelemetryRecord,
    TelemetrySinks,
    resolve_version,
)

# ---------------------------------------------------------------------------
# Rich console helpers
# ---------------------------------------------------------------------------

from rich.console import Console
from rich.table import Table

_console: Console | None = None
_error_console: Console | None = None


def _get_console() -> Console:
    global _console
    if _console is None:
        _console = Console()
    return _console


def _get_error_console() -> Console:
    global _error_console
    if _error_console is None:
        _error_console = Console(stderr=True)
    return _error_console


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def print_json(data: Any) -> None:
    """Print data as formatted JSON to stdout."""
    _get_console().print_json(json.dumps(data, indent=2, default=str))


def print_error(message: str) -> None:
    """Print an error message to stderr."""
    _get_error_console().print(f"[red]Error:[/red] {message}")


def print_success(message: str) -> None:
    """Print a success message to stdout."""
    _get_console().print(f"[green]{message}[/green]")


def print_warning(message: str) -> None:
    """Print a warning message to stdout."""
    _get_console().print(f"[yellow]Warning:[/yellow] {message}")


def print_table(
    title: str,
    columns: list[str],
    rows: list[list[str]],
) -> None:
    """Print data as a formatted Rich table."""
    table = Table(title=title)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*row)
    _get_console().print(table)


def exit_with_error(message: str, code: int = 1) -> NoReturn:
    """Print an error message and exit with the given code."""
    print_error(message)
    sys.exit(code)


# ---------------------------------------------------------------------------
# App subclass
# ---------------------------------------------------------------------------


class App(_CycloptsApp):
    """Cyclopts `App` subclass that disables the default `--version` meta-flag.

    Cyclopts registers `--version` on every `App` by default. When a
    subcommand also accepts `--version` as a regular parameter (e.g.
    `publish --version 1.2.3`), the meta-command intercepts the token
    first and swallows the real command. This subclass sets
    `version_flags` to an empty list to avoid that conflict.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("version_flags", [])
        super().__init__(*args, **kwargs)


# ---------------------------------------------------------------------------
# Telemetry-aware command wrapper
# ---------------------------------------------------------------------------


def _wrap_command_with_telemetry(
    fn: Callable[..., Any],
    sinks: TelemetrySinks,
) -> Callable[..., Any]:
    """Return a wrapper that records telemetry around `fn`."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        timestamp = datetime.now(tz=timezone.utc)
        start = time.monotonic()
        success = True
        error_type: str | None = None

        try:
            return fn(*args, **kwargs)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            success = False
            error_type = type(exc).__name__
            sinks.capture_exception(exc)
            raise
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            record = TelemetryRecord(
                invocation_type="cli_command",
                name=fn.__name__,
                timestamp=timestamp.isoformat(),
                duration_ms=duration_ms,
                success=success,
                error_type=error_type,
                package_version=sinks.package_version,
            )
            sinks.emit(record)

    return wrapper


# ---------------------------------------------------------------------------
# Telemetry-enabled App
# ---------------------------------------------------------------------------


class TelemetryApp(App):
    """Cyclopts `App` with automatic telemetry on every registered command.

    Commands registered via `@app.command` are automatically wrapped with
    telemetry collection. The same three sinks as the MCP middleware are
    used: structured JSON log, Sentry breadcrumb, and Segment event.
    """

    def __init__(
        self,
        *args: Any,
        sinks: TelemetrySinks | None = None,
        **kwargs: Any,
    ) -> None:
        self._sinks = sinks
        super().__init__(*args, **kwargs)

    def command(
        self,
        fn: Callable[..., Any] | _CycloptsApp | None = None,
        /,
        **kwargs: Any,
    ) -> Any:
        """Register a command, wrapping it with telemetry if sinks are set."""
        if (
            self._sinks is not None
            and callable(fn)
            and not isinstance(fn, _CycloptsApp)
        ):
            fn = _wrap_command_with_telemetry(fn, self._sinks)
        return super().command(fn, **kwargs)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def cli_app(
    name: str,
    *,
    help_text: str | None = None,
    package_name: str | None = None,
    sentry_dsn: str | None = None,
    segment_write_key: str | None = None,
    segment_user_id: str = "cli-user",
    docs_url: str | None = None,
    repo_url: str | None = None,
) -> TelemetryApp:
    """Create a Cyclopts `App` with built-in telemetry.

    This is the CLI counterpart of `mcp_server()`. Every command registered
    on the returned app is automatically instrumented with the same
    Sentry / Segment / structured-log sinks used by `ToolCallTelemetryMiddleware`.

    ```python
    app = cli_app(
        name="airbyte-ops",
        package_name="airbyte-internal-ops",
        sentry_dsn="https://...@sentry.io/...",
        segment_write_key="hnWfMdE...",
    )
    ```
    """
    sinks = TelemetrySinks(
        package_name=package_name,
        sentry_dsn=sentry_dsn,
        segment_write_key=segment_write_key,
        segment_user_id=segment_user_id,
    )

    version = resolve_version(package_name)

    help_parts: list[str] = []
    if help_text:
        help_parts.append(help_text)
    if docs_url:
        help_parts.append(f"Documentation: {docs_url}")
    if repo_url:
        help_parts.append(f"Repository:    {repo_url}")

    return TelemetryApp(
        name=name,
        help="\n".join(help_parts) if help_parts else None,
        version=version if version != "unknown" else None,
        version_flags=["--version"] if version != "unknown" else [],
        sinks=sinks,
    )
