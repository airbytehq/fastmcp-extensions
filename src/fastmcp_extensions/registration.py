# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP capability registration utilities.

This module provides functions to register tools, prompts, and resources
with a FastMCP app, filtered by mcp_module.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from fastmcp_extensions.decorators import (
    _REGISTERED_PROMPTS,
    _REGISTERED_RESOURCES,
    _REGISTERED_TOOLS,
    _normalize_mcp_module,
)


@dataclass
class PromptDef:
    """Definition of a deferred MCP prompt."""

    name: str
    description: str
    func: Callable[..., list[dict[str, str]]]


@dataclass
class ResourceDef:
    """Definition of a deferred MCP resource."""

    uri: str
    description: str
    mime_type: str
    func: Callable[..., Any]


def _get_caller_file_stem() -> str:
    """Get the file stem of the caller's module.

    Walks up the call stack to find the first frame outside this module,
    then returns the stem of that file (e.g., "github" for "github.py").

    Returns:
        The file stem of the calling module.
    """
    for frame_info in inspect.stack():
        if frame_info.filename != __file__:
            return Path(frame_info.filename).stem
    return "unknown"


def _register_mcp_callables(
    *,
    app: FastMCP,
    mcp_module: str,
    resource_list: list[tuple[Callable[..., Any], dict[str, Any]]],
    register_fn: Callable[[FastMCP, Callable[..., Any], dict[str, Any]], None],
) -> None:
    """Register resources and tools with the FastMCP app, filtered by mcp_module.

    Args:
        app: The FastMCP app instance
        mcp_module: The mcp_module to register tools for. Can be a simple name (e.g., "github")
            or a full module path (e.g., "my_package.mcp.github" from __name__).
        resource_list: List of (callable, annotations) tuples to register
        register_fn: Function to call for each registration
    """
    mcp_module_str = _normalize_mcp_module(mcp_module)

    filtered_callables = [
        (func, ann)
        for func, ann in resource_list
        if ann.get("mcp_module") == mcp_module_str
    ]

    for callable_fn, callable_annotations in filtered_callables:
        register_fn(app, callable_fn, callable_annotations)


ToolFilterFn = Callable[[Callable[..., Any], dict[str, Any]], bool]
"""Type alias for tool filter functions.

A tool filter function takes a tool function and its annotations,
and returns True if the tool should be registered, False otherwise.
"""

ArgExclusionFilterFn = Callable[[Callable[..., Any], dict[str, Any]], list[str] | None]
"""Type alias for argument exclusion filter functions.

An argument exclusion filter function takes a tool function and its annotations,
and returns a list of argument names to exclude from the tool schema,
or None for no exclusions.
"""


def register_mcp_tools(
    app: FastMCP,
    mcp_module: str | None = None,
    *,
    exclude_args: list[str] | None = None,
    tool_filter: ToolFilterFn | None = None,
    arg_exclusion_filter: ArgExclusionFilterFn | None = None,
) -> None:
    """Register tools with the FastMCP app, filtered by mcp_module.

    This function supports both static and dynamic filtering of tools and their
    arguments. Static filtering uses the `exclude_args` parameter, while dynamic
    filtering uses callable functions that can make decisions at registration time.

    Args:
        app: The FastMCP app instance
        mcp_module: The mcp_module to register for. If not provided, automatically
            derived from the caller's file stem.
        exclude_args: Optional list of argument names to exclude from tool schema.
            This is useful for arguments that are injected by middleware.
        tool_filter: Optional callable that determines whether a tool should be
            registered. Takes (func, annotations) and returns True to include
            the tool, False to exclude it. This allows filtering tools based on
            custom logic (e.g., read-only mode, feature flags).
        arg_exclusion_filter: Optional callable that determines which arguments
            to exclude from a tool's schema. Takes (func, annotations) and returns
            a list of argument names to exclude, or None for no exclusions.
            This allows hiding arguments based on runtime conditions (e.g., hide
            workspace_id when an environment variable is set).

    Example:
        ```python
        import os
        import inspect


        # Only show read-only tools in readonly mode
        def only_readonly_tools(func, annotations):
            if os.environ.get("READONLY_MODE"):
                return annotations.get("readOnlyHint", False)
            return True


        # Hide workspace_id when env var is set
        def exclude_workspace_id_when_set(func, annotations):
            if os.environ.get("WORKSPACE_ID"):
                params = set(inspect.signature(func).parameters.keys())
                return [name for name in ["workspace_id"] if name in params]
            return None


        register_mcp_tools(
            app,
            tool_filter=only_readonly_tools,
            arg_exclusion_filter=exclude_workspace_id_when_set,
        )
        ```
    """
    if mcp_module is None:
        mcp_module = _get_caller_file_stem()

    def _register_fn(
        app: FastMCP,
        callable_fn: Callable[..., Any],
        annotations: dict[str, Any],
    ) -> None:
        # Apply tool filter if provided
        if tool_filter is not None and not tool_filter(callable_fn, annotations):
            return

        # Compute excluded args from both static and dynamic sources
        params = set(inspect.signature(callable_fn).parameters.keys())
        excluded_args_set: set[str] = set()

        # Add static exclusions
        if exclude_args:
            excluded_args_set.update(name for name in exclude_args if name in params)

        # Add dynamic exclusions
        if arg_exclusion_filter is not None:
            dynamic_exclusions = arg_exclusion_filter(callable_fn, annotations)
            if dynamic_exclusions:
                excluded_args_set.update(
                    name for name in dynamic_exclusions if name in params
                )

        tool_exclude_args = list(excluded_args_set) if excluded_args_set else None

        app.tool(
            callable_fn,
            annotations=annotations,
            exclude_args=tool_exclude_args,
        )

    _register_mcp_callables(
        app=app,
        mcp_module=mcp_module,
        resource_list=_REGISTERED_TOOLS,
        register_fn=_register_fn,
    )


def register_mcp_prompts(
    app: FastMCP,
    mcp_module: str | None = None,
) -> None:
    """Register prompt callables with the FastMCP app, filtered by mcp_module.

    Args:
        app: The FastMCP app instance
        mcp_module: The mcp_module to register for. If not provided, automatically
            derived from the caller's file stem.
    """
    if mcp_module is None:
        mcp_module = _get_caller_file_stem()

    def _register_fn(
        app: FastMCP,
        callable_fn: Callable[..., Any],
        annotations: dict[str, Any],
    ) -> None:
        app.prompt(
            name=annotations["name"],
            description=annotations["description"],
        )(callable_fn)

    _register_mcp_callables(
        app=app,
        mcp_module=mcp_module,
        resource_list=_REGISTERED_PROMPTS,
        register_fn=_register_fn,
    )


def register_mcp_resources(
    app: FastMCP,
    mcp_module: str | None = None,
) -> None:
    """Register resource callables with the FastMCP app, filtered by mcp_module.

    Args:
        app: The FastMCP app instance
        mcp_module: The mcp_module to register for. If not provided, automatically
            derived from the caller's file stem.
    """
    if mcp_module is None:
        mcp_module = _get_caller_file_stem()

    def _register_fn(
        app: FastMCP,
        callable_fn: Callable[..., Any],
        annotations: dict[str, Any],
    ) -> None:
        app.resource(
            annotations["uri"],
            description=annotations["description"],
            mime_type=annotations["mime_type"],
        )(callable_fn)

    _register_mcp_callables(
        app=app,
        mcp_module=mcp_module,
        resource_list=_REGISTERED_RESOURCES,
        register_fn=_register_fn,
    )


def get_registered_tools() -> list[tuple[Callable[..., Any], dict[str, Any]]]:
    """Get all registered tools.

    Returns:
        List of (function, annotations) tuples for all registered tools.
    """
    return _REGISTERED_TOOLS.copy()


def get_registered_prompts() -> list[tuple[Callable[..., Any], dict[str, Any]]]:
    """Get all registered prompts.

    Returns:
        List of (function, annotations) tuples for all registered prompts.
    """
    return _REGISTERED_PROMPTS.copy()


def get_registered_resources() -> list[tuple[Callable[..., Any], dict[str, Any]]]:
    """Get all registered resources.

    Returns:
        List of (function, annotations) tuples for all registered resources.
    """
    return _REGISTERED_RESOURCES.copy()
