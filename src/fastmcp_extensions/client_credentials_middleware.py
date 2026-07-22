# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Opt-in HTTP Basic client-credentials transport auth for MCP servers.

The headless bearer path (`JWTVerifier`) verifies an already-minted, short-lived
access token. That works for MCP clients that run the OAuth flow and refresh
tokens automatically, but not for a truly headless agent that can only set a
*static* `Authorization` header value and cannot re-mint on a timer.

This module bridges that gap, generically. When enabled, the server accepts the
long-lived `client_id` / `client_secret` presented on the inbound request via
standard HTTP Basic auth (`Authorization: Basic base64(client_id:client_secret)`,
the same credential encoding OAuth's `client_secret_basic` uses). It runs an
OAuth 2.0 client-credentials grant against the configured token endpoint to
obtain a short-lived access token, and rewrites the request to
`Authorization: Bearer <token>` so the existing token verifier validates it
unchanged. The agent thus presents a durable credential once; the server owns
the short-lived-token churn.

The exchange runs as the outermost ASGI layer (ahead of FastMCP's auth
middleware) so the rewritten bearer header is what the verifier sees. Minted
tokens are cached per credential until shortly before expiry to avoid minting on
every request. A `Bearer` request, or any request when disabled, passes through
untouched.

This module is provider-neutral: the caller owns the opt-in toggle and supplies
the token endpoint (plus optional scope/audience/auth-method), so no issuer,
realm, or env-var name is baked in here. Wrap an ASGI app with
`wrap_client_credentials`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import time
from typing import TYPE_CHECKING

import httpx

from fastmcp_extensions.auth import (
    DEFAULT_CLIENT_CREDENTIALS_TIMEOUT_SECONDS,
    ClientCredentials,
    build_client_credentials_post_kwargs,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger(__name__)

# Re-mint this many seconds before the cached token actually expires, so a token
# never lapses mid-request due to clock skew or in-flight latency.
DEFAULT_EXPIRY_SAFETY_MARGIN_SECONDS = 60


def wrap_client_credentials(
    app: ASGIApp,
    *,
    enabled: bool,
    token_url: str,
    scope: str | None = None,
    audience: str | None = None,
    auth_method: str = "client_secret_post",
    expiry_margin_seconds: int = DEFAULT_EXPIRY_SAFETY_MARGIN_SECONDS,
    timeout_seconds: int = DEFAULT_CLIENT_CREDENTIALS_TIMEOUT_SECONDS,
) -> ASGIApp:
    """Wrap `app` with the client-credentials exchange when `enabled`.

    Returns `app` unchanged when `enabled` is falsy, so the standard bearer/OIDC
    transport auth is the only path. When enabled, returns `app` wrapped as the
    outermost ASGI layer so the Basic-to-Bearer rewrite happens before FastMCP's
    auth verifier runs.

    The caller owns the opt-in decision and the endpoint: `enabled` and
    `token_url` are passed in (e.g. resolved from the server's own branded env
    vars) rather than read from the environment here.
    """
    if not enabled:
        return app
    logger.info(
        "HTTP Basic client-credentials transport auth is enabled; the server "
        "will exchange presented client credentials for bearer tokens."
    )
    return ClientCredentialsExchangeMiddleware(
        app,
        token_url=token_url,
        scope=scope,
        audience=audience,
        auth_method=auth_method,
        expiry_margin_seconds=expiry_margin_seconds,
        timeout_seconds=timeout_seconds,
    )


class ClientCredentialsExchangeMiddleware:
    """ASGI middleware that exchanges HTTP Basic client credentials for a bearer.

    Runs as the outermost layer so the rewritten `Authorization: Bearer` header
    reaches FastMCP's auth verifier. Only `Basic` requests are touched; `Bearer`
    and unauthenticated requests pass through unchanged (the latter are then
    rejected by the verifier, preserving fail-closed behavior).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token_url: str,
        scope: str | None = None,
        audience: str | None = None,
        auth_method: str = "client_secret_post",
        expiry_margin_seconds: int = DEFAULT_EXPIRY_SAFETY_MARGIN_SECONDS,
        timeout_seconds: int = DEFAULT_CLIENT_CREDENTIALS_TIMEOUT_SECONDS,
    ) -> None:
        self._app = app
        self._token_url = token_url
        self._scope = scope
        self._audience = audience
        self._auth_method = auth_method
        self._expiry_margin_seconds = expiry_margin_seconds
        self._timeout_seconds = timeout_seconds
        # Maps a per-credential cache key to `(access_token, expiry_deadline)`,
        # where the deadline is a `time.monotonic()` value (not a wall-clock
        # epoch). Expired entries are pruned under `_locks_guard` so the cache
        # can't grow unbounded from a high-cardinality stream of credentials.
        self._token_cache: dict[str, tuple[str, float]] = {}
        # A lock per credential so a slow/unreachable token endpoint stalls only
        # the affected credential, not all Basic-auth traffic. `_locks_guard`
        # serializes creation of the per-credential locks themselves.
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, cache_key: str) -> asyncio.Lock:
        """Return the lock dedicated to `cache_key`, creating it on first use."""
        async with self._locks_guard:
            self._prune_expired(time.monotonic(), keep=cache_key)
            lock = self._locks.get(cache_key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[cache_key] = lock
            return lock

    def _prune_expired(self, now: float, *, keep: str) -> None:
        """Drop expired token entries and their idle locks; caller holds the guard.

        Keeps the cache and lock dicts bounded to credentials that are currently
        cached or in flight, so an unbounded stream of distinct Basic credentials
        can't leak memory. `keep` is the key whose lock is about to be used: its
        lock is never pruned, but its token-cache entry still is when expired
        (the caller re-mints under that lock, so a stale entry there is moot).
        """
        expired = [
            key for key, (_, deadline) in self._token_cache.items() if deadline <= now
        ]
        for key in expired:
            del self._token_cache[key]
        stale_locks = [
            key
            for key, lock in self._locks.items()
            if key != keep and key not in self._token_cache and not lock.locked()
        ]
        for key in stale_locks:
            del self._locks[key]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite a Basic-auth HTTP request to Bearer, then delegate downstream."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        credentials = _parse_basic_credentials(scope)
        if credentials is None:
            await self._app(scope, receive, send)
            return

        token = await self._token_for(credentials)
        if token is None:
            # Exchange failed (e.g. bad credentials): pass the request through
            # unmodified so the verifier rejects it with a 401, rather than
            # masking the failure here.
            await self._app(scope, receive, send)
            return

        await self._app(_with_bearer(scope, token), receive, send)

    async def _token_for(self, credentials: tuple[str, str]) -> str | None:
        """Return a valid access token for the credentials, minting if needed."""
        client_id, client_secret = credentials
        cache_key = _cache_key(client_id, client_secret)

        lock = await self._lock_for(cache_key)
        async with lock:
            # Read the clock inside the lock so a delayed lock acquisition can't
            # treat an already-expired cached token as still valid.
            now = time.monotonic()
            cached = self._token_cache.get(cache_key)
            if cached is not None and cached[1] > now:
                return cached[0]

            minted = await self._mint_token(client_id, client_secret)
            if minted is None:
                return None

            # Base the expiry deadline on a fresh reading taken after the mint
            # round-trip, so network latency isn't charged against the token's
            # usable lifetime.
            token, expires_in = minted
            expiry = time.monotonic() + max(expires_in - self._expiry_margin_seconds, 0)
            self._token_cache[cache_key] = (token, expiry)
            return token

    async def _mint_token(
        self, client_id: str, client_secret: str
    ) -> tuple[str, float] | None:
        """Exchange client credentials for `(access_token, expires_in)` or `None`.

        Returns `None` when the token endpoint rejects the credentials or omits
        an access token; never logs the credentials or the minted token.
        """
        post_kwargs = build_client_credentials_post_kwargs(
            ClientCredentials(
                token_url=self._token_url,
                client_id=client_id,
                client_secret=client_secret,
                scope=self._scope,
                audience=self._audience,
                auth_method=self._auth_method,
            )
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(self._token_url, **post_kwargs)
        except httpx.HTTPError as exc:
            # Unreachable endpoint, timeout, or transport error: fail closed by
            # passing the request through for the verifier to reject with a 401
            # rather than surfacing a 500. The exception type is safe to log; the
            # credentials are not.
            logger.warning(
                "Client-credentials token exchange request failed (%s); passing "
                "request through for the verifier to reject.",
                type(exc).__name__,
            )
            return None

        if response.status_code != httpx.codes.OK:
            logger.warning(
                "Client-credentials token exchange failed (HTTP %d); passing "
                "request through for the verifier to reject.",
                response.status_code,
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "Client-credentials token endpoint returned a non-JSON body; "
                "passing request through for the verifier to reject."
            )
            return None
        access_token = payload.get("access_token")
        if not access_token or not isinstance(access_token, str):
            logger.warning(
                "Client-credentials token exchange returned no access token; "
                "passing request through for the verifier to reject."
            )
            return None

        # `expires_in` is advisory and comes from an external response, so it may
        # be missing or non-numeric. Coerce defensively and fall back to `0.0`
        # (no caching) rather than letting a bad value raise and turn a clean
        # fail-closed pass-through into a 500.
        return access_token, _coerce_expires_in(payload.get("expires_in"))


def _coerce_expires_in(value: object) -> float:
    """Return `value` as a non-negative float, or `0.0` when unparseable.

    The token endpoint's `expires_in` is external input and may be missing, a
    string, or a non-numeric type. Anything that can't be coerced to a positive
    number yields `0.0` so the token simply isn't cached, never a raised error.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if seconds > 0 else 0.0


def _parse_basic_credentials(scope: Scope) -> tuple[str, str] | None:
    """Return `(client_id, client_secret)` from a Basic `Authorization` header.

    Returns `None` when the header is absent, uses a non-Basic scheme, or is
    malformed, so the request passes through for the verifier to handle.
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            return _decode_basic(value)
    return None


def _decode_basic(header_value: bytes) -> tuple[str, str] | None:
    """Decode a `Basic <base64(client_id:client_secret)>` header value."""
    scheme, _, encoded = header_value.partition(b" ")
    if scheme.lower() != b"basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, sep, client_secret = decoded.partition(":")
    if not sep:
        return None
    return client_id, client_secret


def _cache_key(client_id: str, client_secret: str) -> str:
    """Return a stable, non-reversible cache key for a credential pair.

    Hashes the secret so plaintext credentials never sit in the cache dict.
    """
    return hashlib.sha256(f"{client_id}:{client_secret}".encode()).hexdigest()


def _with_bearer(scope: Scope, token: str) -> Scope:
    """Return a shallow copy of `scope` with `Authorization` set to `Bearer`."""
    headers = [
        (name, value) for name, value in scope["headers"] if name != b"authorization"
    ]
    headers.append((b"authorization", b"Bearer " + token.encode("ascii")))
    new_scope = dict(scope)
    new_scope["headers"] = headers
    return new_scope
