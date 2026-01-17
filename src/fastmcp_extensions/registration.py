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


def register_mcp_tools(
    app: FastMCP,
    mcp_module: str | None = None,
    *,
    exclude_args: list[str] | None = None,
) -> None:
    """Register tools with the FastMCP app, filtered by mcp_module.

    Args:
        app: The FastMCP app instance
        mcp_module: The mcp_module to register for. If not provided, automatically
            derived from the caller's file stem.
        exclude_args: Optional list of argument names to exclude from tool schema.
            This is useful for arguments that are injected by middleware.
    """
    if mcp_module is None:
        mcp_module = _get_caller_file_stem()

    def _register_fn(
        app: FastMCP,
        callable_fn: Callable[..., Any],
        annotations: dict[str, Any],
    ) -> None:
        tool_exclude_args: list[str] | None = None
        if exclude_args:
            params = set(inspect.signature(callable_fn).parameters.keys())
            excluded = [name for name in exclude_args if name in params]
            tool_exclude_args = excluded if excluded else None

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
