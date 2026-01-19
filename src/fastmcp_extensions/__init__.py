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
from fastmcp_extensions.server import mcp_server
from fastmcp_extensions.server_config import (
    MCPServerConfig,
    MCPServerConfigArg,
    get_mcp_config,
)
from fastmcp_extensions.tool_filters import ToolFilterFn

__all__ = [
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
    "register_mcp_prompts",
    "register_mcp_resources",
    "register_mcp_tools",
]
