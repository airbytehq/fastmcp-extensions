# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""FastMCP Extensions - Unofficial extension library for FastMCP 2.0.

This library provides patterns, practices, and utilities for building MCP servers
with FastMCP 2.0, including:

- MCP annotation constants for tool hints
- Deferred registration decorators for tools, prompts, and resources
- Tool testing utilities
- Tool list measurement utilities
- Prompt text retrieval helpers
- Telemetry middleware for MCP tool call instrumentation
- Reusable CLI scaffolding with built-in telemetry (requires `[cli]` extra)
"""

from fastmcp_extensions._telemetry import TelemetryRecord, TelemetrySinks
from fastmcp_extensions._telemetry_middleware import (
    ToolCallTelemetryMiddleware,
    ToolCallTelemetryRecord,
)
from fastmcp_extensions.auth import (
    ClientCredentials,
    IntrospectionAuthConfig,
    JWTAuthConfig,
    OIDCAuthConfig,
    build_mcp_auth,
    fetch_client_credentials_token,
    resolve_mcp_auth,
)
from fastmcp_extensions.decorators import (
    mcp_prompt,
    mcp_provider,
    mcp_resource,
    mcp_tool,
)
from fastmcp_extensions.landing_page import (
    LandingPageContent,
    register_landing_page,
    render_default_landing_html,
)
from fastmcp_extensions.registration import (
    PromptDef,
    ResourceDef,
    register_mcp_prompts,
    register_mcp_resources,
    register_mcp_tools,
)
from fastmcp_extensions.server import mcp_server
from fastmcp_extensions.server_config import (
    MCPServerConfig,
    MCPServerConfigArg,
    get_mcp_config,
)
from fastmcp_extensions.tool_filters import ToolFilterFn

__all__ = [
    "ClientCredentials",
    "IntrospectionAuthConfig",
    "JWTAuthConfig",
    "LandingPageContent",
    "MCPServerConfig",
    "MCPServerConfigArg",
    "OIDCAuthConfig",
    "PromptDef",
    "ResourceDef",
    "TelemetryRecord",
    "TelemetrySinks",
    "ToolCallTelemetryMiddleware",
    "ToolCallTelemetryRecord",
    "ToolFilterFn",
    "build_mcp_auth",
    "fetch_client_credentials_token",
    "get_mcp_config",
    "mcp_prompt",
    "mcp_provider",
    "mcp_resource",
    "mcp_server",
    "mcp_tool",
    "register_landing_page",
    "register_mcp_prompts",
    "register_mcp_resources",
    "register_mcp_tools",
    "render_default_landing_html",
    "resolve_mcp_auth",
]
