# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP Server factory with built-in server info and credential resolution.

This module provides a factory function to create FastMCP servers with common
patterns built-in, including server info resources and HTTP header credential
resolution.

Key Components
--------------
- `mcp_server`: Factory function to create a FastMCP instance with built-in features
- `MCPServerConfigArg`: Dataclass for defining credential resolution configuration
- `MCPServerConfig`: Dataclass storing server configuration (attached to the app)
- `resolve_config`: Helper function to resolve credentials at runtime

Basic Usage
-----------
Create a simple MCP server with server info resource::

    from fastmcp_extensions import mcp_server

    app = mcp_server(
        name="my-server",
        package_name="my-package",
    )

Credential Resolution
---------------------
Define credentials that resolve from HTTP headers, environment variables, or defaults::

    from fastmcp_extensions import mcp_server, MCPServerConfigArg, resolve_config

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

    # Later, resolve the credential (checks header -> env var -> default)
    api_key = resolve_config(app, "api_key")

MCP Module Auto-Discovery
-------------------------
Automatically discover sibling modules in your package::

    app = mcp_server(
        name="my-server",
        auto_discover_assets=True,  # Discovers non-private sibling modules
    )

See Also
--------
`MCPServerConfigArg` : Configuration for individual credentials
`mcp_server` : Main factory function
`resolve_config` : Runtime credential resolution
"""

from __future__ import annotations

import importlib.metadata as md
import inspect
import os
import pkgutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers


@dataclass
class MCPServerConfigArg:
    """Configuration argument for MCP server credential resolution.

    This class defines a configuration argument that can be resolved from
    HTTP headers or environment variables, with support for sensitive values.

    Attributes:
        name: Unique name for this config argument (used for resolution).
        http_header_key: HTTP header name to check first (case-insensitive). Optional.
        env_var: Environment variable name to check as fallback. Optional.
        default: Default value if not found. Can be a string or a callable returning a string.
        required: If True, resolution will raise an error if not found (after checking default).
        sensitive: If True, the value will be masked in logs/output.
        normalize_fn: Optional function to transform the resolved value. Useful for
            parsing values like "Bearer <token>" from Authorization headers.
            The function receives the raw value and returns the normalized value,
            or None if the value should be treated as not found (triggering fallback).
            The function may also raise an exception for invalid input validation.
            When raising exceptions, avoid including the raw value in error messages
            as it may contain sensitive credentials.
    """

    name: str
    http_header_key: str | None = None
    env_var: str | None = None
    default: str | Callable[[], str] | None = None
    required: bool = True
    sensitive: bool = False
    normalize_fn: Callable[[str], str | None] | None = None


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server created via mcp_server().

    This class stores the configuration passed to mcp_server() and provides
    methods for credential resolution.
    """

    name: str
    package_name: str | None = None
    advertised_properties: dict[str, Any] = field(default_factory=dict)
    config_args: list[MCPServerConfigArg] = field(default_factory=list)
    _config_args_by_name: dict[str, MCPServerConfigArg] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Build lookup dict for config args by name."""
        self._config_args_by_name = {arg.name: arg for arg in self.config_args}

    def resolve_config(self, name: str) -> str:
        """Resolve a configuration value by name.

        Resolution order:
        1. HTTP headers (case-insensitive)
        2. Environment variables

        Args:
            name: The name of the config argument to resolve.

        Returns:
            The resolved value as a string.

        Raises:
            KeyError: If the config argument name is not registered.
            ValueError: If the config is required but no value can be resolved.
        """
        if name not in self._config_args_by_name:
            raise KeyError(f"Unknown config argument: {name}")

        config_arg = self._config_args_by_name[name]
        return _resolve_config_arg(config_arg)


def _get_header_value(headers: dict[str, str], header_name: str) -> str | None:
    """Get a header value from a headers dict, case-insensitively.

    Args:
        headers: Dictionary of HTTP headers.
        header_name: The header name to look for (case-insensitive).

    Returns:
        The header value if found, None otherwise.
    """
    header_name_lower = header_name.lower()
    for key, value in headers.items():
        if key.lower() == header_name_lower:
            return value
    return None


def _resolve_config_arg(config_arg: MCPServerConfigArg) -> str:
    """Resolve a single config argument from headers or environment.

    Args:
        config_arg: The config argument to resolve.

    Returns:
        The resolved value as a string.

    Raises:
        ValueError: If the config is required but no value can be resolved.
    """

    def _apply_normalize(value: str) -> str | None:
        """Apply normalize_fn if configured, otherwise return value as-is."""
        if config_arg.normalize_fn is not None:
            return config_arg.normalize_fn(value)
        return value

    if config_arg.http_header_key:
        headers = get_http_headers()
        if headers:
            header_value = _get_header_value(headers, config_arg.http_header_key)
            if header_value:
                normalized = _apply_normalize(header_value)
                if normalized is not None:
                    return normalized

    if config_arg.env_var:
        env_value = os.environ.get(config_arg.env_var)
        if env_value:
            normalized = _apply_normalize(env_value)
            if normalized is not None:
                return normalized

    if config_arg.default is not None:
        if callable(config_arg.default):
            return config_arg.default()
        return config_arg.default

    if config_arg.required:
        sources: list[str] = []
        if config_arg.http_header_key:
            sources.append(f"HTTP header '{config_arg.http_header_key}'")
        if config_arg.env_var:
            sources.append(f"environment variable '{config_arg.env_var}'")
        source_str = " or ".join(sources) if sources else "no sources configured"
        raise ValueError(
            f"Required config '{config_arg.name}' not found. Set {source_str}."
        )

    return ""


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
    **fastmcp_kwargs: Any,
) -> FastMCP:
    """Create a FastMCP server with built-in server info and credential resolution.

    This factory function creates a FastMCP instance with common patterns
    built-in, including:
    - Automatic server info resource registration
    - HTTP header credential resolution
    - Optional MCP module auto-discovery

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
        **fastmcp_kwargs: Additional arguments passed to FastMCP constructor.

    Returns:
        A configured FastMCP instance with server info resource registered.

    Example:
        ```python
        from fastmcp_extensions import mcp_server, MCPServerConfigArg

        app = mcp_server(
            name="my-mcp-server",
            package_name="my-package",
            advertised_properties={
                "docs_url": "https://github.com/org/repo",
            },
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
    app = FastMCP(name, **fastmcp_kwargs)

    config = MCPServerConfig(
        name=name,
        package_name=package_name,
        advertised_properties=advertised_properties or {},
        config_args=server_config_args or [],
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

    return app


def resolve_config(ctx_or_app: Context | FastMCP, name: str) -> str:
    """Resolve a configuration value from an MCP server.

    This is a convenience function to resolve config values from a FastMCP
    app created with mcp_server(). It accepts either a Context object (preferred
    for use in MCP tools) or a FastMCP app instance directly.

    When using Context, the function accesses the app via ctx.fastmcp, which
    ensures session-aware resolution of HTTP headers.

    Args:
        ctx_or_app: Either a FastMCP Context object (from tool/resource functions)
            or a FastMCP application instance (created with mcp_server()).
        name: The name of the config argument to resolve.

    Returns:
        The resolved value as a string.

    Raises:
        AttributeError: If the app was not created with mcp_server().
        KeyError: If the config argument name is not registered.
        ValueError: If the config is required but no value can be resolved.

    Example:
        ```python
        @mcp_tool(...)
        def my_tool(ctx: Context, ...) -> str:
            api_key = resolve_config(ctx, "api_key")
            ...
        ```
    """
    # Extract the FastMCP app from Context if needed
    app = ctx_or_app.fastmcp if isinstance(ctx_or_app, Context) else ctx_or_app

    config: MCPServerConfig = app.x_mcp_server_config  # type: ignore[attr-defined]
    return config.resolve_config(name)
