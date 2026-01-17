# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Deferred MCP capability registration decorators.

This module provides decorators to tag tool, prompt, and resource functions
with MCP annotations for deferred registration. The decorators store metadata
on the functions for later use during registration with a FastMCP app.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastmcp_extensions.annotations import (
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
    READ_ONLY_HINT,
)

F = TypeVar("F", bound=Callable[..., Any])

_REGISTERED_TOOLS: list[tuple[Callable[..., Any], dict[str, Any]]] = []
_REGISTERED_RESOURCES: list[tuple[Callable[..., Any], dict[str, Any]]] = []
_REGISTERED_PROMPTS: list[tuple[Callable[..., Any], dict[str, Any]]] = []


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


def _normalize_mcp_module(mcp_module: str) -> str:
    """Normalize an mcp_module string to its simple form.

    Handles both file stems (e.g., "github") and module names
    (e.g., "my_package.mcp.github") by extracting the last segment.

    Args:
        mcp_module: An mcp_module string, either a simple name or a dotted module path.

    Returns:
        The normalized mcp_module (last segment of a dotted path, or the input if no dots).
    """
    return mcp_module.rsplit(".", 1)[-1]


def mcp_tool(
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
    extra_help_text: str | None = None,
) -> Callable[[F], F]:
    """Decorator to tag an MCP tool function with annotations for deferred registration.

    This decorator stores the annotations on the function for later use during
    deferred registration. It does not register the tool immediately.

    The mcp_module is automatically derived from the file stem of the module where
    the tool is defined (e.g., tools in "github.py" get mcp_module "github").

    Args:
        read_only: If True, tool only reads without making changes (default: False)
        destructive: If True, tool modifies/deletes existing data (default: False)
        idempotent: If True, repeated calls have same effect (default: False)
        open_world: If True, tool interacts with external systems (default: False)
        extra_help_text: Optional text to append to the function's docstring
            with a newline delimiter

    Returns:
        Decorator function that tags the tool with annotations

    Example:
        @mcp_tool(read_only=True, idempotent=True)
        def list_connectors_in_repo():
            ...
    """
    mcp_module_str = _get_caller_file_stem()

    annotations: dict[str, Any] = {
        "mcp_module": mcp_module_str,
        READ_ONLY_HINT: read_only,
        DESTRUCTIVE_HINT: destructive,
        IDEMPOTENT_HINT: idempotent,
        OPEN_WORLD_HINT: open_world,
    }

    def decorator(func: F) -> F:
        if extra_help_text:
            func.__doc__ = ((func.__doc__ or "") + "\n\n" + extra_help_text).rstrip()

        _REGISTERED_TOOLS.append((func, annotations))
        return func

    return decorator


def mcp_prompt(
    name: str,
    description: str,
) -> Callable[
    [Callable[..., list[dict[str, str]]]], Callable[..., list[dict[str, str]]]
]:
    """Decorator for deferred MCP prompt registration.

    The mcp_module is automatically derived from the file stem of the module where
    the prompt is defined (e.g., prompts in "workflows.py" get mcp_module "workflows").

    Args:
        name: Unique name for the prompt
        description: Human-readable description of the prompt

    Returns:
        Decorator function that registers the prompt

    Example:
        @mcp_prompt("my_prompt", "A helpful prompt")
        def my_prompt_func() -> list[dict[str, str]]:
            return [{"role": "user", "content": "Hello"}]
    """
    mcp_module_str = _get_caller_file_stem()

    def decorator(
        func: Callable[..., list[dict[str, str]]],
    ) -> Callable[..., list[dict[str, str]]]:
        annotations = {
            "name": name,
            "description": description,
            "mcp_module": mcp_module_str,
        }
        _REGISTERED_PROMPTS.append((func, annotations))
        return func

    return decorator


def mcp_resource(
    uri: str,
    description: str,
    mime_type: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator for deferred MCP resource registration.

    The mcp_module is automatically derived from the file stem of the module where
    the resource is defined (e.g., resources in "server_info.py" get mcp_module "server_info").

    Args:
        uri: Unique URI for the resource
        description: Human-readable description of the resource
        mime_type: MIME type of the resource content

    Returns:
        Decorator function that registers the resource

    Example:
        @mcp_resource("myserver://version", "Server version info", "application/json")
        def get_version() -> dict:
            return {"version": "1.0.0"}
    """
    mcp_module_str = _get_caller_file_stem()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        annotations = {
            "uri": uri,
            "description": description,
            "mime_type": mime_type,
            "mcp_module": mcp_module_str,
        }
        _REGISTERED_RESOURCES.append((func, annotations))
        return func

    return decorator


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


def clear_registrations() -> None:
    """Clear all registered tools, prompts, and resources.

    This is primarily useful for testing.
    """
    _REGISTERED_TOOLS.clear()
    _REGISTERED_PROMPTS.clear()
    _REGISTERED_RESOURCES.clear()
