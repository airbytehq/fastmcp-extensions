# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the generic client-credentials ASGI middleware.

Covers both credential-presentation forms: standard HTTP Basic in the
`Authorization` header, and the separate `Client-Id` / `Client-Secret` headers.
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING

import httpx
import pytest

from fastmcp_extensions import client_credentials_middleware as ccm

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import Receive, Scope, Send


def _basic_header(client_id: str, client_secret: str) -> bytes:
    """Return a `Basic` header value encoding `client_id:client_secret`."""
    raw = f"{client_id}:{client_secret}".encode()
    return b"Basic " + base64.b64encode(raw)


def _http_scope(*header: tuple[bytes, bytes]) -> Scope:
    """Return a minimal HTTP ASGI scope carrying the given headers."""
    return {"type": "http", "headers": list(header)}


def _auth_header(scope: Scope | None) -> bytes | None:
    """Return the `authorization` header value from an ASGI scope, if present."""
    assert scope is not None
    for name, value in scope["headers"]:
        if name == b"authorization":
            return value
    return None


class _RecordingApp:
    """Downstream ASGI app that records the scope handed to it."""

    def __init__(self) -> None:
        self.seen_scope: Scope | None = None
        self.calls = 0

    async def __call__(self, scope: Scope, _receive: Receive, _send: Send) -> None:
        self.calls += 1
        self.seen_scope = scope


async def _noop_receive() -> dict[str, object]:
    return {"type": "http.request"}


async def _noop_send(_message: dict[str, object]) -> None:
    return None


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Route the middleware's `httpx.AsyncClient` through a mock transport."""
    real_async_client = httpx.AsyncClient

    def factory(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(ccm.httpx, "AsyncClient", factory)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(900, 900.0, id="int"),
        pytest.param(900.5, 900.5, id="float"),
        pytest.param("900", 900.0, id="numeric-string"),
        pytest.param(None, 0.0, id="none"),
        pytest.param("not-a-number", 0.0, id="non-numeric-string"),
        pytest.param({}, 0.0, id="object"),
        pytest.param([], 0.0, id="list"),
        pytest.param(True, 0.0, id="bool-true"),
        pytest.param(0, 0.0, id="zero"),
        pytest.param(-5, 0.0, id="negative"),
    ],
)
def test_coerce_expires_in(value: object, expected: float) -> None:
    assert ccm._coerce_expires_in(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param(_basic_header("id", "secret"), ("id", "secret"), id="simple"),
        pytest.param(
            _basic_header("id", "sec:with:colons"),
            ("id", "sec:with:colons"),
            id="secret-with-colons",
        ),
        pytest.param(_basic_header("id", ""), ("id", ""), id="empty-secret"),
        pytest.param(b"Bearer sometoken", None, id="bearer-scheme"),
        pytest.param(b"Basic ", None, id="basic-no-payload"),
        pytest.param(b"Basic !!!notbase64!!!", None, id="not-base64"),
        pytest.param(
            b"Basic " + base64.b64encode(b"no-colon-here"),
            None,
            id="missing-colon",
        ),
    ],
)
def test_decode_basic(header: bytes, expected: tuple[str, str] | None) -> None:
    assert ccm._decode_basic(header) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(
            [(b"client-id", b"id"), (b"client-secret", b"secret")],
            ("id", "secret"),
            id="both-separate-headers",
        ),
        pytest.param(
            [(b"CLIENT-ID", b"id"), (b"Client-Secret", b"secret")],
            None,
            id="wire-casing-not-normalized-by-caller",
        ),
        pytest.param(
            [(b"client-id", b"id")],
            None,
            id="only-client-id",
        ),
        pytest.param(
            [(b"client-secret", b"secret")],
            None,
            id="only-client-secret",
        ),
        pytest.param(
            [(b"client-id", b""), (b"client-secret", b"secret")],
            None,
            id="empty-client-id",
        ),
        pytest.param(
            [(b"client-id", b"id"), (b"client-secret", b"")],
            None,
            id="empty-client-secret",
        ),
    ],
)
def test_parse_separate_headers(
    headers: list[tuple[bytes, bytes]], expected: tuple[str, str] | None
) -> None:
    # ASGI normalizes header names to lowercase bytes before the app sees them,
    # so the parser matches lowercase; a caller passing raw wire-cased names is
    # not a real ASGI code path and is expected to miss.
    assert ccm._parse_separate_headers(headers) == expected


@pytest.mark.unit
def test_parse_credentials_prefers_separate_headers_over_basic() -> None:
    scope = _http_scope(
        (b"authorization", _basic_header("basic-id", "basic-secret")),
        (b"client-id", b"header-id"),
        (b"client-secret", b"header-secret"),
    )
    assert ccm._parse_credentials(scope) == ("header-id", "header-secret")


@pytest.mark.unit
def test_parse_credentials_falls_back_to_basic() -> None:
    scope = _http_scope((b"authorization", _basic_header("id", "secret")))
    assert ccm._parse_credentials(scope) == ("id", "secret")


@pytest.mark.unit
def test_parse_credentials_returns_none_without_credentials() -> None:
    scope = _http_scope((b"content-type", b"application/json"))
    assert ccm._parse_credentials(scope) is None


@pytest.mark.unit
def test_cache_key_is_stable_and_distinct() -> None:
    key = ccm._cache_key("id", "secret")
    assert key == ccm._cache_key("id", "secret")
    assert key != ccm._cache_key("id", "other-secret")
    assert key != ccm._cache_key("other-id", "secret")
    # The plaintext secret must not appear in the derived key.
    assert "secret" not in key


@pytest.mark.unit
def test_with_bearer_replaces_authorization() -> None:
    scope = _http_scope(
        (b"content-type", b"application/json"),
        (b"authorization", _basic_header("id", "secret")),
    )
    rewritten = ccm._with_bearer(scope, "minted-token")
    assert _auth_header(rewritten) == b"Bearer minted-token"
    # Original scope is left untouched (shallow copy).
    assert _auth_header(scope) != b"Bearer minted-token"
    # Non-auth headers are preserved.
    assert (b"content-type", b"application/json") in rewritten["headers"]


@pytest.mark.unit
def test_with_bearer_strips_separate_credential_headers() -> None:
    scope = _http_scope(
        (b"content-type", b"application/json"),
        (b"client-id", b"id"),
        (b"client-secret", b"secret"),
    )
    rewritten = ccm._with_bearer(scope, "minted-token")
    header_names = {name for name, _ in rewritten["headers"]}
    assert b"client-id" not in header_names
    assert b"client-secret" not in header_names
    assert _auth_header(rewritten) == b"Bearer minted-token"
    assert (b"content-type", b"application/json") in rewritten["headers"]


@pytest.mark.unit
def test_wrap_client_credentials_returns_app_unchanged_when_disabled() -> None:
    app = _RecordingApp()
    assert (
        ccm.wrap_client_credentials(
            app, enabled=False, token_url="https://example/token"
        )
        is app
    )


@pytest.mark.unit
def test_wrap_client_credentials_wraps_when_enabled() -> None:
    app = _RecordingApp()
    wrapped = ccm.wrap_client_credentials(
        app, enabled=True, token_url="https://example/token"
    )
    assert isinstance(wrapped, ccm.ClientCredentialsExchangeMiddleware)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    app = _RecordingApp()
    mw = ccm.ClientCredentialsExchangeMiddleware(app, token_url="https://example/token")
    await mw({"type": "lifespan"}, _noop_receive, _noop_send)
    assert app.calls == 1
    assert app.seen_scope == {"type": "lifespan"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bearer_request_passes_through_unchanged() -> None:
    app = _RecordingApp()
    mw = ccm.ClientCredentialsExchangeMiddleware(app, token_url="https://example/token")
    scope = _http_scope((b"authorization", b"Bearer client-token"))
    await mw(scope, _noop_receive, _noop_send)
    assert app.calls == 1
    assert _auth_header(app.seen_scope) == b"Bearer client-token"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basic_request_is_rewritten_and_cached() -> None:
    app = _RecordingApp()
    mw = ccm.ClientCredentialsExchangeMiddleware(app, token_url="https://example/token")
    scope = _http_scope((b"authorization", _basic_header("id", "secret")))
    mint_calls: list[tuple[str, str]] = []

    async def fake_mint(client_id: str, client_secret: str) -> tuple[str, float]:
        mint_calls.append((client_id, client_secret))
        return "minted-token", 900.0

    mw._mint_token = fake_mint  # type: ignore[assignment,method-assign]

    await mw(scope, _noop_receive, _noop_send)
    # A second identical request must reuse the cached token (no re-mint).
    await mw(scope, _noop_receive, _noop_send)

    assert _auth_header(app.seen_scope) == b"Bearer minted-token"
    assert mint_calls == [("id", "secret")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_separate_header_request_is_rewritten_and_credentials_stripped() -> None:
    app = _RecordingApp()
    mw = ccm.ClientCredentialsExchangeMiddleware(app, token_url="https://example/token")
    scope = _http_scope(
        (b"client-id", b"id"),
        (b"client-secret", b"secret"),
    )
    mint_calls: list[tuple[str, str]] = []

    async def fake_mint(client_id: str, client_secret: str) -> tuple[str, float]:
        mint_calls.append((client_id, client_secret))
        return "minted-token", 900.0

    mw._mint_token = fake_mint  # type: ignore[assignment,method-assign]

    await mw(scope, _noop_receive, _noop_send)

    assert mint_calls == [("id", "secret")]
    assert _auth_header(app.seen_scope) == b"Bearer minted-token"
    # The long-lived credential headers must not propagate downstream.
    assert app.seen_scope is not None
    downstream_names = {name for name, _ in app.seen_scope["headers"]}
    assert b"client-id" not in downstream_names
    assert b"client-secret" not in downstream_names


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_identical_requests_mint_once() -> None:
    app = _RecordingApp()
    mw = ccm.ClientCredentialsExchangeMiddleware(app, token_url="https://example/token")
    scope = _http_scope((b"authorization", _basic_header("id", "secret")))
    mint_calls: list[tuple[str, str]] = []

    async def fake_mint(client_id: str, client_secret: str) -> tuple[str, float]:
        mint_calls.append((client_id, client_secret))
        # Yield so the second concurrent request can interleave; the
        # per-credential lock must still serialize them into a single mint.
        await asyncio.sleep(0)
        return "minted-token", 900.0

    mw._mint_token = fake_mint  # type: ignore[assignment,method-assign]

    await asyncio.gather(
        mw(scope, _noop_receive, _noop_send),
        mw(scope, _noop_receive, _noop_send),
    )

    assert mint_calls == [("id", "secret")]
    assert _auth_header(app.seen_scope) == b"Bearer minted-token"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_exchange_passes_request_through_unmodified() -> None:
    app = _RecordingApp()
    mw = ccm.ClientCredentialsExchangeMiddleware(app, token_url="https://example/token")
    basic = _basic_header("id", "bad-secret")
    scope = _http_scope((b"authorization", basic))

    async def fake_mint(_client_id: str, _client_secret: str) -> None:
        return None

    mw._mint_token = fake_mint  # type: ignore[assignment,method-assign]

    await mw(scope, _noop_receive, _noop_send)
    assert app.calls == 1
    # Unchanged: still the original Basic header for the verifier to reject.
    assert _auth_header(app.seen_scope) == basic


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mint_token_success_posts_client_secret_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})

    _patch_async_client(monkeypatch, handler)
    mw = ccm.ClientCredentialsExchangeMiddleware(
        app=_RecordingApp(), token_url="https://idp/token"
    )

    result = await mw._mint_token("cid", "sec")

    assert result == ("tok", 900.0)
    body = str(captured["body"])
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "client_secret=sec" in body
    assert captured["url"] == "https://idp/token"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(
            lambda _r: httpx.Response(401, json={"error": "invalid_client"}),
            id="http-error-status",
        ),
        pytest.param(
            lambda _r: httpx.Response(200, text="not-json"),
            id="non-json-body",
        ),
        pytest.param(
            lambda _r: httpx.Response(200, json=["not", "an", "object"]),
            id="non-object-json-body",
        ),
        pytest.param(
            lambda _r: httpx.Response(200, json={"token_type": "bearer"}),
            id="missing-access-token",
        ),
    ],
)
async def test_mint_token_failures_return_none(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    _patch_async_client(monkeypatch, handler)
    mw = ccm.ClientCredentialsExchangeMiddleware(
        app=_RecordingApp(), token_url="https://idp/token"
    )
    assert await mw._mint_token("cid", "sec") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mint_token_transport_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _patch_async_client(monkeypatch, handler)
    mw = ccm.ClientCredentialsExchangeMiddleware(
        app=_RecordingApp(), token_url="https://idp/token"
    )
    assert await mw._mint_token("cid", "sec") is None
