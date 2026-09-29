# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Tests for stateless MCP capability propagation."""

import asyncio
import base64
import json
import uuid
from typing import Any

import pytest
from fastmcp import FastMCP
from mcp.types import ClientCapabilities, Tool, ToolAnnotations

import fastmcp_extensions.capability_tokens as capability_tokens
import fastmcp_extensions.tool_filters as tool_filters
from fastmcp_extensions import (
    CapabilityTokenMiddleware,
    RejectEventStreamGetMiddleware,
    SessionToken,
    client_declared_extensions_from_headers,
    client_supports_extension,
    decode_capability_token,
    decode_session_token,
    encode_capability_token,
    encode_session_token,
    extension_tool_filter,
    interactive_ui_filter,
    minted_session_token,
    session_token_from_headers,
)
from fastmcp_extensions.tool_filters import (
    STANDARD_TOOL_FILTERS,
    capability_filter,
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


def _b64(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _raw_token(payload: bytes) -> str:
    return f"{uuid.uuid4().hex}.{_b64(payload)}"


def _v2_raw_token(extensions: str, metadata: bytes) -> str:
    return _raw_token(f"{extensions} ~meta:{_b64(metadata)}".encode())


def test_session_token_round_trip() -> None:
    """V2 tokens carry extensions, client info, and protocol version."""
    token = encode_session_token(
        extensions={"ui", "has whitespace"},
        client_name="Claude Desktop",
        client_version="1.2.3",
        protocol_version="2025-06-18",
    )

    assert decode_session_token(token) == SessionToken(
        extensions=frozenset({"ui"}),
        client_name="Claude Desktop",
        client_version="1.2.3",
        protocol_version="2025-06-18",
    )
    assert decode_capability_token(token) == {"ui"}
    assert token.isascii() and token.isprintable() and " " not in token


def test_session_token_is_minted_without_any_declarations() -> None:
    """An empty v2 token still decodes, unlike an empty v1 token."""
    token = encode_session_token()

    assert token
    assert decode_session_token(token) == SessionToken()
    assert decode_capability_token(token) == set()


def test_session_token_cleans_client_fields() -> None:
    """Client fields drop non-printable characters and are length-capped."""
    token = encode_session_token(
        client_name=" Evil\r\nClient\x00 ",
        client_version="9" * 500,
        protocol_version="   ",
    )

    decoded = decode_session_token(token)

    assert decoded is not None
    assert decoded.client_name == "EvilClient"
    assert decoded.client_version == "9" * 128
    assert decoded.protocol_version is None


def test_decode_session_token_cleans_untrusted_payload_fields() -> None:
    """Hand-crafted v2 payloads are cleaned the same way as minted ones."""
    metadata = {
        "v": 2,
        "client_name": {"nested": True},
        "client_version": "x" * 500,
        "protocol_version": 20250618,
    }
    token = _v2_raw_token("ui", json.dumps(metadata).encode())

    assert decode_session_token(token) == SessionToken(
        extensions=frozenset({"ui"}),
        client_version="x" * 128,
    )


def test_v2_token_extensions_readable_by_v1_decoders() -> None:
    """Pre-v2 replicas split the payload on whitespace and still find extensions."""
    token = encode_session_token(
        extensions={"ui", "roots"}, client_name="Claude Desktop", client_version="1"
    )
    payload = token.split(".", maxsplit=1)[1]
    v1_view = set(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split()
    )

    assert {"ui", "roots"} <= v1_view
    assert len(v1_view) == 3


def test_encode_session_token_drops_metadata_like_extension_ids() -> None:
    """Extension IDs cannot be mistaken for the metadata item."""
    token = encode_session_token(extensions={"ui", "~meta:spoof"})

    assert decode_session_token(token) == SessionToken(extensions=frozenset({"ui"}))


def test_decode_session_token_reads_v1_tokens() -> None:
    """Legacy v1 tokens decode to extensions with no client metadata."""
    token = encode_capability_token({"ui", "roots"})

    assert decode_session_token(token) == SessionToken(
        extensions=frozenset({"ui", "roots"})
    )


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("garbage", id="garbage"),
        pytest.param("", id="empty"),
        pytest.param(f"{uuid.uuid4().hex}.not-base64!", id="non-base64-payload"),
        pytest.param(_v2_raw_token("ui", b"{not json"), id="invalid-json"),
        pytest.param(_v2_raw_token("ui", b'{"v":3}'), id="unknown-version"),
        pytest.param(_v2_raw_token("ui", b"[2]"), id="non-object-metadata"),
        pytest.param(_raw_token(b"ui ~meta:!!"), id="non-base64-metadata"),
        pytest.param(
            _raw_token(f"ui ~meta:{_b64(b'{}')} ~meta:{_b64(b'{}')}".encode()),
            id="duplicate-metadata",
        ),
        pytest.param(_raw_token(b"   "), id="whitespace-v1-payload"),
    ],
)
def test_decode_session_token_fails_closed(token: str) -> None:
    """Invalid tokens decode to `None` and expose no extensions."""
    assert decode_session_token(token) is None
    assert decode_capability_token(token) == set()


def test_session_token_from_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The current request's `Mcp-Session-Id` header is decoded."""
    token = encode_session_token(client_name="Cursor", client_version="3.0")
    monkeypatch.setattr(
        capability_tokens,
        "get_http_headers",
        lambda **_: {"mcp-session-id": token},
    )
    assert session_token_from_headers() == SessionToken(
        client_name="Cursor", client_version="3.0"
    )

    monkeypatch.setattr(capability_tokens, "get_http_headers", lambda **_: {})
    assert session_token_from_headers() is None


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
    """The convenience resolver checks session capabilities and headers."""

    class Session:
        client_capabilities = ClientCapabilities(extensions={"session": {}})

    class Context:
        session = Session()

    monkeypatch.setattr(capability_tokens, "get_context", lambda: Context())
    monkeypatch.setattr(
        capability_tokens,
        "get_http_headers",
        lambda **_: {"x-mcp-extensions": "header"},
    )

    assert client_supports_extension("session") is True
    assert client_supports_extension("header") is True
    assert client_supports_extension("missing") is False


def test_client_supports_extension_reads_envelope_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-07-28 sessionless requests expose capabilities via the envelope.

    `session.client_params` is `None` here, so FastMCP's
    `Context.client_supports_extension` cannot see the declaration; the
    resolver reads `session.client_capabilities` directly.
    """

    class Session:
        client_capabilities = ClientCapabilities(
            extensions={"io.modelcontextprotocol/ui": {}}
        )

    class Context:
        session = Session()

    monkeypatch.setattr(capability_tokens, "get_context", lambda: Context())
    monkeypatch.setattr(capability_tokens, "get_http_headers", lambda **_: {})

    assert client_supports_extension("io.modelcontextprotocol/ui") is True

    class EmptySession:
        client_capabilities = None

    class EmptyContext:
        session = EmptySession()

    monkeypatch.setattr(capability_tokens, "get_context", lambda: EmptyContext())

    assert client_supports_extension("io.modelcontextprotocol/ui") is False


def test_client_supports_extension_tolerates_missing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sessionless contexts (e.g. `fastmcp inspect`) must not raise."""

    class Context:
        @property
        def session(self) -> object:
            raise RuntimeError("session is not available")

    monkeypatch.setattr(capability_tokens, "get_context", lambda: Context())
    monkeypatch.setattr(capability_tokens, "get_http_headers", lambda **_: {})

    assert client_supports_extension("io.modelcontextprotocol/ui") is False


app_scopes: list[Any] = []
"""Scopes the fake app saw during the last `_run_capability_middleware` call."""


async def _run_capability_middleware(
    messages: list[dict[str, object]],
    *,
    request_scope: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    received: list[dict[str, object]] = []
    responses: list[dict[str, object]] = []
    message_index = 0
    app_scopes.clear()

    async def receive() -> Any:
        nonlocal message_index
        message = messages[message_index]
        message_index += 1
        return message

    async def send(message: dict[str, object]) -> None:
        responses.append(message)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        app_scopes.append(scope)
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


def test_capability_middleware_mints_token_with_client_info() -> None:
    """Every initialize gets a token carrying client info, even without extensions."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"extensions": {}},
                "clientInfo": {"name": "Claude Code", "version": "2.1.0"},
            },
        }
    ).encode()
    _, responses = asyncio.run(
        _run_capability_middleware(
            [{"type": "http.request", "body": body, "more_body": False}]
        )
    )

    headers = dict(responses[0].get("headers", []))
    assert decode_session_token(headers[b"mcp-session-id"].decode()) == SessionToken(
        client_name="Claude Code",
        client_version="2.1.0",
        protocol_version="2025-06-18",
    )


def test_capability_middleware_exposes_minted_token_to_app() -> None:
    """The wrapped app can read the token it is about to receive in the response."""
    body = b'{"method":"initialize","params":{"clientInfo":{"name":"Cursor"}}}'
    _, responses = asyncio.run(
        _run_capability_middleware(
            [{"type": "http.request", "body": body, "more_body": False}]
        )
    )

    headers = dict(responses[0].get("headers", []))
    (scope,) = app_scopes
    assert minted_session_token(scope) == headers[b"mcp-session-id"].decode()


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param({}, id="no-state"),
        pytest.param({"state": None}, id="non-mapping-state"),
        pytest.param({"state": {}}, id="not-minted"),
    ],
)
def test_minted_session_token_absent(scope: dict[str, object]) -> None:
    """Requests that mint nothing report no token."""
    assert minted_session_token(scope) is None


def test_capability_middleware_mints_token_for_bare_initialize() -> None:
    """An initialize with no params still receives an empty session token."""
    _, responses = asyncio.run(
        _run_capability_middleware(
            [
                {
                    "type": "http.request",
                    "body": b'{"method":"initialize"}',
                    "more_body": False,
                }
            ]
        )
    )

    headers = dict(responses[0].get("headers", []))
    assert decode_session_token(headers[b"mcp-session-id"].decode()) == SessionToken()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"method":"tools/call","params":{}}', id="tools-call"),
        pytest.param(b"not json", id="non-json"),
        pytest.param(b'[{"method":"initialize"}]', id="batch"),
    ],
)
def test_capability_middleware_does_not_mint_for_other_requests(body: bytes) -> None:
    """Non-initialize requests keep their response headers untouched."""
    _, responses = asyncio.run(
        _run_capability_middleware(
            [{"type": "http.request", "body": body, "more_body": False}]
        )
    )

    assert b"mcp-session-id" not in dict(responses[0].get("headers", []))
    assert minted_session_token(app_scopes[0]) is None


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
    """The factory gates meta-marked tools and leaves others visible."""
    tool = Tool(
        name="tool",
        description="tool",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"my-ext": {"marker": True}},
    )
    filter_tool = extension_tool_filter("ui", "my-ext")
    monkeypatch.setattr(tool_filters, "client_supports_extension", lambda _: False)
    app = FastMCP("test")
    assert filter_tool(tool, app) is False
    assert extension_tool_filter("ui", "missing")(tool, app) is True


def test_standard_tool_filters_gate_interactive_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standard filters gate UI rendering without restricting unannotated tools."""
    annotated_tool = Tool(
        name="ui_tool",
        description="ui tool",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(),
        meta={"ui": {"resourceUri": "ui://test/x.html"}},
    )
    plain_tool = Tool(
        name="plain_tool",
        description="plain tool",
        inputSchema={"type": "object"},
    )
    app = FastMCP("test")

    def no_context() -> None:
        raise RuntimeError

    assert capability_filter in STANDARD_TOOL_FILTERS
    monkeypatch.setattr(capability_tokens, "get_context", no_context)
    monkeypatch.setattr(capability_tokens, "get_http_headers", lambda **_: {})

    assert interactive_ui_filter(annotated_tool, app) is False
    assert interactive_ui_filter(plain_tool, app) is True

    monkeypatch.setattr(
        capability_tokens,
        "get_http_headers",
        lambda **_: {"x-mcp-extensions": "io.modelcontextprotocol/ui"},
    )
    assert interactive_ui_filter(annotated_tool, app) is True


@pytest.mark.parametrize(
    ("accept", "path", "middleware_path", "expected_status", "inner_called"),
    [
        pytest.param(
            b"text/event-stream",
            "/mcp",
            "/mcp",
            405,
            False,
            id="scoped-sse-get-rejected",
        ),
        pytest.param(
            b"text/event-stream",
            "/other",
            "/mcp",
            200,
            True,
            id="scoped-other-path-passes-through",
        ),
        pytest.param(
            b"text/html",
            "/mcp/",
            "/mcp",
            200,
            True,
            id="scoped-browser-get-passes-through",
        ),
        pytest.param(
            b"text/event-stream",
            "/other",
            None,
            405,
            False,
            id="unscoped-sse-get-rejected",
        ),
    ],
)
def test_event_stream_get_negotiation(
    accept: bytes,
    path: str,
    middleware_path: str | None,
    expected_status: int,
    inner_called: bool,
) -> None:
    """Scoped rejection protects the MCP path while direct use stays unscoped."""
    messages: list[dict[str, object]] = []
    calls = 0

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal calls
        calls += 1
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b"<title>Airbyte MCP Server</title>",
            }
        )

    middleware = RejectEventStreamGetMiddleware(app, path=middleware_path)
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [(b"accept", accept)],
            },
            receive,
            send,
        )
    )

    assert messages[0]["status"] == expected_status
    assert calls == int(inner_called)
    if expected_status == 405:
        assert dict(messages[0]["headers"])[b"allow"] == b"POST, DELETE"
    else:
        assert messages[-1]["body"] == b"<title>Airbyte MCP Server</title>"
