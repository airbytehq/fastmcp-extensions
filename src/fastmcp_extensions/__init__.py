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
    build_client_credentials_post_kwargs,
    build_mcp_auth,
    fetch_client_credentials_token,
)
from fastmcp_extensions.client_credentials_middleware import (
    ClientCredentialsExchangeMiddleware,
    wrap_client_credentials,
)
from fastmcp_extensions.decorators import (
    mcp_prompt,
    mcp_provider,
    mcp_resource,
    mcp_tool,
)
from fastmcp_extensions.key_normalization import (
    DEFAULT_HASH_ALGORITHM,
    DEFAULT_KEY_PREFIX,
    HashKeyNormalizer,
    KeyNormalizer,
    NormalizedKeysWrapper,
)
from fastmcp_extensions.landing_page import (
    LandingPageContent,
    register_landing_page,
    render_default_landing_html,
)
from fastmcp_extensions.logging_redaction import (
    REDACTION_PLACEHOLDER,
    AuthorizationRedactionFilter,
    install_authorization_redaction,
    redact_authorization,
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
from fastmcp_extensions.tool_filters import (
    ToolFilterFn,
    assert_http_trusted_execution_disabled,
    is_trusted_execution_enabled,
)

__all__ = [
    "DEFAULT_HASH_ALGORITHM",
    "DEFAULT_KEY_PREFIX",
    "REDACTION_PLACEHOLDER",
    "AuthorizationRedactionFilter",
    "ClientCredentials",
    "ClientCredentialsExchangeMiddleware",
    "HashKeyNormalizer",
    "IntrospectionAuthConfig",
    "JWTAuthConfig",
    "KeyNormalizer",
    "LandingPageContent",
    "MCPServerConfig",
    "MCPServerConfigArg",
    "NormalizedKeysWrapper",
    "OIDCAuthConfig",
    "PromptDef",
    "ResourceDef",
    "TelemetryRecord",
    "TelemetrySinks",
    "ToolCallTelemetryMiddleware",
    "ToolCallTelemetryRecord",
    "ToolFilterFn",
    "assert_http_trusted_execution_disabled",
    "build_client_credentials_post_kwargs",
    "build_mcp_auth",
    "fetch_client_credentials_token",
    "get_mcp_config",
    "install_authorization_redaction",
    "is_trusted_execution_enabled",
    "mcp_prompt",
    "mcp_provider",
    "mcp_resource",
    "mcp_server",
    "mcp_tool",
    "redact_authorization",
    "register_landing_page",
    "register_mcp_prompts",
    "register_mcp_resources",
    "register_mcp_tools",
    "render_default_landing_html",
    "wrap_client_credentials",
]
