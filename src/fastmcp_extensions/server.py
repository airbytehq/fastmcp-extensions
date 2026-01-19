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
"""

from __future__ import annotations

import importlib.metadata as md
import inspect
import pkgutil
import subprocess
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from fastmcp import FastMCP

from fastmcp_extensions._middleware import ToolFilterMiddleware
from fastmcp_extensions.server_config import (
    MCPServerConfig,
    MCPServerConfigArg,
)
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
    def server_info() -> dict[str, Any]:
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

        return info


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
    auto_discover_assets: bool | Callable[[], list[str]] = False,
    server_config_args: list[MCPServerConfigArg] | None = None,
    tool_filters: list[ToolFilterFn] | None = None,
    include_standard_tool_filters: bool = False,
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

    # Build the list of config args, including standard ones if requested
    all_config_args: list[MCPServerConfigArg] = list(server_config_args or [])
    if include_standard_tool_filters:
        all_config_args.extend(STANDARD_CONFIG_ARGS)

    config = MCPServerConfig(
        name=name,
        package_name=package_name,
        advertised_properties=advertised_properties or {},
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

    app.x_mcp_server_config = config  # type: ignore[attr-defined]

    # Build the list of tool filters, including standard ones if requested
    all_tool_filters: list[ToolFilterFn] = list(tool_filters or [])
    if include_standard_tool_filters:
        all_tool_filters.extend(STANDARD_TOOL_FILTERS)

    # Register tool filter middleware for each filter function
    for filter_fn in all_tool_filters:
        app.add_middleware(ToolFilterMiddleware(app, tool_filter=filter_fn))

    return app
