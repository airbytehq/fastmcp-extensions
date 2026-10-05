# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Table tests for the derived tracing attributes."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent

from fastmcp_extensions._tracing import OWNED_NAMESPACES
from fastmcp_extensions._tracing_extras import (
    ContractCache,
    argument_name_attributes,
    error_attributes,
    result_attributes,
    tool_contract,
    tool_list_attributes,
    validation_attributes,
)


def _owned(attrs: dict[str, object]) -> dict[str, object]:
    """Return `attrs`, checked to be keys that hooks and tools cannot overwrite."""
    assert all(key.split(".", 1)[0] in OWNED_NAMESPACES for key in attrs)
    return attrs


class _StatusError(Exception):
    def __init__(self, code: object, *, on_response: bool = False) -> None:
        if on_response:
            self.response = SimpleNamespace(status_code=code)
        else:
            self.status_code = code


class ReadTimeout(Exception):  # noqa: N818  # Named like an HTTP client's timeout.
    pass


def _caused_by(cause: BaseException) -> RuntimeError:
    error = RuntimeError()
    error.__cause__ = cause
    return error


def _cancels(_: BaseException) -> str:
    raise asyncio.CancelledError


USER_FACING = {"user_facing_errors": (KeyError,)}
# (cause, keyword arguments, (category, fault[, upstream status]))
ERROR_CASES: list[tuple[BaseException | None, dict[str, Any], tuple[Any, ...]]] = [
    (None, {}, ("tool_error", "unknown")),
    (asyncio.CancelledError(), {}, ("cancelled", "unknown")),
    (NotFoundError(), {"unknown_tool": True}, ("unknown_tool", "caller")),
    (NotFoundError(), {}, ("internal", "server")),
    (ValidationError(), {}, ("invalid_arguments", "caller")),
    (KeyError(), USER_FACING, ("user_error", "caller")),
    (_caused_by(KeyError()), USER_FACING, ("internal", "server")),
    (_StatusError(401), {}, ("auth", "caller", 401)),
    (_StatusError(403, on_response=True), {}, ("auth", "caller", 403)),
    (_StatusError(404), {}, ("not_found", "caller", 404)),
    (_StatusError(429, on_response=True), {}, ("rate_limited", "upstream", 429)),
    (_StatusError(500), {}, ("upstream_error", "upstream", 500)),
    (_caused_by(_StatusError(503)), {}, ("upstream_error", "upstream", 503)),
    (_StatusError(True), {}, ("internal", "server")),
    (ReadTimeout(), {}, ("upstream_timeout", "upstream")),
    (TimeoutError(), {}, ("timeout", "unknown")),
    (ConnectionRefusedError(), {}, ("upstream_unreachable", "upstream")),
    (ToolError(), {}, ("tool_error", "unknown")),
    (RuntimeError(), {}, ("internal", "server")),
    (RuntimeError(), {"classifier": lambda _: "auth"}, ("auth", "caller")),
    (RuntimeError(), {"classifier": lambda _: "nonsense"}, ("internal", "server")),
    (RuntimeError(), {"classifier": lambda _: 1 / 0}, ("internal", "server")),
    (RuntimeError(), {"classifier": _cancels}, ("internal", "server")),
]


@pytest.mark.parametrize(("cause", "kwargs", "expected"), ERROR_CASES)
def test_error_attributes(
    cause: BaseException | None, kwargs: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    keys = ("error.category", "error.fault", "upstream.status_code")
    assert _owned(error_attributes(cause, **kwargs)) == dict(zip(keys, expected))


def _app() -> FastMCP:
    app = FastMCP("t")

    @app.tool
    def query(sql: str, limit: int = 10) -> str:
        return sql

    @app.tool
    def ping() -> str:
        return "pong"

    return app


UNEXPECTED = "unexpected_keyword_argument"
ARGUMENT_CASES: dict[str, tuple[dict[str, Any], dict[str, object]]] = {
    "wrong-type-and-missing": (
        {"limit": "x"},
        {
            "args.supplied": ("limit",),
            "args.invalid": ("sql:missing_argument", "limit:int_parsing"),
        },
    ),
    "invented-names": (
        {"sql": "s", "customer": 1, "Customer Name!": 2},
        {
            "args.supplied": ("sql",),
            "args.unknown": ("<other>", "customer"),
            "args.invalid": (f"customer:{UNEXPECTED}", f"<other>:{UNEXPECTED}"),
        },
    ),
    "more-than-five-errors": (
        {f"x{n}": 1 for n in range(7)},
        {
            "args.unknown": tuple(f"x{n}" for n in range(7)),
            "args.invalid": (
                "sql:missing_argument",
                *(f"x{n}:{UNEXPECTED}" for n in range(4)),
            ),
        },
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ARGUMENT_CASES)
async def test_argument_names_and_validation_detail(case: str) -> None:
    arguments, expected = ARGUMENT_CASES[case]
    tool = await _app().get_tool("query")
    with pytest.raises(ValidationError) as raised:
        await tool.run(arguments)
    attrs = {
        **argument_name_attributes(arguments, tool),
        **validation_attributes(raised.value, tool),
    }
    assert _owned(attrs) == expected
    assert argument_name_attributes(arguments, None) == {}
    assert validation_attributes(raised.value.__cause__, tool) == {}


TEXT = TextContent(type="text", text="hi")
IMAGE = ImageContent(type="image", data="aGk=", mimeType="image/png")
RESULT_KEYS = ("content_count", "text_chars", "content_types")
STRUCTURED_KEYS = ("structured_chars", "item_count")
# (result, values of `RESULT_KEYS` then `STRUCTURED_KEYS`; `None` means omitted)
RESULT_CASES: list[tuple[ToolResult, tuple[Any, ...]]] = [
    (ToolResult(content="hello"), (1, 5, ("text",), None, None)),
    (
        ToolResult(content="ok", structured_content={"a": [1, 2, 3], "b": [1], "n": 1}),
        (1, 2, ("text",), 27, 3),
    ),
    (ToolResult(content="ok", structured_content={"n": 1}), (1, 2, ("text",), 7, None)),
    (ToolResult(content=[TEXT, IMAGE]), (2, 2, ("image", "text"), None, None)),
    (ToolResult(content=[]), (0, 0, None, None, None)),
]


@pytest.mark.parametrize(("result", "expected"), RESULT_CASES)
def test_result_attributes(result: ToolResult, expected: tuple[Any, ...]) -> None:
    keys = (f"result.{key}" for key in RESULT_KEYS + STRUCTURED_KEYS)
    assert _owned(result_attributes(result)) == {
        key: value for key, value in zip(keys, expected) if value is not None
    }


@pytest.mark.asyncio
async def test_tool_contract_and_tool_list_fingerprints() -> None:
    app = _app()
    query = await app.get_tool("query")
    fingerprint, chars = tool_contract(query)
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint)
    assert chars > 0

    cache: ContractCache = {}
    assert tool_contract(query, cache) == (fingerprint, chars)
    # The same key holding a different object is recomputed, never stale.
    edited = query.model_copy(update={"description": "changed"})
    assert tool_contract(edited, cache)[0] != fingerprint

    tools = list(await app.list_tools())
    attrs = _owned(tool_list_attributes(tools))
    assert attrs == tool_list_attributes(tools[::-1])
    assert attrs["tools.count"] == 2
    assert attrs["tools.schema_chars"] == sum(tool_contract(tool)[1] for tool in tools)
    assert re.fullmatch(r"[0-9a-f]{16}", str(attrs["tools.set_fingerprint"]))
