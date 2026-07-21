# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Standard tool filters for MCP servers.

This module provides pre-defined config args and filter functions for common
tool filtering use cases. These can be used individually or together via the
`include_standard_tool_filters=True` parameter on `mcp_server()`.

## Key Components

- **Config Args**: Pre-defined MCPServerConfigArg instances for standard filters
- **Filter Functions**: Public filter functions that can be used individually
- **Constants**: Config names, env vars, headers, and annotation keys

## Basic Usage

Use standard filters automatically:

```py
from fastmcp_extensions import mcp_server

app = mcp_server(
    name="my-server",
    include_standard_tool_filters=True,
)
```

Or use individual filters:

```py
from fastmcp_extensions import mcp_server, ToolFilterMiddleware
from fastmcp_extensions.tool_filters import (
    readonly_mode_filter,
    READONLY_MODE_CONFIG_ARG,
)

app = mcp_server(
    name="my-server",
    server_config_args=[READONLY_MODE_CONFIG_ARG],
)
app.add_middleware(ToolFilterMiddleware(app, tool_filter=readonly_mode_filter))
```
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from mcp.types import Tool

from fastmcp_extensions.server_config import MCPServerConfigArg, get_mcp_config

ToolFilterFn = Callable[[Tool, FastMCP], bool]
"""Type alias for tool filter functions.

A tool filter function takes a Tool object and the FastMCP app,
and returns True if the tool should be visible, False to hide it.

The FastMCP app is passed so the filter can call get_mcp_config()
to access request-specific configuration values (from HTTP headers,
env vars, or defaults).

Example:
    ```python
    def readonly_filter(tool: Tool, app: FastMCP) -> bool:
        if get_mcp_config(app, "readonly_mode") == "1":
            annotations = tool.annotations
            if annotations is None:
                return False
            return getattr(annotations, "readOnlyHint", False)
        return True
    ```
"""

# =============================================================================
# Constants - Config Names
# =============================================================================

CONFIG_READONLY_MODE = "readonly_mode"
"""Config name for read-only mode."""

CONFIG_NO_DESTRUCTIVE_TOOLS = "no_destructive_tools"
"""Config name for hiding destructive tools."""

CONFIG_EXCLUDE_MODULES = "exclude_modules"
"""Config name for excluding tools by module name."""

CONFIG_INCLUDE_MODULES = "include_modules"
"""Config name for including only specific modules."""

CONFIG_EXCLUDE_TOOLS = "exclude_tools"
"""Config name for excluding specific tools by name."""

CONFIG_NO_CLIENT_FILESYSTEM = "no_client_filesystem"
"""Config name for hiding tools that require client filesystem access."""

CONFIG_TRUSTED_EXECUTION = "trusted_execution"
"""Config name for the trusted-execution master gate.

When disabled (the default), tools annotated `requiresClientFilesystem=True`
(local connector execution, filesystem reads, cache/SQL, smoke tests) are hidden.
Granular filters such as `no_client_filesystem` layer *under* this gate and can
only further-restrict, never widen.
"""

# =============================================================================
# Constants - Environment Variables
# =============================================================================

ENV_READONLY_MODE = "MCP_READONLY_MODE"
"""Environment variable for read-only mode."""

ENV_NO_DESTRUCTIVE_TOOLS = "MCP_NO_DESTRUCTIVE_TOOLS"
"""Environment variable for hiding destructive tools."""

ENV_EXCLUDE_MODULES = "MCP_EXCLUDE_MODULES"
"""Environment variable for excluding tools by module name."""

ENV_INCLUDE_MODULES = "MCP_INCLUDE_MODULES"
"""Environment variable for including only specific modules."""

ENV_EXCLUDE_TOOLS = "MCP_EXCLUDE_TOOLS"
"""Environment variable for excluding specific tools by name."""

ENV_NO_CLIENT_FILESYSTEM = "MCP_NO_CLIENT_FILESYSTEM"
"""Environment variable for hiding tools that require client filesystem access."""

ENV_TRUSTED_EXECUTION = "MCP_TRUSTED_EXECUTION"
"""Environment variable for the trusted-execution master gate (generic default).

This is the library's generic default env var. A host application may back the
`trusted_execution` config with its own env var by supplying a replacement
`MCPServerConfigArg` named `trusted_execution` (host-supplied config args take
precedence over the standard ones); see `TRUSTED_EXECUTION_CONFIG_ARG`.
"""

# =============================================================================
# Constants - HTTP Headers
# =============================================================================

HEADER_READONLY_MODE = "X-MCP-Readonly-Mode"
"""HTTP header for read-only mode."""

HEADER_NO_DESTRUCTIVE_TOOLS = "X-MCP-No-Destructive-Tools"
"""HTTP header for hiding destructive tools."""

HEADER_EXCLUDE_MODULES = "X-MCP-Exclude-Modules"
"""HTTP header for excluding tools by module name."""

HEADER_INCLUDE_MODULES = "X-MCP-Include-Modules"
"""HTTP header for including only specific modules."""

HEADER_EXCLUDE_TOOLS = "X-MCP-Exclude-Tools"
"""HTTP header for excluding specific tools by name."""

# NOTE: `no_client_filesystem` and `trusted_execution` deliberately have no HTTP
# header. They govern the backend host's filesystem/execution exposure profile,
# which must be server-determined and never caller-controlled: a header source
# would let a remote caller change the security footprint of the deployment.
# The self-restricting filters above (readonly, no-destructive, module/tool
# exclusion) keep their headers because a caller can only narrow their own
# surface with them, never widen the host's.

# =============================================================================
# Constants - Annotation Keys
# =============================================================================

ANNOTATION_READ_ONLY_HINT = "readOnlyHint"
"""Annotation key for read-only hint (MCP spec)."""

ANNOTATION_DESTRUCTIVE_HINT = "destructiveHint"
"""Annotation key for destructive hint (MCP spec)."""

ANNOTATION_MCP_MODULE = "mcp_module"
"""Annotation key for MCP module name (set by @mcp_tool decorator)."""

ANNOTATION_REQUIRES_CLIENT_FILESYSTEM = "requiresClientFilesystem"
"""Annotation key for client filesystem requirement."""

# =============================================================================
# Standard Config Args
# =============================================================================

READONLY_MODE_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_READONLY_MODE,
    http_header_key=HEADER_READONLY_MODE,
    env_var=ENV_READONLY_MODE,
    default="0",
    required=False,
)
"""Standard config arg for read-only mode.

When set to "1", only tools with readOnlyHint=True annotation will be visible.
Can be set via X-MCP-Readonly-Mode HTTP header or MCP_READONLY_MODE env var.
"""

NO_DESTRUCTIVE_TOOLS_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_NO_DESTRUCTIVE_TOOLS,
    http_header_key=HEADER_NO_DESTRUCTIVE_TOOLS,
    env_var=ENV_NO_DESTRUCTIVE_TOOLS,
    default="0",
    required=False,
)
"""Standard config arg for hiding destructive tools.

When set to "1" or "true", tools with destructiveHint=True annotation will be hidden.
Can be set via X-No-Destructive-Tools HTTP header or MCP_NO_DESTRUCTIVE_TOOLS env var.
"""

EXCLUDE_MODULES_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_EXCLUDE_MODULES,
    http_header_key=HEADER_EXCLUDE_MODULES,
    env_var=ENV_EXCLUDE_MODULES,
    default="",
    required=False,
)
"""Standard config arg for excluding tools by module name.

Comma-separated list of module names to exclude. Tools from these modules will be hidden.
Can be set via X-MCP-Exclude-Modules HTTP header or MCP_EXCLUDE_MODULES env var.
Mutually exclusive with include_modules.
"""

INCLUDE_MODULES_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_INCLUDE_MODULES,
    http_header_key=HEADER_INCLUDE_MODULES,
    env_var=ENV_INCLUDE_MODULES,
    default="",
    required=False,
)
"""Standard config arg for including only specific modules.

Comma-separated list of module names to include. Only tools from these modules will be visible.
Can be set via X-MCP-Include-Modules HTTP header or MCP_INCLUDE_MODULES env var.
Mutually exclusive with exclude_modules.
"""

EXCLUDE_TOOLS_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_EXCLUDE_TOOLS,
    http_header_key=HEADER_EXCLUDE_TOOLS,
    env_var=ENV_EXCLUDE_TOOLS,
    default="",
    required=False,
)
"""Standard config arg for excluding specific tools by name.

Comma-separated list of tool names to exclude. These tools will be hidden.
Can be set via X-MCP-Exclude-Tools HTTP header or MCP_EXCLUDE_TOOLS env var.
"""

NO_CLIENT_FILESYSTEM_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_NO_CLIENT_FILESYSTEM,
    env_var=ENV_NO_CLIENT_FILESYSTEM,
    default="0",
    required=False,
)
"""Standard config arg for hiding tools that require client filesystem access.

When set to `1` or `true`, tools annotated with `requiresClientFilesystem=True`
will be hidden. Use this in hosted/remote environments where the client has
no local filesystem.

Resolved from the `MCP_NO_CLIENT_FILESYSTEM` env var only. It has no HTTP header
source on purpose: it governs the backend host's filesystem-exposure profile,
which must not be caller-controllable.
"""

TRUSTED_EXECUTION_CONFIG_ARG = MCPServerConfigArg(
    name=CONFIG_TRUSTED_EXECUTION,
    env_var=ENV_TRUSTED_EXECUTION,
    default="0",
    required=False,
)
"""Standard config arg for the trusted-execution master gate.

Defaults to `0` (untrusted) on every transport, so a copied stdio registration
grants no filesystem/execution capability until an operator explicitly opts in.
Resolved from the `MCP_TRUSTED_EXECUTION` env var only, with no HTTP header
source: the gate widens the tool surface, so it must never be caller-controlled.

A host application may back this config with a different env var by supplying its
own `MCPServerConfigArg(name="trusted_execution", env_var="...")` in
`server_config_args`; host-supplied config args take precedence over the standard
ones of the same name.
"""

STANDARD_CONFIG_ARGS: list[MCPServerConfigArg] = [
    READONLY_MODE_CONFIG_ARG,
    NO_DESTRUCTIVE_TOOLS_CONFIG_ARG,
    EXCLUDE_MODULES_CONFIG_ARG,
    INCLUDE_MODULES_CONFIG_ARG,
    EXCLUDE_TOOLS_CONFIG_ARG,
    NO_CLIENT_FILESYSTEM_CONFIG_ARG,
    TRUSTED_EXECUTION_CONFIG_ARG,
]
"""List of all standard config args for tool filtering."""


# =============================================================================
# Helper Functions
# =============================================================================


def get_annotation(
    tool_or_asset: Tool,
    annotation_name: str,
    default: bool | str | None = None,
) -> bool | str | None:
    """Get an annotation value from a tool or asset.

    This helper hides the messy getattr implementation needed to access
    annotations stored in pydantic's model_extra.

    Args:
        tool_or_asset: The Tool (or other MCP asset) to get the annotation from.
        annotation_name: The name of the annotation to retrieve.
        default: The default value to return if the annotation is not present.

    Returns:
        The annotation value, or the default if not present.
    """
    annotations = tool_or_asset.annotations
    if annotations is None:
        return default
    return getattr(annotations, annotation_name, default)


def _parse_csv_config(value: str) -> list[str]:
    """Parse a comma-separated config value into a list of strings.

    Args:
        value: Comma-separated string value.

    Returns:
        List of trimmed, non-empty strings.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# =============================================================================
# Standard Filter Functions
# =============================================================================


def readonly_mode_filter(tool: Tool, app: FastMCP) -> bool:
    """Filter tools based on readonly_mode config.

    When readonly_mode is "1" or "true", only show tools with readOnlyHint=True.
    When readonly_mode is "0" (default), show all tools.

    Args:
        tool: The tool to check.
        app: The FastMCP app instance.

    Returns:
        True if the tool should be visible, False to hide it.
    """
    config_value = get_mcp_config(app, CONFIG_READONLY_MODE).lower()
    if config_value in ("1", "true"):
        return bool(get_annotation(tool, ANNOTATION_READ_ONLY_HINT, False))
    return True


def no_destructive_tools_filter(tool: Tool, app: FastMCP) -> bool:
    """Filter tools based on no_destructive_tools config.

    When no_destructive_tools is "1" or "true", hide tools with destructiveHint=True.
    When no_destructive_tools is "0" (default), show all tools.

    Args:
        tool: The tool to check.
        app: The FastMCP app instance.

    Returns:
        True if the tool should be visible, False to hide it.
    """
    config_value = get_mcp_config(app, CONFIG_NO_DESTRUCTIVE_TOOLS).lower()
    if config_value in ("1", "true"):
        return not bool(get_annotation(tool, ANNOTATION_DESTRUCTIVE_HINT, False))
    return True


def module_filter(tool: Tool, app: FastMCP) -> bool:
    """Filter tools based on exclude_modules and include_modules config.

    When exclude_modules is set, hide tools from those modules.
    When include_modules is set, only show tools from those modules.
    If both are set, raises ValueError (mutually exclusive).

    Args:
        tool: The tool to check.
        app: The FastMCP app instance.

    Returns:
        True if the tool should be visible, False to hide it.

    Raises:
        ValueError: If both exclude_modules and include_modules are set.
    """
    exclude_modules = _parse_csv_config(get_mcp_config(app, CONFIG_EXCLUDE_MODULES))
    include_modules = _parse_csv_config(get_mcp_config(app, CONFIG_INCLUDE_MODULES))

    if exclude_modules and include_modules:
        raise ValueError(
            "Incompatible module filter configuration: both `exclude_modules` and "
            "`include_modules` are set, but they are mutually exclusive. "
            "Remediation: configure only one of them (clear the other) and restart "
            "the server."
        )

    # Get the tool's mcp_module from annotations
    tool_module = get_annotation(tool, ANNOTATION_MCP_MODULE, None)

    if exclude_modules:
        # Hide tools from excluded modules
        return not (tool_module and tool_module in exclude_modules)

    if include_modules:
        # Only show tools from included modules
        return bool(tool_module and tool_module in include_modules)

    return True


def tool_exclusion_filter(tool: Tool, app: FastMCP) -> bool:
    """Filter tools based on exclude_tools config.

    When exclude_tools is set, hide tools with those names.

    Args:
        tool: The tool to check.
        app: The FastMCP app instance.

    Returns:
        True if the tool should be visible, False to hide it.
    """
    exclude_tools = _parse_csv_config(get_mcp_config(app, CONFIG_EXCLUDE_TOOLS))
    return not (exclude_tools and tool.name in exclude_tools)


def no_client_filesystem_filter(tool: Tool, app: FastMCP) -> bool:
    """Filter tools based on `no_client_filesystem` config.

    When `no_client_filesystem` is `1` or `true`, hide tools annotated with
    `requiresClientFilesystem=True`. Use this in hosted/remote environments
    where the MCP client has no local filesystem.

    Args:
        tool: The tool to check.
        app: The FastMCP app instance.

    Returns:
        `True` if the tool should be visible, `False` to hide it.
    """
    config_value = get_mcp_config(app, CONFIG_NO_CLIENT_FILESYSTEM).lower()
    if config_value in ("1", "true"):
        return not bool(
            get_annotation(tool, ANNOTATION_REQUIRES_CLIENT_FILESYSTEM, False)
        )
    return True


def _is_truthy(value: str | None) -> bool:
    """Return whether a config string represents an enabled/true value."""
    return (value or "").strip().lower() in ("1", "true", "yes")


def _is_http_transport_request() -> bool:
    """Return whether the current call is being served over the HTTP transport.

    `get_http_request` raises `RuntimeError` when there is no active HTTP request
    (for example on the stdio transport), which is how we distinguish transports.
    """
    try:
        get_http_request()
    except RuntimeError:
        return False
    return True


def is_trusted_execution_enabled(app: FastMCP) -> bool:
    """Return whether trusted execution is enabled via server configuration.

    Reads the `trusted_execution` config, which resolves from the server
    environment only (there is deliberately no HTTP-header source: the gate
    *widens* the tool surface, so it must never be caller-controlled). Defaults
    to `False` when unset.
    """
    return _is_truthy(get_mcp_config(app, CONFIG_TRUSTED_EXECUTION))


def trusted_execution_filter(tool: Tool, app: FastMCP) -> bool:
    """Master gate hiding client-filesystem/exec tools unless trusted execution is on.

    Trusted execution defaults to *off* on every transport, so a tool annotated
    `requiresClientFilesystem=True` (local connector execution, filesystem reads,
    cache/SQL, smoke tests) is hidden unless an operator explicitly enables the
    gate via server configuration.

    The gate is *permanently incompatible* with the HTTP transport: when the
    current call is served over HTTP the gate is forced off regardless of
    configuration, so a hosted deployment can never expose the local/exec
    surface even if the env var is set. Granular filters such as
    `no_client_filesystem_filter` layer *under* this gate -- because filters
    compose with logical AND, they can only further-restrict, never widen.

    Args:
        tool: The tool to check.
        app: The FastMCP app instance.

    Returns:
        `True` if the tool should be visible, `False` to hide it.
    """
    if is_trusted_execution_enabled(app) and not _is_http_transport_request():
        return True
    return not bool(get_annotation(tool, ANNOTATION_REQUIRES_CLIENT_FILESYSTEM, False))


def assert_http_trusted_execution_disabled(app: FastMCP) -> None:
    """Hard-fail when trusted execution is enabled; call from an HTTP entrypoint.

    Invoke this from the HTTP transport entrypoint *before* serving. Trusted
    execution grants local filesystem and connector-execution capability and is
    permanently incompatible with HTTP/hosted access, so an explicit opt-in on an
    HTTP server is a configuration error to surface loudly rather than silently
    ignore. The `trusted_execution_filter` independently forces the gate off
    per-request under HTTP, so this is a defense-in-depth startup guard.

    Args:
        app: The FastMCP app instance.

    Raises:
        RuntimeError: If trusted execution is enabled in the environment.
    """
    if is_trusted_execution_enabled(app):
        raise RuntimeError(
            "Trusted execution is enabled but is permanently incompatible with the "
            "HTTP transport. Trusted execution grants local filesystem and connector "
            "execution capability, which must never be reachable by a remote HTTP "
            "caller. Remediation: unset the trusted-execution environment variable "
            "(or set it to '0') for this HTTP/hosted deployment. Trusted execution is "
            "only available on the stdio transport."
        )


STANDARD_TOOL_FILTERS: list[ToolFilterFn] = [
    readonly_mode_filter,
    no_destructive_tools_filter,
    module_filter,
    tool_exclusion_filter,
    no_client_filesystem_filter,
    trusted_execution_filter,
]
"""List of all standard tool filter functions."""
