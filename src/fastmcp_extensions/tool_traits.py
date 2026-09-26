# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""In-process per-tool traits registry.

`mcp` 2.x `ToolAnnotations` drops unknown keys, and we deliberately keep
custom keys like `mcp_module` and `requiresClientFilesystem` off the wire
entirely. Instead, `register_mcp_tools` records each tool's traits — the
module it was declared in and the capabilities it requires — here,
keyed by the FastMCP app instance and tool name. Internal filters read them
back via `get_tool_traits`; `capability_filter` is the general consumer.
Entries are held weakly by app so they die with the server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from fastmcp import FastMCP


class Capability(str, Enum):
    """A capability a tool may require.

    Each member is satisfied either client-side (declared in the client's MCP
    capabilities) or deployment-side (transport, server settings). Tools
    declare *required* capabilities; `available_capabilities(app)` resolves
    what the current request actually has.
    """

    # MCP-native: satisfied client-side — the client declares the MCP Apps
    # UI extension in its capabilities.
    UI = "io.modelcontextprotocol/ui"

    # Custom: satisfied deployment-side — the server and client share a
    # local filesystem (stdio transport plus the trusted-execution setting).
    CLIENT_FILESYSTEM = "io.airbyte/client-filesystem"


@dataclass(frozen=True)
class ToolTraits:
    """Registration-time traits kept off the wire for a tool."""

    mcp_module: str | None = None
    required_capabilities: frozenset[Capability] = field(default_factory=frozenset)


_TRAITS: WeakKeyDictionary[FastMCP, dict[str, ToolTraits]] = WeakKeyDictionary()


def set_tool_traits(app: FastMCP, tool_name: str, traits: ToolTraits) -> None:
    """Record `traits` for `tool_name` on `app`."""
    _TRAITS.setdefault(app, {})[tool_name] = traits


def get_tool_traits(app: FastMCP, tool_name: str) -> ToolTraits:
    """Return the traits recorded for `tool_name`, or empty defaults."""
    return _TRAITS.get(app, {}).get(tool_name, ToolTraits())
