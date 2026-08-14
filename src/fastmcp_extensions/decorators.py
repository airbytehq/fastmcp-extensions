# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Deferred MCP capability registration decorators.

This module provides decorators to tag tool, prompt, and resource functions
with MCP annotations for deferred registration. The decorators store metadata
on the functions for later use during registration with a FastMCP app.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from datetime import timedelta
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar, cast, get_type_hints

from fastmcp.server.providers import Provider
from pydantic import Field, create_model

from fastmcp_extensions.annotations import (
    ANNOTATION_INTERACTIVE_UI,
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
    READ_ONLY_HINT,
    REQUIRES_CLIENT_FILESYSTEM,
)
from fastmcp_extensions.session_state import (
    ToolStateBase,
    current_principal,
    decode_session_state,
    encode_session_state,
    state_ttl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

F = TypeVar("F", bound=Callable[..., Any])
P = TypeVar("P", bound=Callable[[], Provider])

_REGISTERED_TOOLS: list[tuple[Callable[..., Any], dict[str, Any]]] = []
_REGISTERED_PROVIDERS: list[tuple[Callable[[], Provider], dict[str, Any]]] = []
_REGISTERED_RESOURCES: list[tuple[Callable[..., Any], dict[str, Any]]] = []
_REGISTERED_PROMPTS: list[tuple[Callable[..., Any], dict[str, Any]]] = []
logger = logging.getLogger(__name__)


def _format_state_ttl(ttl: timedelta) -> str:
    total_seconds = int(ttl.total_seconds())
    for unit_seconds, unit_name in (
        (86_400, "day"),
        (3_600, "hour"),
        (60, "minute"),
        (1, "second"),
    ):
        if total_seconds >= unit_seconds and total_seconds % unit_seconds == 0:
            value = total_seconds // unit_seconds
            suffix = "" if value == 1 else "s"
            return f"{value} {unit_name}{suffix}"
    return str(ttl)


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
    requires_client_filesystem: bool = False,
    interactive_ui: bool = False,
    with_state: type[ToolStateBase] | None = None,
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
        requires_client_filesystem: If True, tool requires the client to have a
            local filesystem available (default: False)
        interactive_ui: If True, tool requires MCP Apps UI rendering support
            (default: False)
        with_state: Optional `ToolStateBase` subclass for explicit state carried
            between calls.
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
    if requires_client_filesystem:
        annotations[REQUIRES_CLIENT_FILESYSTEM] = True
    if interactive_ui:
        annotations[ANNOTATION_INTERACTIVE_UI] = True
    if with_state is not None:
        if not issubclass(with_state, ToolStateBase):
            raise TypeError("with_state must be a ToolStateBase subclass")
        annotations["_fastmcp_extensions_with_state"] = with_state

    def decorator(func: F) -> F:
        if extra_help_text:
            func.__doc__ = ((func.__doc__ or "") + "\n\n" + extra_help_text).rstrip()

        _REGISTERED_TOOLS.append((func, annotations))
        return func

    return decorator


def prepare_stateful_tool(
    func: Callable[..., Any],
    state_type: type[ToolStateBase],
    app: FastMCP,
) -> Callable[..., Any]:
    """Wrap a deferred tool with explicit encoded session state."""

    config = getattr(app, "x_mcp_extensions_session_state", None)
    if config is None:
        raise RuntimeError(
            "Stateful tools require an explicit encoded_session_state configuration "
            "with signing='required' or signing='disabled'."
        )
    if config.signing == "disabled":
        logger.warning(
            "Encoded session-state signing is explicitly disabled; handles are bearer credentials"
        )
    try:
        state_type()
    except TypeError as exc:
        raise TypeError(
            f"{state_type.__name__} must be default-constructible; "
            "every state field must have a default"
        ) from exc
    signature = inspect.signature(func)
    if "state_handle" not in signature.parameters:
        raise TypeError(
            f"Stateful tool {getattr(func, '__name__', 'stateful_tool')!r} must declare "
            "a `state_handle` parameter"
        )
    input_parameter = signature.parameters["state_handle"]
    if input_parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise TypeError("Stateful tool `state_handle` must be keyword-only")
    resolved_hints = get_type_hints(func, include_extras=True)
    durability = state_ttl(state_type)
    state_description = (
        "Pass back the `encoded_session_state` returned by the previous call. "
        f"If omitted, a fresh state is created. This state is durable for "
        f"{_format_state_ttl(durability)}."
    )
    encoded_state_annotation = Annotated[
        str | None,
        Field(description=state_description),
    ]
    parameters = [
        input_parameter.replace(
            name="encoded_session_state",
            annotation=encoded_state_annotation,
            default=None,
        )
        if parameter.name == "state_handle"
        else parameter.replace(
            annotation=resolved_hints.get(parameter.name, parameter.annotation)
        )
        for parameter in signature.parameters.values()
    ]
    return_annotation = resolved_hints.get("return", signature.return_annotation)
    if return_annotation is inspect.Parameter.empty:
        return_annotation = Any

    func_name = getattr(func, "__name__", "stateful_tool")
    result_type: type[Any] = cast(
        type[Any],
        create_model(
            f"{func_name.title().replace('_', '')}StatefulResult",
            result=(return_annotation, ...),
            encoded_session_state=(str, ...),
        ),
    )

    @wraps(func)
    async def stateful_wrapper(*args: Any, **kwargs: Any) -> Any:
        encoded = kwargs.pop("encoded_session_state", None)
        principal = current_principal(required=config.principal_binding)
        state = (
            state_type()
            if not encoded
            else decode_session_state(
                encoded,
                state_type,
                config,
                principal=principal,
            )
        )
        kwargs["state_handle"] = state
        result = func(*args, **kwargs)
        resolved = await result if inspect.isawaitable(result) else result
        return result_type(
            result=resolved,
            encoded_session_state=encode_session_state(
                state,
                config,
                principal=principal,
            ),
        )

    stateful_wrapper.__signature__ = (  # ty: ignore[unresolved-attribute]  # Function metadata supports runtime signature replacement.
        signature.replace(parameters=parameters).replace(return_annotation=result_type)
    )
    wrapper_annotations = dict(resolved_hints)
    wrapper_annotations.pop("state_handle", None)
    wrapper_annotations["encoded_session_state"] = encoded_state_annotation
    wrapper_annotations["return"] = result_type
    stateful_wrapper.__annotations__ = wrapper_annotations
    stateful_wrapper.__name__ = func_name
    stateful_wrapper.__doc__ = func.__doc__
    return stateful_wrapper


def mcp_provider(
    *,
    interactive_ui: bool = False,
    annotations: Mapping[str, object] | None = None,
) -> Callable[[P], P]:
    """Decorator to tag an MCP provider factory for deferred registration.

    Args:
        interactive_ui: If True, provider tools require MCP Apps UI rendering
            support (default: False)
        annotations: Extra annotations to apply to provider-sourced tools.

    Returns:
        Decorator function that tags the provider factory for registration
    """
    mcp_module_str = _get_caller_file_stem()

    provider_annotations: dict[str, Any] = {
        "mcp_module": mcp_module_str,
    }
    if interactive_ui:
        provider_annotations[ANNOTATION_INTERACTIVE_UI] = True
    provider_annotations.update(annotations or {})

    def decorator(func: P) -> P:
        _REGISTERED_PROVIDERS.append((func, provider_annotations))
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


def _clear_registrations() -> None:
    """Clear all registered tools, prompts, and resources.

    This is primarily useful for testing.
    """
    _REGISTERED_TOOLS.clear()
    _REGISTERED_PROVIDERS.clear()
    _REGISTERED_PROMPTS.clear()
    _REGISTERED_RESOURCES.clear()
