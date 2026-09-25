# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP tool annotation constants.

These constants define the standard MCP annotations for tools, following the
FastMCP 2.2.7+ specification.

For more information, see:
https://gofastmcp.com/concepts/tools#mcp-annotations
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

# =============================================================================
# Standard MCP spec annotations (ToolAnnotations fields)
# =============================================================================

READ_ONLY_HINT = "readOnlyHint"
"""Indicates if the tool only reads data without making any changes.

When True, the tool performs read-only operations and does not modify any state.
When False, the tool may write, create, update, or delete data.

FastMCP default if not specified: False
"""

DESTRUCTIVE_HINT = "destructiveHint"
"""Signals if the tool's changes are destructive (updates or deletes existing data).

This hint is only relevant for non-read-only tools (readOnlyHint=False).
When True, the tool modifies or deletes existing data in a way that may be
difficult or impossible to reverse.
When False, the tool creates new data or performs non-destructive operations.

FastMCP default if not specified: True
"""

IDEMPOTENT_HINT = "idempotentHint"
"""Indicates if repeated calls with the same parameters have the same effect.

When True, calling the tool multiple times with identical parameters produces
the same result and side effects as calling it once.
When False, each call may produce different results or side effects.

FastMCP default if not specified: False
"""

OPEN_WORLD_HINT = "openWorldHint"
"""Specifies if the tool interacts with external systems.

When True, the tool communicates with external services, APIs, or systems
outside the local environment (e.g., cloud APIs, remote databases, internet).
When False, the tool only operates on local state or resources.

FastMCP default if not specified: True
"""

# =============================================================================
# Custom metadata keys (surfaced via Tool.meta on the wire, not ToolAnnotations)
# =============================================================================

ANNOTATION_MCP_MODULE = "mcp_module"
"""Metadata key for the module a capability was declared in.

Set automatically by the ``@mcp_tool`` / ``@mcp_prompt`` / ``@mcp_resource`` /
``@mcp_provider`` decorators from the caller's file stem, and used by
``register_mcp_tools`` and the docs generator to route capabilities.
"""

REQUIRES_CLIENT_FILESYSTEM = "requiresClientFilesystem"
"""Indicates that the tool requires access to the client's local filesystem.

When `True`, the tool depends on the MCP client having a local filesystem
available (e.g., reading/writing files, scanning directories, accessing a
local git checkout). In hosted environments where the client has no local
filesystem, tools with this annotation should be hidden.

This is a custom annotation (not part of the MCP spec). `mcp` 2.x
`ToolAnnotations` drops unknown keys, so custom keys like this one travel
in `meta` instead of `annotations` on the wire.

Default if not specified: `False` (no client filesystem required).
"""

UI_META_KEY = "ui"
"""MCP Apps standard `_meta.ui` key linking a tool to its UI resource.

Written by FastMCP from `AppConfig` when a tool is registered with `app=`;
`interactive_ui_filter` gates tools carrying this key on the client's
`io.modelcontextprotocol/ui` extension declaration.
See https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/draft/apps.mdx
"""

TOOL_META_KEY = "_fastmcp_extensions_meta"
"""Internal registration-time key carrying user-supplied tool `meta`.

Set by ``@mcp_tool(meta=...)`` and popped at registration time — the mapping
itself is merged into `tool.meta` (the wire `meta` field); the key is never
sent on the wire.
"""

TOOL_APP_KEY = "_fastmcp_extensions_app"
"""Internal registration-time key carrying the tool's `AppConfig`.

Set by ``@mcp_tool(app=...)`` and popped at registration time — the config is
passed to `app.tool(app=...)`, which writes the standard `_meta.ui` marker;
the key is never sent on the wire.
"""

WITH_STATE_ANNOTATION = "_fastmcp_extensions_with_state"
"""Internal registration-time key carrying the `ToolStateBase` subclass.

Set by ``@mcp_tool(with_state=...)`` and popped at registration time — it is
never sent on the wire.
"""


def standard_annotation_field_names() -> dict[str, str]:
    """Map every accepted standard annotation key to its `ToolAnnotations` field.

    Keys include both the snake_case field names and their camelCase wire
    aliases, so callers can resolve either spelling without triggering the
    deprecated camelCase attribute shims.
    """
    return {field_name: field_name for field_name in ToolAnnotations.model_fields} | {
        field.alias: field_name
        for field_name, field in ToolAnnotations.model_fields.items()
        if field.alias is not None
    }
