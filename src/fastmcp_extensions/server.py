# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP Server factory with built-in server info and credential resolution.

This module provides a factory function to create FastMCP servers with common
patterns built-in, including server info resources and HTTP header credential
resolution.

## Key Components

- `mcp_server`: Factory function to create a FastMCP instance with built-in features
- `MCPServerConfigArg`: Dataclass for defining credential resolution configuration
- `MCPServerConfig`: Dataclass storing server configuration (attached to the app)
- `get_mcp_config`: Helper function to get credentials at runtime

## Basic Usage

Create a simple MCP server with server info resource:

```py
from fastmcp_extensions import mcp_server

app = mcp_server(
    name="my-server",
    package_name="my-package",
)
```

## Credential Resolution

Define credentials that resolve from HTTP headers, environment variables, or defaults:

```py
from fastmcp_extensions import mcp_server, MCPServerConfigArg, get_mcp_config

app = mcp_server(
    name="my-server",
    server_config_args=[
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            env_var="MY_API_KEY",
            default="fallback-value",
        ),
    ],
)

# Later, get the credential (checks header -> env var -> default)
api_key = get_mcp_config(app, "api_key")
```

## MCP Module Auto-Discovery

Automatically discover sibling modules in your package:

```py
app = mcp_server(
    name="my-server",
    auto_discover_assets=True,  # Discovers non-private sibling modules
)
```

## Tool-Call Telemetry

Telemetry is enabled by default and emits a structured log line for every MCP
tool invocation, recording the tool name, timestamp, duration, success or
failure, error type, and package version. Tool arguments and results are never
captured. Segment and Sentry stay disabled unless a write key or DSN is
supplied:

```py
from fastmcp_extensions import TelemetryConfig, mcp_server

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

`extra_properties` accepts a mapping or a zero-argument callable, which is
re-evaluated per tool call for values that are only known at runtime.
Anonymized attribution is enabled by default; configure
`known_public_mcp_domains` and `anonymization_salt` on
`TelemetryConfig` for deployment-specific behavior. Caller attribution uses
verified token subjects or OAuth client IDs; IPs are never read.

Set `telemetry=False` (or `TelemetryConfig(enabled=False)`) to opt out. Servers
built with `telemetry=False` can register later via
`register_tool_call_telemetry(app, config)`. That registration helper is
idempotent, while calling
`app.add_middleware(ToolCallTelemetryMiddleware(...))` directly on top of an
automatically instrumented app yields two instances and duplicate log lines.
"""

from __future__ import annotations

import importlib.metadata as md
import inspect
import json
import pkgutil
import subprocess
from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache
from typing import Any

from fastmcp import FastMCP

from fastmcp_extensions._middleware import ToolFilterMiddleware
from fastmcp_extensions._telemetry import TelemetryConfig
from fastmcp_extensions._telemetry_middleware import register_tool_call_telemetry
from fastmcp_extensions.server_config import (
    MCPServerConfig,
    MCPServerConfigArg,
)
from fastmcp_extensions.session_state import EncodedSessionStateConfig
from fastmcp_extensions.tool_filters import ToolFilterFn


@lru_cache(maxsize=1)
def _get_git_sha() -> str | None:
    """Get the current git SHA (short form).

    Returns:
        The short git SHA, or None if not in a git repository.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _get_fastmcp_version() -> str | None:
    """Get the installed FastMCP version.

    Returns:
        The FastMCP version string, or None if not installed.
    """
    try:
        return md.version("fastmcp")
    except md.PackageNotFoundError:
        return None


def _get_package_version(package_name: str) -> str:
    """Get the version of a package.

    Args:
        package_name: The name of the package.

    Returns:
        The package version, or "0.0.0+dev" if not found.
    """
    try:
        return md.version(package_name)
    except md.PackageNotFoundError:
        return "0.0.0+dev"


def _create_server_info_resource(
    app: FastMCP,
    config: MCPServerConfig,
) -> None:
    """Register the server info resource with the FastMCP app.

    Args:
        app: The FastMCP application instance.
        config: The server configuration.
    """
    server_name = config.name

    @app.resource(
        f"{server_name}://server/info",
        description=f"Server information for the {server_name} MCP server",
        mime_type="application/json",
    )
    def server_info() -> str:
        """Get server information including version, git SHA, and advertised properties."""
        info: dict[str, Any] = {
            "name": server_name,
            "fastmcp_version": _get_fastmcp_version(),
            "git_sha": _get_git_sha(),
        }

        if config.package_name:
            info["package_name"] = config.package_name
            info["version"] = _get_package_version(config.package_name)

        for key, value in config.advertised_properties.items():
            info[key] = value

        if config.server_info_provider is not None:
            provider_info = config.server_info_provider()
            if not isinstance(provider_info, dict):
                raise TypeError(
                    "server_info_provider must return a dict, "
                    f"got {type(provider_info).__name__}"
                )
            info.update(provider_info)

        return json.dumps(info)


def _discover_mcp_module_names() -> list[str]:
    """Auto-discover MCP module names from sibling non-private modules.

    This function inspects the calling package's structure to find non-private
    modules that could contain MCP assets (tools, resources, prompts).

    The discovery walks up the call stack to find the first frame that is not
    in this module, then discovers all non-private submodules of that package.

    Returns:
        List of discovered MCP module names (excluding private modules starting with '_').
    """
    # Get the caller's frame (skip this function and mcp_server)
    frame = inspect.currentframe()
    if frame is None:
        return []

    caller_frame = frame.f_back
    if caller_frame is None:
        return []

    # Walk up the stack to find a frame outside this module
    while caller_frame is not None:
        caller_module = caller_frame.f_globals.get("__name__", "")
        if caller_module != __name__:
            break
        caller_frame = caller_frame.f_back

    if caller_frame is None:
        return []

    caller_module = caller_frame.f_globals.get("__name__", "")
    if not caller_module:
        return []

    # Get the package name (parent of the module)
    package_name = (
        caller_module.rsplit(".", 1)[0] if "." in caller_module else caller_module
    )

    # Try to get the package path
    try:
        package = __import__(package_name, fromlist=[""])
        package_path = getattr(package, "__path__", None)
        if package_path is None:
            return []
    except ImportError:
        return []

    # Discover all non-private submodules
    module_names: list[str] = []
    for module_info in pkgutil.iter_modules(package_path):
        if not module_info.name.startswith("_"):
            module_names.append(module_info.name)

    return sorted(module_names)


def mcp_server(
    name: str,
    *,
    package_name: str | None = None,
    advertised_properties: dict[str, Any] | None = None,
    server_info_provider: Callable[[], dict[str, Any]] | None = None,
    auto_discover_assets: bool | Callable[[], list[str]] = False,
    server_config_args: list[MCPServerConfigArg] | None = None,
    tool_filters: list[ToolFilterFn] | None = None,
    include_standard_tool_filters: bool = False,
    encoded_session_state: EncodedSessionStateConfig | None = None,
    telemetry: TelemetryConfig | bool = True,
    **fastmcp_kwargs: Any,
) -> FastMCP:
    """Create a FastMCP server with built-in server info and credential resolution.

    This factory function creates a FastMCP instance with common patterns
    built-in, including:
    - Automatic server info resource registration
    - HTTP header credential resolution
    - Optional MCP module auto-discovery
    - Per-request tool filtering via middleware
    - Optional standard tool filters (readonly mode, safe mode)

    Args:
        name: The name of the MCP server.
        package_name: The Python package name (enables version detection in server info).
        advertised_properties: Custom properties to include in server info.
            Common properties include:
            - docs_url: URL to documentation
            - release_history_url: URL to release history
        server_info_provider: Optional callable that returns a dictionary of
            request-specific properties to include in server info.
        auto_discover_assets: If True, auto-detect MCP modules from sibling modules.
            Can also be a callable that returns a list of MCP module names.
        server_config_args: List of MCPServerConfigArg for credential resolution.
        tool_filters: List of tool filter functions for per-request tool filtering.
            Each filter function takes (Tool, FastMCP) and returns True to show
            the tool, False to hide it. Filters can use get_mcp_config() to access
            request-specific configuration values from HTTP headers or env vars.
        include_standard_tool_filters: If True, automatically add standard config args
            and tool filters for readonly_mode and safe_mode. These filters use
            tool annotations (readOnlyHint, destructiveHint) to control visibility.
        encoded_session_state: Server-level policy for encoded state handles.
        telemetry: Tool-call telemetry configuration. Defaults to structured
            log-only telemetry. Set to False or use
            `TelemetryConfig(enabled=False)` to disable the middleware.
        **fastmcp_kwargs: Additional arguments passed to FastMCP constructor.

    Returns:
        A configured FastMCP instance with server info resource registered.

    Example:
        ```python
        # Simple usage with standard tool filters
        app = mcp_server(
            name="my-server",
            include_standard_tool_filters=True,
        )

        # Custom usage with additional config args
        from fastmcp_extensions import mcp_server, MCPServerConfigArg

        app = mcp_server(
            name="my-mcp-server",
            package_name="my-package",
            include_standard_tool_filters=True,
            server_config_args=[
                MCPServerConfigArg(
                    name="api_key",
                    http_header_key="X-API-Key",
                    env_var="MY_API_KEY",
                    required=True,
                    sensitive=True,
                ),
            ],
        )
        ```
    """
    # Late import to avoid circular dependency
    # (tool_filters imports MCPServerConfigArg and get_mcp_config from this module)
    from fastmcp_extensions.tool_filters import (
        STANDARD_CONFIG_ARGS,
        STANDARD_TOOL_FILTERS,
    )

    app = FastMCP(name, **fastmcp_kwargs)

    # Build the list of config args, including standard ones if requested.
    # Host-supplied args take precedence over standard args of the same name, so
    # a host can back a standard config (e.g. `trusted_execution`) with its own
    # env var by supplying a replacement `MCPServerConfigArg`.
    all_config_args: list[MCPServerConfigArg] = list(server_config_args or [])
    if include_standard_tool_filters:
        provided_names = {arg.name for arg in all_config_args}
        all_config_args.extend(
            arg for arg in STANDARD_CONFIG_ARGS if arg.name not in provided_names
        )

    config = MCPServerConfig(
        name=name,
        package_name=package_name,
        advertised_properties=advertised_properties or {},
        server_info_provider=server_info_provider,
        config_args=all_config_args,
    )

    _create_server_info_resource(app, config)

    if auto_discover_assets:
        if callable(auto_discover_assets):
            mcp_modules = auto_discover_assets()
        else:
            mcp_modules = _discover_mcp_module_names()

        if mcp_modules:
            config.advertised_properties["mcp_modules"] = mcp_modules

    app.x_mcp_server_config = config  # ty: ignore[unresolved-attribute]  # FastMCP does not declare extension configuration attributes.
    if encoded_session_state is not None:
        app.x_mcp_extensions_session_state = encoded_session_state  # ty: ignore[unresolved-attribute]  # FastMCP does not declare extension configuration attributes.

    telemetry_config: TelemetryConfig | None
    if telemetry is True:
        telemetry_config = TelemetryConfig()
    elif telemetry is False:
        telemetry_config = None
    else:
        telemetry_config = telemetry

    if telemetry_config is not None and telemetry_config.enabled:
        if telemetry_config.package_name is None:
            telemetry_config = replace(telemetry_config, package_name=package_name)
        register_tool_call_telemetry(app, telemetry_config)

    # Build the list of tool filters, including standard ones if requested
    all_tool_filters: list[ToolFilterFn] = list(tool_filters or [])
    if include_standard_tool_filters:
        all_tool_filters.extend(STANDARD_TOOL_FILTERS)

    # Register tool filter middleware for each filter function
    for filter_fn in all_tool_filters:
        app.add_middleware(ToolFilterMiddleware(app, tool_filter=filter_fn))

    return app
