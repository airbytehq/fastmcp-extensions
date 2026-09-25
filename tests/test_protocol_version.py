# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Tests for protocol-version negotiation middleware."""

import asyncio
import json
from typing import Any

import pytest
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from fastmcp_extensions import (
    UNSUPPORTED_PROTOCOL_VERSION_ERROR_CODE,
    ProtocolVersionNegotiationMiddleware,
)


def _headers(**headers: str) -> list[tuple[bytes, bytes]]:
    return [
        (name.encode("ascii"), value.encode("ascii")) for name, value in headers.items()
    ]


def _post_scope(
    path: str = "/mcp",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, object]:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
    }


def _run(
    middleware: ProtocolVersionNegotiationMiddleware,
    scope: dict[str, object],
    messages: list[dict[str, object]],
    inner_app: Any | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    received: list[dict[str, object]] = []
    responses: list[dict[str, object]] = []
    message_index = 0

    async def receive() -> Any:
        nonlocal message_index
        if message_index < len(messages):
            message = messages[message_index]
            message_index += 1
            return message
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        responses.append(message)

    async def default_app(scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    app = inner_app or default_app
    middleware.app = app
    asyncio.run(middleware(scope, receive, send))
    return received, responses


def _middleware(**kwargs: Any) -> ProtocolVersionNegotiationMiddleware:
    return ProtocolVersionNegotiationMiddleware(lambda *args: None, **kwargs)


def _response_body(responses: list[dict[str, object]]) -> bytes:
    return b"".join(
        r.get("body", b"") for r in responses if r["type"] == "http.response.body"
    )


def test_unsupported_version_answers_correlated_error() -> None:
    """An unsupported probe is answered with -32022 echoing the request id."""
    body = b'{"jsonrpc":"2.0","id":0,"method":"server/discover","params":{}}'
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    _, responses = _run(
        _middleware(),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2026-07-28"})),
        [{"type": "http.request", "body": body, "more_body": False}],
        inner_app=app,
    )

    assert not called
    assert responses[0]["status"] == 400
    payload = json.loads(_response_body(responses))
    assert payload["id"] == 0
    assert payload["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION_ERROR_CODE == -32022
    assert payload["error"]["data"]["requested"] == "2026-07-28"
    assert payload["error"]["data"]["supported"] == list(SUPPORTED_PROTOCOL_VERSIONS)


def test_string_request_id_is_echoed() -> None:
    """String ids echo back unchanged."""
    body = b'{"jsonrpc":"2.0","id":"abc","method":"server/discover"}'
    _, responses = _run(
        _middleware(),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2026-07-28"})),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert json.loads(_response_body(responses))["id"] == "abc"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="non-json"),
        pytest.param(b'{"jsonrpc":"2.0","method":"server/discover"}', id="no-id"),
        pytest.param(
            b'[{"jsonrpc":"2.0","id":0,"method":"server/discover"}]', id="batch"
        ),
        pytest.param(b"[" * 30_000 + b"]" * 30_000, id="deeply-nested"),
        pytest.param(b"x" * (64 * 1024 + 1), id="oversized"),
    ],
)
def test_uncorrelatable_bodies_answer_null_id(body: bytes) -> None:
    """Uncorrelatable bodies still get a 400 with an explicit null id."""
    messages = [
        {"type": "http.request", "body": body, "more_body": False},
    ]
    if len(body) > 1024:
        messages = [
            {"type": "http.request", "body": body[:1024], "more_body": True},
            {"type": "http.request", "body": body[1024:], "more_body": False},
        ]
    _, responses = _run(
        _middleware(),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2026-07-28"})),
        messages,
    )
    assert responses[0]["status"] == 400
    assert b'"id":null' in _response_body(responses)


def test_disconnect_still_answers_with_null_id() -> None:
    """A disconnected body read still yields the -32022 response."""
    _, responses = _run(
        _middleware(),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2026-07-28"})),
        [
            {"type": "http.request", "body": b'{"id":0', "more_body": True},
            {"type": "http.disconnect"},
        ],
    )
    assert responses[0]["status"] == 400
    assert b'"id":null' in _response_body(responses)


def test_supported_version_passes_through() -> None:
    """Supported versions reach the inner app with the body intact."""
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    received, responses = _run(
        _middleware(),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2025-11-25"})),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert b"".join(m.get("body", b"") for m in received) == body
    assert responses[0]["status"] == 200


def test_missing_version_header_passes_through() -> None:
    """Requests without the header are untouched."""
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    received, responses = _run(
        _middleware(),
        _post_scope(),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert b"".join(m.get("body", b"") for m in received) == body
    assert responses[0]["status"] == 200


def test_get_with_unsupported_version_passes_through() -> None:
    """Only POSTs are intercepted."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": _headers(**{"mcp-protocol-version": "2026-07-28"}),
    }
    _, responses = _run(_middleware(), scope, [])
    assert responses[0]["status"] == 200


def test_path_scoping_passes_other_paths_through() -> None:
    """A configured `path` limits interception to that endpoint."""
    body = b'{"jsonrpc":"2.0","id":0,"method":"server/discover"}'
    _, responses = _run(
        _middleware(path="/mcp"),
        _post_scope(
            path="/other",
            headers=_headers(**{"mcp-protocol-version": "2026-07-28"}),
        ),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert responses[0]["status"] == 200


def test_path_scoping_intercepts_matching_path() -> None:
    """The configured path is intercepted, trailing slashes aside."""
    body = b'{"jsonrpc":"2.0","id":0,"method":"server/discover"}'
    _, responses = _run(
        _middleware(path="/mcp"),
        _post_scope(
            path="/mcp/",
            headers=_headers(**{"mcp-protocol-version": "2026-07-28"}),
        ),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert responses[0]["status"] == 400
    assert json.loads(_response_body(responses))["id"] == 0


def test_float_request_id_is_echoed() -> None:
    """Float ids echo back unchanged."""
    body = b'{"jsonrpc":"2.0","id":1.5,"method":"server/discover"}'
    _, responses = _run(
        _middleware(),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2026-07-28"})),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert json.loads(_response_body(responses))["id"] == 1.5


def test_root_path_mounted_app_is_intercepted() -> None:
    """A `root_path` mount prefix is stripped before path matching."""
    body = b'{"jsonrpc":"2.0","id":0,"method":"server/discover"}'
    scope = _post_scope(
        path="/proxy/mcp",
        headers=_headers(**{"mcp-protocol-version": "2026-07-28"}),
    )
    scope["root_path"] = "/proxy"
    _, responses = _run(
        _middleware(path="/mcp"),
        scope,
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert responses[0]["status"] == 400
    payload = json.loads(_response_body(responses))
    assert payload["id"] == 0
    assert payload["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION_ERROR_CODE


def test_root_path_mounted_app_non_matching_path_passes_through() -> None:
    """Requests outside the configured path still pass through under `root_path`."""
    body = b'{"jsonrpc":"2.0","id":0,"method":"server/discover"}'
    scope = _post_scope(
        path="/proxy/other",
        headers=_headers(**{"mcp-protocol-version": "2026-07-28"}),
    )
    scope["root_path"] = "/proxy"
    _, responses = _run(
        _middleware(path="/mcp"),
        scope,
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert responses[0]["status"] == 200


def test_custom_supported_versions_are_advertised() -> None:
    """A caller-provided version list is echoed in the error data."""
    _, responses = _run(
        _middleware(supported_versions=["2025-11-25"]),
        _post_scope(headers=_headers(**{"mcp-protocol-version": "2026-07-28"})),
        [{"type": "http.request", "body": b'{"id":0}', "more_body": False}],
    )
    payload = json.loads(_response_body(responses))
    assert payload["error"]["data"]["supported"] == ["2025-11-25"]
