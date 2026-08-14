# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Tests for stateless MCP capability propagation."""

import asyncio
import uuid
from typing import Any

import pytest
from fastmcp import FastMCP
from mcp.types import Tool, ToolAnnotations

import fastmcp_extensions.capability_tokens as capability_tokens
import fastmcp_extensions.tool_filters as tool_filters
from fastmcp_extensions import (
    CapabilityTokenMiddleware,
    RejectEventStreamGetMiddleware,
    client_declared_extensions_from_headers,
    client_supports_extension,
    decode_capability_token,
    encode_capability_token,
    extension_tool_filter,
)


@pytest.mark.parametrize(
    ("extension_ids", "expected"),
    [
        pytest.param({"one"}, {"one"}, id="single-extension"),
        pytest.param({"one", "two"}, {"one", "two"}, id="multiple-extensions"),
        pytest.param(set(), set(), id="empty-set"),
        pytest.param({"has whitespace"}, set(), id="whitespace-is-dropped"),
        pytest.param({" \t"}, set(), id="all-whitespace-is-dropped"),
    ],
)
def test_capability_token_round_trip(
    extension_ids: set[str],
    expected: set[str],
) -> None:
    """Capability tokens round-trip usable extension IDs."""
    token = encode_capability_token(extension_ids)
    assert decode_capability_token(token) == expected
    if not expected:
        assert token == ""


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("garbage", id="garbage"),
        pytest.param(f"{uuid.uuid4().hex}.not-base64!", id="non-base64-payload"),
        pytest.param(
            "00000000000000000000000000000000.aW8",
            id="non-uuid4-with-valid-payload",
        ),
        pytest.param("00000000-0000-0000-0000-000000000000", id="uuid-only"),
        pytest.param("", id="empty"),
    ],
)
def test_decode_capability_token_fails_closed(token: str) -> None:
    """Malformed capability tokens never expose extensions or raise."""
    assert decode_capability_token(token) == set()


def test_client_declared_extensions_union_token_and_fallback_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token and fallback-header declarations are combined."""
    token = encode_capability_token({"roots"})
    monkeypatch.setattr(
        capability_tokens,
        "get_http_headers",
        lambda **_: {
            "mcp-session-id": token,
            "x-mcp-extensions": "ui, another",
        },
    )

    assert client_declared_extensions_from_headers() == {"roots", "ui", "another"}


def test_client_supports_extension_checks_session_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convenience resolver checks FastMCP session capabilities and headers."""

    class Context:
        def client_supports_extension(self, extension_id: str) -> bool:
            return extension_id == "session"

    monkeypatch.setattr(capability_tokens, "get_context", lambda: Context())
    monkeypatch.setattr(
        capability_tokens,
        "get_http_headers",
        lambda **_: {"x-mcp-extensions": "header"},
    )

    assert client_supports_extension("session") is True
    assert client_supports_extension("header") is True
    assert client_supports_extension("missing") is False


async def _run_capability_middleware(
    messages: list[dict[str, object]],
    *,
    request_scope: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    received: list[dict[str, object]] = []
    responses: list[dict[str, object]] = []
    message_index = 0

    async def receive() -> Any:
        nonlocal message_index
        message = messages[message_index]
        message_index += 1
        return message

    async def send(message: dict[str, object]) -> None:
        responses.append(message)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        del scope
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})

    middleware = CapabilityTokenMiddleware(app)
    await middleware(
        request_scope or {"type": "http", "method": "POST"},
        receive,
        send,
    )
    return received, responses


def test_capability_middleware_mints_token_for_initialize() -> None:
    """An initialize declaration is replayed and returned as a session token."""
    body = b'{"method":"initialize","params":{"capabilities":{"extensions":{"ui":{}}}}}'
    received, responses = asyncio.run(
        _run_capability_middleware(
            [{"type": "http.request", "body": body, "more_body": False}]
        )
    )

    assert b"".join(message.get("body", b"") for message in received) == body
    headers = dict(responses[0].get("headers", []))
    assert decode_capability_token(headers[b"mcp-session-id"].decode()) == {"ui"}


def test_capability_middleware_does_not_mint_without_extensions() -> None:
    """Requests without usable extensions receive no session token."""
    body = b'{"method":"initialize","params":{"capabilities":{"extensions":{}}}}'
    _, responses = asyncio.run(
        _run_capability_middleware(
            [{"type": "http.request", "body": body, "more_body": False}]
        )
    )

    assert b"mcp-session-id" not in dict(responses[0].get("headers", []))


def test_capability_middleware_forwards_oversized_body() -> None:
    """Oversized bodies remain byte-identical and do not mint tokens."""
    body = b"x" * (capability_tokens._MAX_INITIALIZE_BODY_BYTES + 1)
    received, responses = asyncio.run(
        _run_capability_middleware(
            [
                {"type": "http.request", "body": body[:1024], "more_body": True},
                {"type": "http.request", "body": body[1024:], "more_body": False},
            ]
        )
    )

    assert b"".join(message.get("body", b"") for message in received) == body
    assert b"mcp-session-id" not in dict(responses[0].get("headers", []))


def test_capability_middleware_forwards_disconnect() -> None:
    """A mid-body disconnect is forwarded without minting or raising."""
    received, responses = asyncio.run(
        _run_capability_middleware(
            [
                {
                    "type": "http.request",
                    "body": b'{"method":"initialize"',
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ]
        )
    )

    assert received[-1]["type"] == "http.disconnect"
    assert b"mcp-session-id" not in dict(responses[0].get("headers", []))


def test_extension_tool_filter_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory gates annotated tools and leaves others visible."""
    tool = Tool(
        name="tool",
        description="tool",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    filter_tool = extension_tool_filter("ui", "readOnlyHint")
    monkeypatch.setattr(tool_filters, "client_supports_extension", lambda _: False)
    app = FastMCP("test")
    assert filter_tool(tool, app) is False
    assert extension_tool_filter("ui", "missing")(tool, app) is True


@pytest.mark.parametrize(
    ("accept", "expected_status"),
    [
        pytest.param(b"text/event-stream", 405, id="sse-get-rejected"),
        pytest.param(b"text/html", 200, id="browser-get-passes-through"),
    ],
)
def test_event_stream_get_negotiation(accept: bytes, expected_status: int) -> None:
    """SSE GETs are rejected while browser GETs pass through."""
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})

    middleware = RejectEventStreamGetMiddleware(app)
    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "headers": [(b"accept", accept)]},
            receive,
            send,
        )
    )

    assert messages[0]["status"] == expected_status
