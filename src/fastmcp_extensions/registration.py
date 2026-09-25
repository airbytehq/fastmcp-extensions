# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP capability registration utilities.

This module provides functions to register tools, prompts, and resources
with a FastMCP app, filtered by mcp_module.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.utilities.versions import VersionSpec
from mcp.types import ToolAnnotations

from fastmcp_extensions.annotations import (
    ANNOTATION_MCP_MODULE,
    TOOL_APP_KEY,
    TOOL_META_KEY,
    TOOL_REQUIRES_KEY,
    UI_META_KEY,
    WITH_STATE_ANNOTATION,
    standard_annotation_field_names,
)
from fastmcp_extensions.decorators import (
    _REGISTERED_PROMPTS,
    _REGISTERED_PROVIDERS,
    _REGISTERED_RESOURCES,
    _REGISTERED_TOOLS,
    _normalize_mcp_module,
    prepare_stateful_tool,
)
from fastmcp_extensions.session_state import (
    DecodedSessionState,
    ToolStateBase,
    current_principal,
    inspect_session_state,
)
from fastmcp_extensions.tool_traits import (
    Capability,
    ToolTraits,
    set_tool_traits,
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


def _split_annotations(
    annotations: Mapping[str, Any],
) -> tuple[ToolAnnotations | None, dict[str, Any]]:
    """Partition annotation keys into standard `ToolAnnotations` fields and extras.

    `mcp` 2.x `ToolAnnotations` no longer retains unknown keys, so keys that
    are not standard annotation fields (accepting both the snake_case field
    names and their camelCase wire aliases) must travel in `meta` instead.
    Returns a `(standard_annotations, meta_extras)` pair where
    `standard_annotations` is `None` when no standard keys were present.
    """
    standard_keys = set(standard_annotation_field_names())
    standard = {
        key: value for key, value in annotations.items() if key in standard_keys
    }
    extras = {
        key: value for key, value in annotations.items() if key not in standard_keys
    }
    return (ToolAnnotations(**standard) if standard else None, extras)


def _constant(value: Any) -> Callable[[], Any]:
    """Return a zero-argument dependency factory yielding `value`."""

    def _factory() -> Any:
        return value

    return _factory


def _exclude_parameters(
    callable_fn: Callable[..., Any],
    exclude_args: Sequence[str],
) -> Callable[..., Any]:
    """Rebind `callable_fn`'s signature so excluded params resolve via `Depends`.

    FastMCP 4 removed the `exclude_args` tool kwarg; exclusion is emulated by
    making each excluded parameter a `Depends(_constant(default))` injection,
    which hides it from the tool schema while the original default is still
    delivered at call time. Excluded parameters must declare a default.
    """
    signature = inspect.signature(callable_fn)
    parameters = list(signature.parameters.values())
    excluded = [param for param in parameters if param.name in exclude_args]
    if not excluded:
        return callable_fn
    for param in excluded:
        if param.default is inspect.Parameter.empty:
            raise ValueError(
                f"register_mcp_tools: cannot exclude parameter '{param.name}' "
                f"of {getattr(callable_fn, '__name__', callable_fn)!r}: excluded "
                "parameters must declare a default value so it can be injected "
                "via a Depends() factory."
            )
    rebound_parameters = [
        param.replace(default=Depends(_constant(param.default)))
        if param.name in exclude_args
        else param
        for param in parameters
    ]

    @functools.wraps(callable_fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return callable_fn(*args, **kwargs)

    wrapper.__signature__ = signature.replace(  # ty: ignore[unresolved-attribute]  # functools.wraps callables accept a __signature__ override.
        parameters=rebound_parameters
    )
    return wrapper


class _ProviderToolAnnotations(Transform):
    """Transform that fills missing annotations on provider-sourced tools.

    Also records the provider's `mcp_module` / `requiresClientFilesystem`
    traits for each tool in the in-process traits registry — custom keys
    never reach the wire.
    """

    def __init__(self, app: FastMCP, annotations: dict[str, Any]) -> None:
        self._app = app
        annotations = dict(annotations)
        provider_requires = frozenset(annotations.pop(TOOL_REQUIRES_KEY, None) or ())
        self._traits = ToolTraits(
            mcp_module=annotations.pop(ANNOTATION_MCP_MODULE, None),
            required_capabilities=provider_requires,
        )
        self._standard_annotations, extras = _split_annotations(annotations)
        if extras:
            raise ValueError(
                "mcp_provider: unsupported annotation key(s) "
                f"{sorted(extras)}; MCP 2 ToolAnnotations only accepts the "
                "standard hints."
            )

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._apply_annotations(tool) for tool in tools]

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None:
            return None
        return self._apply_annotations(tool)

    def _apply_annotations(self, tool: Tool) -> Tool:
        tool_annotations = (
            tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
        )
        default_annotations = (
            self._standard_annotations.model_dump(exclude_none=True)
            if self._standard_annotations is not None
            else {}
        )
        merged_annotations = default_annotations
        merged_annotations.update(tool_annotations)
        tool_annotations_type = next(
            annotation_type
            for annotation_type in get_args(
                type(tool).model_fields["annotations"].annotation
            )
            if annotation_type is not type(None)
        )
        required_capabilities = self._traits.required_capabilities
        if (tool.meta or {}).get(UI_META_KEY):
            required_capabilities = required_capabilities | {Capability.UI}
        set_tool_traits(
            self._app,
            tool.name,
            ToolTraits(
                mcp_module=self._traits.mcp_module,
                required_capabilities=required_capabilities,
            ),
        )
        return tool.model_copy(
            update={"annotations": tool_annotations_type(**merged_annotations)}
        )


def _register_state_inspection_tool(
    app: FastMCP,
    state_types: set[type[ToolStateBase]],
) -> None:
    config = getattr(app, "x_mcp_extensions_session_state", None)
    if config is None or not config.enable_state_inspection_tool:
        return
    if getattr(app, "_fastmcp_extensions_state_inspection_tool", False):
        return

    @app.tool(
        name="get_decoded_state",
        description=(
            "Inspect an encoded session-state handle, including its state fields, "
            "type, expiry, signing, key ID, and remaining lifetime."
        ),
    )
    def get_decoded_state(encoded_session_state: str) -> DecodedSessionState:
        principal = current_principal(required=config.principal_binding)
        return inspect_session_state(
            encoded_session_state,
            config,
            state_types,
            principal=principal,
        )

    app._fastmcp_extensions_state_inspection_tool = True  # ty: ignore[unresolved-attribute]  # FastMCP does not declare extension attributes.


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
        if ann.get(ANNOTATION_MCP_MODULE) == mcp_module_str
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

    state_types: set[type[ToolStateBase]] = set()

    def _register_fn(
        app: FastMCP,
        callable_fn: Callable[..., Any],
        annotations: dict[str, Any],
    ) -> None:
        registration_annotations = dict(annotations)
        state_type = registration_annotations.pop(WITH_STATE_ANNOTATION, None)
        tool_meta = dict(registration_annotations.pop(TOOL_META_KEY, None) or {})
        tool_app = registration_annotations.pop(TOOL_APP_KEY, None)
        traits = ToolTraits(
            mcp_module=registration_annotations.pop(ANNOTATION_MCP_MODULE, None),
            required_capabilities=frozenset(
                registration_annotations.pop(TOOL_REQUIRES_KEY, None) or ()
            ),
        )
        if state_type is not None:
            state_types.add(state_type)
            callable_fn = prepare_stateful_tool(callable_fn, state_type, app)
        if exclude_args:
            callable_fn = _exclude_parameters(callable_fn, exclude_args)

        standard_annotations, extras = _split_annotations(registration_annotations)
        if extras:
            raise ValueError(
                "register_mcp_tools: unsupported annotation key(s) "
                f"{sorted(extras)} on "
                f"{getattr(callable_fn, '__name__', callable_fn)!r}; MCP 2 "
                "ToolAnnotations only accepts the standard hints. "
                "Use meta= for custom wire metadata."
            )
        app.tool(
            callable_fn,
            annotations=standard_annotations,
            meta=tool_meta or None,
            app=tool_app,
        )
        set_tool_traits(
            app,
            getattr(callable_fn, "__name__", str(callable_fn)),
            traits,
        )

    _register_mcp_callables(
        app=app,
        mcp_module=mcp_module,
        resource_list=_REGISTERED_TOOLS,
        register_fn=_register_fn,
    )

    matching_providers = [
        (provider_factory, provider_annotations)
        for provider_factory, provider_annotations in _REGISTERED_PROVIDERS
        if provider_annotations.get(ANNOTATION_MCP_MODULE)
        == _normalize_mcp_module(mcp_module)
    ]

    for provider_factory, provider_annotations in matching_providers:
        provider = provider_factory()
        provider.add_transform(_ProviderToolAnnotations(app, provider_annotations))
        app.add_provider(provider)

    if state_types:
        registered_state_types = getattr(
            app,
            "_fastmcp_extensions_state_types",
            set(),
        )
        registered_state_types.update(state_types)
        app._fastmcp_extensions_state_types = registered_state_types  # ty: ignore[unresolved-attribute]  # FastMCP does not declare extension attributes.
        _register_state_inspection_tool(app, registered_state_types)


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
