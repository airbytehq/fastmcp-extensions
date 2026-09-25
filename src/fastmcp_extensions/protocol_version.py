# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Protocol-version negotiation stopgap for legacy `mcp` 1.x HTTP transports.

The `mcp` 1.x streamable-HTTP transport rejects an unknown
`MCP-Protocol-Version` header with HTTP 400 and a JSON-RPC error whose `id` is
the literal string `"server-error"`. Modern clients that open a connection with
a `server/discover` probe (rmcp, Goose CLI) cannot correlate that response to
their request, so they abort instead of falling back to `initialize`. Until the
server can run on `mcp` 2.x, this middleware answers those requests itself with
the spec-shaped `-32022` error so clients negotiate down to a supported
revision.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from starlette.responses import Response

from fastmcp_extensions.capability_tokens import _normalize_path

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

UNSUPPORTED_PROTOCOL_VERSION_ERROR_CODE = -32022  # mcp 2026-07-28 spec code

_HTTP_REQUEST = "http"
_HTTP_REQUEST_METHOD = "method"
_HTTP_REQUEST_BODY = "body"
_HTTP_REQUEST_MORE_BODY = "more_body"
_HTTP_DISCONNECT = "http.disconnect"
_PROTOCOL_VERSION_HEADER = b"mcp-protocol-version"
_MAX_BODY_BYTES = 64 * 1024


def _request_id(body: bytes) -> str | int | float | None:
    """Return the JSON-RPC request id from a buffered body, or `None`."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("id")
    if isinstance(request_id, str | int | float) and not isinstance(request_id, bool):
        return request_id
    return None


class ProtocolVersionNegotiationMiddleware:
    """Answer unsupported `MCP-Protocol-Version` POSTs with a correlated JSON-RPC error.

    The legacy `mcp` 1.x transport rejects unknown protocol-version headers with
    `id: "server-error"`, which some clients (rmcp / Goose CLI) cannot correlate,
    so they abort instead of falling back to `initialize`. This middleware
    intercepts those requests and replies with the spec-shaped -32022 error,
    echoing the request id and listing the supported versions so the client
    negotiates down.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str | None = None,
        supported_versions: Sequence[str] | None = None,
    ) -> None:
        self.app = app
        self.path = _normalize_path(path) if path is not None else None
        self.supported_versions = (
            list(SUPPORTED_PROTOCOL_VERSIONS)
            if supported_versions is None
            else list(supported_versions)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != _HTTP_REQUEST
            or scope.get(_HTTP_REQUEST_METHOD) != "POST"
            or (self.path is not None and self._routing_path(scope) != self.path)
        ):
            await self.app(scope, receive, send)
            return

        requested_version: str | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == _PROTOCOL_VERSION_HEADER:
                requested_version = value.decode("latin-1")
                break
        if requested_version is None or requested_version in self.supported_versions:
            await self.app(scope, receive, send)
            return

        body_parts: list[bytes] = []
        body_size = 0
        while True:
            message = await receive()
            if message.get("type") == _HTTP_DISCONNECT:
                break
            body_part = message.get(_HTTP_REQUEST_BODY, b"")
            body_size += len(body_part)
            if body_size > _MAX_BODY_BYTES:
                body_parts.clear()
                break
            body_parts.append(body_part)
            if not message.get(_HTTP_REQUEST_MORE_BODY, False):
                break

        request_id = _request_id(b"".join(body_parts))
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": UNSUPPORTED_PROTOCOL_VERSION_ERROR_CODE,
                "message": "Unsupported protocol version",
                "data": {
                    "supported": self.supported_versions,
                    "requested": requested_version,
                },
            },
        }
        response = Response(
            json.dumps(payload, separators=(",", ":")),
            status_code=400,
            media_type="application/json",
        )
        await response(scope, receive, send)

    @staticmethod
    def _routing_path(scope: Scope) -> str:
        """Return the normalized request path with any `root_path` mount stripped."""
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        return _normalize_path(path)
