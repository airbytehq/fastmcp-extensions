# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Derived span attributes for tool-call tracing.

Pure functions that describe a tool call without recording its content: the
error category, argument names, validation failures, result shape, the tool's
contract fingerprint, and eval tags.

Every function returns attribute *suffixes* (for example `error.category`);
`middleware` adds the attribute prefix. An attribute with nothing to say is
omitted. No function reads an exception message, an argument value, or result
content, and none raises for any input a tool call can produce; `middleware`
still guards each call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import typing
from collections.abc import Callable, Collection, Mapping, Sequence
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union, get_args, get_origin

from fastmcp.exceptions import ToolError, ValidationError

from fastmcp_extensions.user_facing_errors import UserFacingErrorMiddleware

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.tools import Tool, ToolResult

# category -> fault. The keys are the closed set of `error.category` values.
ERROR_FAULTS: Mapping[str, str] = {
    "invalid_arguments": "caller",
    "unknown_tool": "caller",
    "user_error": "caller",
    "auth": "caller",
    "not_found": "caller",
    "rate_limited": "upstream",
    "upstream_error": "upstream",
    "upstream_unreachable": "upstream",
    "upstream_timeout": "upstream",
    "timeout": "unknown",
    "cancelled": "unknown",
    "tool_error": "unknown",
    "internal": "server",
}
MAX_LIST_ITEMS = 16
OTHER = "<other>"
_MAX_CHAIN = 5
_MAX_INVALID = 5
_STATUS_CATEGORIES = {401: "auth", 403: "auth", 404: "not_found", 429: "rate_limited"}
_SAFE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,63}")
_SAFE_TYPE = re.compile(r"[a-z_]{1,64}")
_EVAL_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_EVAL_HEADERS = {"eval.run_id": "x-mcp-eval-run", "eval.case_id": "x-mcp-eval-case"}

ContractCache = dict[str, tuple[Any, str, int]]


def user_facing_error_types(app: FastMCP) -> tuple[type[BaseException], ...]:
    """Return the exception types `app` converts into concise client errors."""
    return tuple(
        error_type
        for middleware in app.middleware
        if isinstance(middleware, UserFacingErrorMiddleware)
        for error_type in middleware._error_types
    )


def _chain(exc: BaseException) -> list[BaseException]:
    """Return `exc` and up to four chained causes, following `raise ... from` first."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < _MAX_CHAIN:
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def _status_code(exc: BaseException) -> int | None:
    try:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code is None:
            code = getattr(exc, "status_code", None)
    except Exception:
        return None
    if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
        return code
    return None


def _network_category(exc: BaseException) -> str | None:
    names = [cls.__name__ for cls in type(exc).__mro__]
    timeouts = [name for name in names if "Timeout" in name]
    if timeouts:
        # Only the builtin / asyncio `TimeoutError`: a local deadline, not an HTTP client.
        return "timeout" if set(timeouts) == {"TimeoutError"} else "upstream_timeout"
    if any("ConnectionError" in name or "ConnectError" in name for name in names):
        return "upstream_unreachable"
    return None


def error_attributes(
    cause: BaseException | None,
    *,
    unknown_tool: bool = False,
    user_facing_errors: tuple[type[BaseException], ...] = (),
    classifier: Callable[[BaseException], str | None] | None = None,
) -> dict[str, object]:
    """Classify a failed call.

    `cause` is the unwrapped exception, or `None` for a returned error.
    `unknown_tool` is true when FastMCP itself rejected the tool name.

    A `classifier` result wins when it is a known category. The type rules
    look at `cause` only; the status-code and class-name rules also walk its
    chain. A tool-filter rejection is a bare `ValueError`, so it is `internal`.
    """
    if cause is None:
        return {
            "error.category": "tool_error",
            "error.fault": ERROR_FAULTS["tool_error"],
        }
    chain = _chain(cause)
    status = next((s for s in map(_status_code, chain) if s is not None), None)
    category: str | None = None
    if classifier is not None:
        try:
            chosen = classifier(cause)
        except (Exception, asyncio.CancelledError):
            # A faulty classifier is ignored; the built-in category applies.
            chosen = None
        if isinstance(chosen, str) and chosen in ERROR_FAULTS:
            category = chosen
    if category is None:
        if isinstance(cause, asyncio.CancelledError):
            category = "cancelled"
        elif unknown_tool:
            category = "unknown_tool"
        elif isinstance(cause, ValidationError):
            category = "invalid_arguments"
        elif user_facing_errors and isinstance(cause, user_facing_errors):
            category = "user_error"
        elif status is not None:
            category = _STATUS_CATEGORIES.get(status, "upstream_error")
        else:
            category = next(
                (c for c in map(_network_category, chain) if c is not None),
                "tool_error" if isinstance(cause, ToolError) else "internal",
            )
    attrs: dict[str, object] = {
        "error.category": category,
        "error.fault": ERROR_FAULTS[category],
    }
    if status is not None:
        attrs["upstream.status_code"] = status
    return attrs


def declared_parameters(tool: Tool) -> Mapping[str, object]:
    """Return the `properties` of a tool's input schema: its client-supplied parameters."""
    properties = tool.parameters.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def safe_name(name: object, declared: Collection[str] = ()) -> str:
    """Return an agent-supplied name if it is declared or looks like an identifier, else `OTHER`."""
    if isinstance(name, str) and (name in declared or _SAFE_NAME.fullmatch(name)):
        return name
    return OTHER


def argument_name_attributes(
    arguments: Mapping[str, object] | None, tool: Tool | None
) -> dict[str, object]:
    """Return `args.supplied` and `args.unknown` for a call to a known tool."""
    if tool is None or not arguments:
        return {}
    declared = declared_parameters(tool)
    attrs: dict[str, object] = {}
    supplied = sorted(name for name in arguments if name in declared)
    if supplied:
        attrs["args.supplied"] = tuple(supplied[:MAX_LIST_ITEMS])
    if not tool.parameters.get("additionalProperties"):
        unknown = sorted(
            {safe_name(name) for name in arguments if name not in declared}
        )
        if unknown:
            attrs["args.unknown"] = tuple(unknown[:MAX_LIST_ITEMS])
    return attrs


def validation_attributes(
    cause: BaseException | None, tool: Tool | None
) -> dict[str, object]:
    """Return `args.invalid` for an argument-validation failure."""
    if not isinstance(cause, ValidationError):
        return {}
    errors = getattr(cause.__cause__, "errors", None)
    if not callable(errors):
        return {}
    declared = declared_parameters(tool) if tool is not None else ()
    invalid: list[str] = []
    for error in errors(include_input=False, include_url=False, include_context=False):
        loc, kind = error.get("loc") or (None,), error.get("type")
        kind = kind if isinstance(kind, str) and _SAFE_TYPE.fullmatch(kind) else OTHER
        item = f"{safe_name(loc[0], declared)}:{kind}"
        if item not in invalid:
            invalid.append(item)
    return {"args.invalid": tuple(invalid[:_MAX_INVALID])} if invalid else {}


def result_attributes(result: ToolResult) -> dict[str, object]:
    """Describe the shape of a tool result without recording any of its content."""
    types: set[str] = set()
    text_chars = 0
    for block in result.content:
        kind = getattr(block, "type", None)
        types.add(
            kind if isinstance(kind, str) and _SAFE_TYPE.fullmatch(kind) else OTHER
        )
        text = getattr(block, "text", None)
        if kind == "text" and isinstance(text, str):
            text_chars += len(text)
    attrs: dict[str, object] = {
        "result.content_count": len(result.content),
        "result.text_chars": text_chars,
    }
    if types:
        attrs["result.content_types"] = tuple(sorted(types)[:MAX_LIST_ITEMS])
    structured = result.structured_content
    if structured is not None:
        attrs["result.structured_chars"] = len(
            json.dumps(
                structured, separators=(",", ":"), ensure_ascii=False, default=str
            )
        )
        lists = [len(v) for v in structured.values() if isinstance(v, list)]
        if lists:
            attrs["result.item_count"] = max(lists)
    return attrs


def literal_error_strings(func: Callable[..., object] | None) -> frozenset[str]:
    """Return every `str` inside a `Literal[...]` of a function's return annotation."""
    if func is None:
        return frozenset()
    try:
        hint = typing.get_type_hints(func, include_extras=True).get("return")
    except Exception:
        return frozenset()
    if get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    members = get_args(hint) if get_origin(hint) in (Union, UnionType) else (hint,)
    found: set[str] = set()
    for member in members:
        if get_origin(member) is Literal:
            found.update(value for value in get_args(member) if isinstance(value, str))
    return frozenset(found)


def is_error_like(result: ToolResult, errors: Collection[str]) -> bool:
    """Return whether a result is exactly one of the tool's declared error strings."""
    if not errors:
        return False
    structured = result.structured_content
    if isinstance(structured, dict) and structured.keys() == {"result"}:
        value = structured["result"]
        if isinstance(value, str) and value in errors:
            return True
    content = result.content
    return (
        len(content) == 1
        and getattr(content[0], "type", None) == "text"
        and getattr(content[0], "text", None) in errors
    )


def tool_contract(tool: Tool, cache: ContractCache | None = None) -> tuple[str, int]:
    """Return `(fingerprint, schema_chars)` for a tool's agent-visible contract."""
    if cache is not None:
        hit = cache.get(tool.key)
        if hit is not None and hit[0] is tool:
            return hit[1], hit[2]
    annotations = tool.annotations
    canonical = json.dumps(
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema,
            "annotations": (
                annotations.model_dump(mode="json", exclude_none=True)
                if annotations is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    if cache is not None:
        cache[tool.key] = (tool, fingerprint, len(canonical))
    return fingerprint, len(canonical)


def tool_list_attributes(
    tools: Sequence[Tool], cache: ContractCache | None = None
) -> dict[str, object]:
    """Return the `tools.*` attributes for one `tools/list` response."""
    contracts = [tool_contract(tool, cache) for tool in tools]
    joined = "\n".join(sorted(fingerprint for fingerprint, _ in contracts))
    return {
        "tools.count": len(contracts),
        "tools.schema_chars": sum(chars for _, chars in contracts),
        "tools.set_fingerprint": hashlib.sha256(joined.encode()).hexdigest()[:16],
    }


def eval_attributes(headers: Mapping[str, str]) -> dict[str, object]:
    """Return `eval.run_id` / `eval.case_id` from lower-cased request headers."""
    attrs: dict[str, object] = {}
    for key, header in _EVAL_HEADERS.items():
        value = headers.get(header)
        if isinstance(value, str) and _EVAL_VALUE.fullmatch(value):
            attrs[key] = value
    return attrs
