# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP Server configuration classes and resolution logic.

This module provides configuration classes for MCP servers, including
credential resolution from HTTP headers and environment variables.

## Key Components

- `MCPServerConfigArg`: Dataclass for defining credential resolution configuration
- `MCPServerConfig`: Dataclass storing server configuration (attached to the app)
- `get_mcp_config`: Helper function to get credentials at runtime
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
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

    def get_config(self, name: str) -> str:
        """Get a configuration value by name.

        Resolution order:
        1. HTTP headers (case-insensitive)
        2. Environment variables
        3. Default value

        Args:
            name: The name of the config argument to get.

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


def get_mcp_config(ctx_or_app: Context | FastMCP, name: str) -> str:
    """Get a configuration value from an MCP server.

    This is a convenience function to get config values from a FastMCP
    app created with mcp_server(). It accepts either a Context object (preferred
    for use in MCP tools) or a FastMCP app instance directly.

    When using Context, the function accesses the app via ctx.fastmcp, which
    ensures session-aware resolution of HTTP headers.

    Args:
        ctx_or_app: Either a FastMCP Context object (from tool/resource functions)
            or a FastMCP application instance (created with mcp_server()).
        name: The name of the config argument to get.

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
            api_key = get_mcp_config(ctx, "api_key")
            ...
        ```
    """
    # Extract the FastMCP app from Context if needed
    app = ctx_or_app.fastmcp if isinstance(ctx_or_app, Context) else ctx_or_app

    config: MCPServerConfig = app.x_mcp_server_config  # type: ignore[attr-defined]
    return config.get_config(name)
