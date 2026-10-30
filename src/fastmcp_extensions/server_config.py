# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP Server configuration classes and resolution logic.

This module provides configuration classes for MCP servers, including
credential resolution from HTTP headers, URL query parameters, and environment variables.

## Key Components

- `MCPServerConfigArg`: Dataclass for defining credential resolution configuration
- `MCPServerConfig`: Dataclass storing server configuration (attached to the app)
- `get_mcp_config`: Helper function to get credentials at runtime
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers, get_http_request

from fastmcp_extensions.tool_traits import Capability, CapabilityResolver


@dataclass
class MCPServerConfigArg:
    """Configuration argument for MCP server credential resolution.

    This class defines a configuration argument that can be resolved from
    HTTP headers, URL query parameters, or environment variables, with support for sensitive values.

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
        url_param_key: Query param name on the HTTP request URL, matched case-insensitively.
            Checked after the header and before the env var. Optional.
        disallowed_url_param_keys: Case-insensitive URL query parameter names that return
            their mapped error message. Requires `url_param_key`.
        url_param_allowed_values: Optional values accepted for `url_param_key`, compared
            case-insensitively. Empty values remain unset.
    """

    name: str
    http_header_key: str | None = None
    env_var: str | None = None
    default: str | Callable[[], str] | None = None
    required: bool = True
    sensitive: bool = False
    normalize_fn: Callable[[str], str | None] | None = None
    url_param_key: str | None = None
    disallowed_url_param_keys: Mapping[str, str] = field(default_factory=dict)
    url_param_allowed_values: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.sensitive and self.url_param_key:
            raise ValueError(
                "sensitive config args can't be read from URL query params because URLs are commonly logged"
            )
        if self.disallowed_url_param_keys and not self.url_param_key:
            raise ValueError("disallowed URL query params require url_param_key")


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server created via mcp_server().

    This class stores the configuration passed to mcp_server() and provides
    methods for credential resolution.
    """

    name: str
    package_name: str | None = None
    advertised_properties: dict[str, Any] = field(default_factory=dict)
    server_info_provider: Callable[[], dict[str, Any]] | None = None
    config_args: list[MCPServerConfigArg] = field(default_factory=list)
    capability_resolvers: dict[str, CapabilityResolver] = field(default_factory=dict)
    _config_args_by_name: dict[str, MCPServerConfigArg] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Validate resolvers and build lookup dict for config args by name."""
        built_in_capabilities = {capability.value for capability in Capability}
        for capability_id in self.capability_resolvers:
            if not isinstance(capability_id, str) or not capability_id.strip():
                raise ValueError("Capability resolver keys must be non-empty strings")
            if capability_id in built_in_capabilities:
                raise ValueError(
                    f"Capability resolver cannot override built-in capability {capability_id!r}"
                )
        self._config_args_by_name = {arg.name: arg for arg in self.config_args}

    def get_config(self, name: str) -> str:
        """Get a configuration value by name.

        Resolution order:
        1. HTTP headers (case-insensitive)
        2. URL query parameters (case-insensitive)
        3. Environment variables
        4. Default value

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


def near_miss_url_param_error(given: str, expected: str) -> str:
    """Return a standard error message for an unsupported URL query parameter."""
    return (
        f"URL query param '{given}' is not supported by the standard. "
        f"Did you mean '{expected}'?"
    )


def validate_url_query_params(
    query_params: Iterable[tuple[str, str]],
    config_args: Iterable[MCPServerConfigArg],
) -> str | None:
    """Return an error message for a disallowed or invalid owned query param."""
    args = tuple(config_args)
    for given, value in query_params:
        given_normalized = given.casefold()
        for config_arg in args:
            for disallowed_key, message in config_arg.disallowed_url_param_keys.items():
                if given_normalized == disallowed_key.casefold():
                    return message

            if (
                config_arg.url_param_key is None
                or given_normalized != config_arg.url_param_key.casefold()
                or not value
                or config_arg.url_param_allowed_values is None
            ):
                continue

            allowed_values = config_arg.url_param_allowed_values
            if value.casefold() not in {
                allowed_value.casefold() for allowed_value in allowed_values
            }:
                return (
                    f"Invalid value for URL query param "
                    f"'{config_arg.url_param_key}'. Use one of: "
                    f"{', '.join(allowed_values)}."
                )
    return None


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
    """Resolve a single config argument from headers, URL query params, or environment.

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

    if config_arg.url_param_key:
        try:
            request = get_http_request()
        except RuntimeError:
            request = None
        if request is not None:
            url_param_key_lower = config_arg.url_param_key.lower()
            query_value = next(
                (
                    value
                    for key, value in request.query_params.multi_items()
                    if key.lower() == url_param_key_lower and value
                ),
                None,
            )
            if query_value is not None:
                normalized = _apply_normalize(query_value)
                if normalized is not None:
                    return normalized

    if config_arg.env_var:
        env_value = os.environ.get(config_arg.env_var)
        if env_value:
            normalized = _apply_normalize(env_value)
            if normalized is not None:
                return normalized

    if config_arg.default is not None:
        default = config_arg.default
        if isinstance(default, str):
            return default
        return default()

    if config_arg.required:
        sources: list[str] = []
        if config_arg.http_header_key:
            sources.append(f"HTTP header '{config_arg.http_header_key}'")
        if config_arg.url_param_key:
            sources.append(f"URL query param '{config_arg.url_param_key}'")
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
    ensures session-aware resolution of HTTP headers and URL query params.

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

    config: MCPServerConfig = app.x_mcp_server_config  # ty: ignore[unresolved-attribute]  # FastMCP does not declare extension configuration attributes.
    return config.get_config(name)
