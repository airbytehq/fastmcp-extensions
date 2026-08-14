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
from fastmcp_extensions.capability_tokens import (
    DEFAULT_EXTENSIONS_HEADER,
    CapabilityTokenMiddleware,
    RejectEventStreamGetMiddleware,
    client_declared_extensions_from_headers,
    client_supports_extension,
    decode_capability_token,
    encode_capability_token,
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
from fastmcp_extensions.http_server import DEFAULT_UVICORN_CONFIG, run_mcp_http_server
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
from fastmcp_extensions.session_state import (
    DEFAULT_STATE_SECRET_ENV_VAR,
    DEFAULT_STATE_TTL,
    DecodedSessionState,
    EncodedSessionStateConfig,
    EncodedSessionStateError,
    ToolStateBase,
    decode_session_state,
    encode_session_state,
)
from fastmcp_extensions.tool_filters import (
    ANNOTATION_INTERACTIVE_UI,
    ToolFilterFn,
    assert_http_trusted_execution_disabled,
    extension_tool_filter,
    interactive_ui_filter,
    is_trusted_execution_enabled,
)

__all__ = [
    "ANNOTATION_INTERACTIVE_UI",
    "DEFAULT_EXTENSIONS_HEADER",
    "DEFAULT_HASH_ALGORITHM",
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_STATE_SECRET_ENV_VAR",
    "DEFAULT_STATE_TTL",
    "DEFAULT_UVICORN_CONFIG",
    "REDACTION_PLACEHOLDER",
    "AuthorizationRedactionFilter",
    "CapabilityTokenMiddleware",
    "ClientCredentials",
    "ClientCredentialsExchangeMiddleware",
    "DecodedSessionState",
    "EncodedSessionStateConfig",
    "EncodedSessionStateError",
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
    "RejectEventStreamGetMiddleware",
    "ResourceDef",
    "TelemetryRecord",
    "TelemetrySinks",
    "ToolCallTelemetryMiddleware",
    "ToolCallTelemetryRecord",
    "ToolFilterFn",
    "ToolStateBase",
    "assert_http_trusted_execution_disabled",
    "build_client_credentials_post_kwargs",
    "build_mcp_auth",
    "client_declared_extensions_from_headers",
    "client_supports_extension",
    "decode_capability_token",
    "decode_session_state",
    "encode_capability_token",
    "encode_session_state",
    "extension_tool_filter",
    "fetch_client_credentials_token",
    "get_mcp_config",
    "install_authorization_redaction",
    "interactive_ui_filter",
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
    "run_mcp_http_server",
    "wrap_client_credentials",
]
