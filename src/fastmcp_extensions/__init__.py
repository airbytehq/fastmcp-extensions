# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""FastMCP Extensions - Unofficial extension library for FastMCP 2.0.

This library provides patterns, practices, and utilities for building MCP servers
with FastMCP 2.0, including:

- MCP annotation constants for tool hints
- Deferred registration decorators for tools, prompts, and resources
- Tool testing utilities
- Tool list measurement utilities
- Prompt text retrieval helpers
"""

from fastmcp_extensions._middleware import ToolFilterFn
from fastmcp_extensions.annotations import (
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
    READ_ONLY_HINT,
)
from fastmcp_extensions.decorators import (
    mcp_prompt,
    mcp_resource,
    mcp_tool,
)
from fastmcp_extensions.registration import (
    PromptDef,
    ResourceDef,
    register_mcp_prompts,
    register_mcp_resources,
    register_mcp_tools,
)
from fastmcp_extensions.server import (
    MCPServerConfig,
    MCPServerConfigArg,
    get_mcp_config,
    mcp_server,
)
from fastmcp_extensions.tool_filters import (
    # Constants - Annotation Keys
    ANNOTATION_DESTRUCTIVE_HINT,
    ANNOTATION_MCP_MODULE,
    ANNOTATION_READ_ONLY_HINT,
    # Constants - Config Names
    CONFIG_EXCLUDE_MODULES,
    CONFIG_EXCLUDE_TOOLS,
    CONFIG_INCLUDE_MODULES,
    CONFIG_NO_DESTRUCTIVE_TOOLS,
    CONFIG_READONLY_MODE,
    # Constants - Environment Variables
    ENV_EXCLUDE_MODULES,
    ENV_EXCLUDE_TOOLS,
    ENV_INCLUDE_MODULES,
    ENV_NO_DESTRUCTIVE_TOOLS,
    ENV_READONLY_MODE,
    # Config Args
    EXCLUDE_MODULES_CONFIG_ARG,
    EXCLUDE_TOOLS_CONFIG_ARG,
    # Constants - HTTP Headers
    HEADER_EXCLUDE_MODULES,
    HEADER_EXCLUDE_TOOLS,
    HEADER_INCLUDE_MODULES,
    HEADER_NO_DESTRUCTIVE_TOOLS,
    HEADER_READONLY_MODE,
    INCLUDE_MODULES_CONFIG_ARG,
    NO_DESTRUCTIVE_TOOLS_CONFIG_ARG,
    READONLY_MODE_CONFIG_ARG,
    STANDARD_CONFIG_ARGS,
    STANDARD_TOOL_FILTERS,
    # Filter Functions
    module_filter,
    no_destructive_tools_filter,
    readonly_mode_filter,
    tool_exclusion_filter,
)

__all__ = [
    "ANNOTATION_DESTRUCTIVE_HINT",
    "ANNOTATION_MCP_MODULE",
    "ANNOTATION_READ_ONLY_HINT",
    "CONFIG_EXCLUDE_MODULES",
    "CONFIG_EXCLUDE_TOOLS",
    "CONFIG_INCLUDE_MODULES",
    "CONFIG_NO_DESTRUCTIVE_TOOLS",
    "CONFIG_READONLY_MODE",
    "DESTRUCTIVE_HINT",
    "ENV_EXCLUDE_MODULES",
    "ENV_EXCLUDE_TOOLS",
    "ENV_INCLUDE_MODULES",
    "ENV_NO_DESTRUCTIVE_TOOLS",
    "ENV_READONLY_MODE",
    "EXCLUDE_MODULES_CONFIG_ARG",
    "EXCLUDE_TOOLS_CONFIG_ARG",
    "HEADER_EXCLUDE_MODULES",
    "HEADER_EXCLUDE_TOOLS",
    "HEADER_INCLUDE_MODULES",
    "HEADER_NO_DESTRUCTIVE_TOOLS",
    "HEADER_READONLY_MODE",
    "IDEMPOTENT_HINT",
    "INCLUDE_MODULES_CONFIG_ARG",
    "NO_DESTRUCTIVE_TOOLS_CONFIG_ARG",
    "OPEN_WORLD_HINT",
    "READONLY_MODE_CONFIG_ARG",
    "READ_ONLY_HINT",
    "STANDARD_CONFIG_ARGS",
    "STANDARD_TOOL_FILTERS",
    "MCPServerConfig",
    "MCPServerConfigArg",
    "PromptDef",
    "ResourceDef",
    "ToolFilterFn",
    "get_mcp_config",
    "mcp_prompt",
    "mcp_resource",
    "mcp_server",
    "mcp_tool",
    "module_filter",
    "no_destructive_tools_filter",
    "readonly_mode_filter",
    "register_mcp_prompts",
    "register_mcp_resources",
    "register_mcp_tools",
    "tool_exclusion_filter",
]
