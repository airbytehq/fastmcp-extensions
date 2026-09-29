# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Self-describing capability tokens for stateless MCP HTTP transport."""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp.server.dependencies import get_context, get_http_headers
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_TOKEN_SEPARATOR = "."
_SESSION_HEADER = b"mcp-session-id"
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_HTTP_REQUEST = "http"
_HTTP_RESPONSE_START = "http.response.start"
_HTTP_REQUEST_METHOD = "method"
_HTTP_REQUEST_BODY = "body"
_HTTP_REQUEST_MORE_BODY = "more_body"
_HTTP_RESPONSE_HEADERS = "headers"
_HTTP_DISCONNECT = "http.disconnect"
_UUID4_VERSION = 4
_MAX_INITIALIZE_BODY_BYTES = 64 * 1024
_SESSION_TOKEN_VERSION = 2
_MAX_CLIENT_FIELD_CHARS = 128
_METADATA_PREFIX = "~meta:"
_MINTED_SESSION_TOKEN_STATE_KEY = "fastmcp_extensions.minted_session_token"
DEFAULT_EXTENSIONS_HEADER = "X-MCP-Extensions"


@dataclass(frozen=True, slots=True)
class SessionToken:
    """Decoded contents of a self-describing stateless session token.

    Every field is a client self-declaration carried in an unsigned token. Use
    it for rendering decisions and analytics, never for authorization.
    """

    extensions: frozenset[str] = frozenset()
    client_name: str | None = None
    client_version: str | None = None
    protocol_version: str | None = None


def encode_capability_token(extension_ids: AbstractSet[str]) -> str:
    """Encode extension IDs as a legacy (v1) visible-ASCII capability token.

    The token contains a random UUID4 component and a base64url-encoded,
    space-separated payload. An empty extension set returns an empty string.
    Prefer `encode_session_token`, which also carries client metadata.
    """
    normalized_ids = _normalize_extension_ids(extension_ids)
    if not normalized_ids:
        return ""
    return _wrap_payload(" ".join(normalized_ids))


def encode_session_token(
    *,
    extensions: AbstractSet[str] = frozenset(),
    client_name: str | None = None,
    client_version: str | None = None,
    protocol_version: str | None = None,
) -> str:
    """Encode a v2 session token carrying extensions and client metadata.

    The payload is the v1 space-separated extension list plus one
    `~meta:<base64url JSON>` item, so v1 decoders still read the extensions.
    The token is always non-empty. Client metadata is trimmed, stripped of
    non-printable characters, and capped at 128 characters per field.
    """
    metadata: dict[str, object] = {"v": _SESSION_TOKEN_VERSION}
    for key, value in (
        ("client_name", client_name),
        ("client_version", client_version),
        ("protocol_version", protocol_version),
    ):
        cleaned = _clean_client_field(value)
        if cleaned is not None:
            metadata[key] = cleaned
    metadata_item = _METADATA_PREFIX + _b64encode(
        json.dumps(metadata, separators=(",", ":"))
    )
    return _wrap_payload(
        " ".join([*_normalize_extension_ids(extensions), metadata_item])
    )


def decode_session_token(token: str) -> SessionToken | None:
    """Decode a v1 or v2 session token, returning `None` for invalid tokens."""
    payload = _unwrap_payload(token)
    if payload is None:
        return None

    items = payload.split()
    metadata_items = [item for item in items if item.startswith(_METADATA_PREFIX)]
    extension_ids = frozenset(
        item for item in items if not item.startswith(_METADATA_PREFIX)
    )
    if not metadata_items:
        return SessionToken(extensions=extension_ids) if extension_ids else None
    if len(metadata_items) > 1:
        return None
    return _decode_metadata(
        metadata_items[0].removeprefix(_METADATA_PREFIX), extension_ids
    )


def decode_capability_token(token: str) -> set[str]:
    """Decode extension IDs from a v1 or v2 token, failing closed on errors."""
    decoded = decode_session_token(token)
    if decoded is None:
        return set()
    return set(decoded.extensions)


def session_token_from_headers() -> SessionToken | None:
    """Return the decoded `Mcp-Session-Id` token of the current HTTP request.

    Returns `None` outside HTTP requests and when the header is absent or
    invalid. The token is unsigned and caller-controlled.
    """
    session_header = _SESSION_HEADER.decode("ascii")
    headers = get_http_headers(include={session_header})
    return decode_session_token(headers.get(session_header, ""))


def minted_session_token(scope: Mapping[str, object]) -> str | None:
    """Return the session token `CapabilityTokenMiddleware` mints for this request.

    Set in the ASGI scope state before the wrapped app runs, so in-app code can
    correlate an `initialize` with the `Mcp-Session-Id` the client will echo.
    Returns `None` for requests that are not an `initialize`.
    """
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    token = state.get(_MINTED_SESSION_TOKEN_STATE_KEY)
    return token if isinstance(token, str) and token else None


def client_declared_extensions_from_headers(
    *,
    fallback_header: str = DEFAULT_EXTENSIONS_HEADER,
) -> set[str]:
    """Return extension IDs from the session token and fallback header.

    The fallback header accepts comma-separated or whitespace-separated values.
    Invalid or absent session tokens contribute no extension IDs.

    Both sources are unauthenticated and caller-controlled: the token is unsigned
    and the header is whatever the client sent. Treat the result as a statement of
    what the client can render, never as a privilege.
    """
    headers = get_http_headers(
        include={fallback_header.lower(), _SESSION_HEADER.decode("ascii")}
    )
    session_extensions = decode_capability_token(headers.get("mcp-session-id", ""))
    fallback_value = headers.get(fallback_header.lower(), "")
    fallback_extensions = set(fallback_value.replace(",", " ").split())
    return session_extensions | fallback_extensions


def client_supports_extension(
    extension_id: str,
    *,
    fallback_header: str = DEFAULT_EXTENSIONS_HEADER,
) -> bool:
    """Return whether the current client declared an extension.

    FastMCP session capabilities are checked first, followed by the
    self-describing session token and the fallback HTTP header. All three are
    client self-declarations, so this answers "can the client handle this?" and
    never "is the client allowed to?".
    """
    try:
        context = get_context()
    except RuntimeError:
        session_supports_extension = False
    else:
        session_supports_extension = False
        try:
            session = context.session
        except RuntimeError:
            # Sessionless contexts (e.g. `fastmcp inspect`, docs generation)
            # expose a Context with no underlying session.
            caps = None
        else:
            caps = session.client_capabilities
        if caps is not None:
            extensions = caps.extensions
            if extensions is None:
                # Legacy clients may carry `extensions` as an extra key.
                extensions = (caps.model_extra or {}).get("extensions")
            session_supports_extension = bool(extensions) and extension_id in extensions
    return session_supports_extension or (
        extension_id
        in client_declared_extensions_from_headers(fallback_header=fallback_header)
    )


def _normalize_extension_ids(extension_ids: AbstractSet[str]) -> list[str]:
    return sorted(
        extension_id
        for extension_id in extension_ids
        if extension_id
        and not extension_id.startswith(_METADATA_PREFIX)
        and not any(char.isspace() for char in extension_id)
    )


def _clean_client_field(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    return cleaned[:_MAX_CLIENT_FIELD_CHARS] or None


def _b64encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _b64decode(value: str) -> str | None:
    if not value or not _BASE64URL_PATTERN.fullmatch(value):
        return None
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _wrap_payload(payload: str) -> str:
    return f"{uuid.uuid4().hex}{_TOKEN_SEPARATOR}{_b64encode(payload)}"


def _unwrap_payload(token: str) -> str | None:
    if not token or _TOKEN_SEPARATOR not in token:
        return None

    nonce, encoded_payload = token.split(_TOKEN_SEPARATOR, maxsplit=1)
    if not _is_uuid4_hex(nonce):
        return None
    return _b64decode(encoded_payload)


def _decode_metadata(
    encoded_metadata: str, extension_ids: frozenset[str]
) -> SessionToken | None:
    metadata = _b64decode(encoded_metadata)
    if metadata is None:
        return None
    try:
        parsed = json.loads(metadata)
    except json.JSONDecodeError:
        return None
    parsed_mapping = _mapping(parsed)
    if parsed_mapping is None or parsed_mapping.get("v") != _SESSION_TOKEN_VERSION:
        return None
    return SessionToken(
        extensions=extension_ids,
        client_name=_clean_client_field(parsed_mapping.get("client_name")),
        client_version=_clean_client_field(parsed_mapping.get("client_version")),
        protocol_version=_clean_client_field(parsed_mapping.get("protocol_version")),
    )


def _is_uuid4_hex(value: str) -> bool:
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError:
        return False
    return parsed.hex == value.lower() and parsed.version == _UUID4_VERSION


def _mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _initialize_session_token(body: bytes) -> str:
    """Return a session token for an `initialize` body, or `""` for other bodies."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""

    payload_mapping = _mapping(payload)
    if payload_mapping is None or payload_mapping.get("method") != "initialize":
        return ""
    params = _mapping(payload_mapping.get("params")) or {}
    capabilities = _mapping(params.get("capabilities"))
    extensions = (
        _mapping(capabilities.get("extensions")) if capabilities is not None else None
    )
    client_info = _mapping(params.get("clientInfo")) or {}
    protocol_version = params.get("protocolVersion")
    return encode_session_token(
        extensions={
            extension_id
            for extension_id in extensions or {}
            if isinstance(extension_id, str) and extension_id
        },
        client_name=_clean_client_field(client_info.get("name")),
        client_version=_clean_client_field(client_info.get("version")),
        protocol_version=_clean_client_field(protocol_version),
    )


class CapabilityTokenMiddleware:
    """Carry initialize declarations through stateless HTTP requests.

    Every `initialize` response gets a v2 session token in `Mcp-Session-Id`
    carrying the declared extensions, `clientInfo` name and version, and the
    requested protocol version. Other requests pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != _HTTP_REQUEST or scope.get(_HTTP_REQUEST_METHOD) != "POST":
            await self.app(scope, receive, send)
            return

        buffered_messages: list[Message] = []
        body_parts: list[bytes] = []
        body_size = 0
        oversized = False
        disconnected = False
        while True:
            message = await receive()
            if message.get("type") == _HTTP_DISCONNECT:
                buffered_messages.append(message)
                disconnected = True
                break
            buffered_messages.append(message)
            body_part = message.get(_HTTP_REQUEST_BODY, b"")
            if not oversized:
                body_size += len(body_part)
                if body_size > _MAX_INITIALIZE_BODY_BYTES:
                    oversized = True
                    body_parts.clear()
                else:
                    body_parts.append(body_part)
            if oversized:
                break
            if not message.get(_HTTP_REQUEST_MORE_BODY, False):
                break
        body = b"".join(body_parts)
        token = "" if oversized or disconnected else _initialize_session_token(body)
        if token:
            scope.setdefault("state", {})[_MINTED_SESSION_TOKEN_STATE_KEY] = token
        replay_index = 0

        async def replay_receive() -> Message:
            nonlocal replay_index
            if replay_index < len(buffered_messages):
                message = buffered_messages[replay_index]
                replay_index += 1
                return message
            if disconnected:
                return {"type": _HTTP_DISCONNECT}
            return await receive()

        async def send_response(message: Message) -> None:
            if (
                message.get("type") == _HTTP_RESPONSE_START
                and token
                and isinstance(message.get(_HTTP_RESPONSE_HEADERS), list)
            ):
                headers = [
                    (name, value)
                    for name, value in message[_HTTP_RESPONSE_HEADERS]
                    if name.lower() != _SESSION_HEADER
                ]
                headers.append((_SESSION_HEADER, token.encode("ascii")))
                message = {**message, _HTTP_RESPONSE_HEADERS: headers}
            await send(message)

        await self.app(scope, replay_receive, send_response)


class RejectEventStreamGetMiddleware:
    """Reject SSE GETs while allowing browser landing-page GETs.

    `path` scopes rejection to one MCP endpoint. Omitting it preserves the
    unscoped behavior for callers that construct this middleware directly.
    """

    def __init__(self, app: ASGIApp, path: str | None = None) -> None:
        self.app = app
        self.path = _normalize_path(path) if path is not None else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and (
                self.path is None or _normalize_path(scope.get("path", "")) == self.path
            )
            and any(
                name.lower() == b"accept" and b"text/event-stream" in value.lower()
                for name, value in scope.get("headers", [])
            )
        ):
            response = Response(status_code=405, headers={"allow": "POST, DELETE"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _normalize_path(path: str) -> str:
    """Normalize a path while treating trailing slashes as equivalent."""
    return path.rstrip("/") or "/"
