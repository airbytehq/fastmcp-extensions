# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Reusable auth-provider factory for headless and interactive MCP auth.

This module makes it easy for a FastMCP server to accept **both** interactive
OAuth clients (humans in a browser) and **headless** clients (CI jobs, agents)
on the same deployment, without the fragility of storing and rotating refresh
tokens.

## The two client shapes

- **Interactive** clients use the Authorization Code + PKCE flow via an
  `OIDCProxy`. This requires a browser and human consent.
- **Headless** clients (agents, CI) cannot open a browser. Instead they mint a
  short-lived access token themselves using the OAuth 2.0 **client credentials
  grant** (`client_id` + `client_secret` → bearer token) and send it as an
  `Authorization: Bearer <token>` header. The server only has to *verify* that
  token — via `JWTVerifier` (validate a JWT against the issuer's JWKS) or
  `IntrospectionTokenVerifier` (RFC 7662 introspection for opaque tokens).

Because a headless client re-mints its access token on demand from stable
client credentials, there is **no long-lived refresh token to persist or
rotate** — so two copies of an agent can never invalidate each other's tokens.

## Combining both

`build_mcp_auth` assembles the configured pieces into a single FastMCP
`AuthProvider`. When both an interactive server (`OIDCProxy`) and one or more
headless verifiers are configured, they are combined with `MultiAuth` so a
single deployment serves both audiences.

## Minting tokens (client side / hybrid server side)

`fetch_client_credentials_token` performs the client credentials grant against
a token endpoint and returns the access token. Headless clients use it to mint
their own bearer token. A server can also use it to implement the "hybrid" path
where it accepts raw `client_id`/`client_secret` and does the exchange itself.

## Example

```python
from fastmcp_extensions import build_mcp_auth, JWTAuthConfig, OIDCAuthConfig

auth = build_mcp_auth(
    # Interactive humans (optional):
    oidc=OIDCAuthConfig(
        config_url="https://idp.example/.well-known/openid-configuration",
        client_id="mcp-web",
        client_secret="...",
        base_url="https://mcp.example",
    ),
    # Headless agents / CI (optional):
    jwt=JWTAuthConfig(
        jwks_uri="https://idp.example/.well-known/jwks.json",
        issuer="https://idp.example/",
        audience="mcp-api",
    ),
)
app = mcp_server(name="my-server", auth=auth)
```
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp.server.auth import AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier, StaticTokenVerifier

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_CREDENTIALS_TIMEOUT_SECONDS = 30

SUPPORTED_CLIENT_AUTH_METHODS = ("client_secret_post", "client_secret_basic")


@dataclass
class OIDCAuthConfig:
    """Config for the interactive Authorization Code + PKCE flow (`OIDCProxy`).

    Use this for browser-based MCP clients that authenticate a human. `base_url`
    is the public base URL of the MCP server (used for OAuth redirect
    callbacks); it is required by `OIDCProxy`, either here or via the
    `base_url` argument to `build_mcp_auth`.
    """

    config_url: str
    client_id: str
    client_secret: str | None = None
    base_url: str | None = None
    audience: str | None = None
    required_scopes: list[str] | None = None


@dataclass
class JWTAuthConfig:
    """Config for headless verification of JWT bearer tokens (`JWTVerifier`).

    Provide either `jwks_uri` (fetch the issuer's public keys dynamically) or a
    static `public_key`. `issuer` and `audience` are validated against the
    token's claims when set. This is the recommended headless path: the client
    mints a JWT via the client credentials grant and the server verifies it
    with no shared state.
    """

    jwks_uri: str | None = None
    public_key: str | None = None
    issuer: str | None = None
    audience: str | None = None
    algorithm: str | None = None
    required_scopes: list[str] | None = None
    base_url: str | None = None

    def __post_init__(self) -> None:
        if not self.jwks_uri and not self.public_key:
            raise ValueError(
                "JWTAuthConfig requires either 'jwks_uri' or 'public_key'."
            )


@dataclass
class IntrospectionAuthConfig:
    """Config for headless verification of opaque tokens via RFC 7662.

    Use this when the issuer hands out opaque (non-JWT) access tokens; the
    server calls the introspection endpoint to validate them. `client_id` /
    `client_secret` authenticate the server to that endpoint.
    """

    introspection_url: str
    client_id: str
    client_secret: str
    client_auth_method: str = "client_secret_basic"
    required_scopes: list[str] | None = None
    cache_ttl_seconds: int | None = None


def _build_jwt_verifier(config: JWTAuthConfig) -> JWTVerifier:
    return JWTVerifier(
        public_key=config.public_key,
        jwks_uri=config.jwks_uri,
        issuer=config.issuer,
        audience=config.audience,
        algorithm=config.algorithm,
        required_scopes=config.required_scopes,
        base_url=config.base_url,
    )


def _build_introspection_verifier(
    config: IntrospectionAuthConfig,
) -> IntrospectionTokenVerifier:
    return IntrospectionTokenVerifier(
        introspection_url=config.introspection_url,
        client_id=config.client_id,
        client_secret=config.client_secret,
        client_auth_method=config.client_auth_method,  # type: ignore[arg-type]
        required_scopes=config.required_scopes,
        cache_ttl_seconds=config.cache_ttl_seconds,
    )


def _build_oidc_proxy(config: OIDCAuthConfig, base_url: str | None) -> OIDCProxy:
    resolved_base_url = config.base_url or base_url
    if not resolved_base_url:
        raise ValueError(
            "OIDCAuthConfig requires a base_url (set it on the config or pass "
            "base_url to build_mcp_auth)."
        )
    return OIDCProxy(
        config_url=config.config_url,
        client_id=config.client_id,
        client_secret=config.client_secret,
        base_url=resolved_base_url,
        audience=config.audience,
        required_scopes=config.required_scopes,
    )


def _assemble_auth(
    *,
    server: AuthProvider | None,
    verifiers: list[TokenVerifier],
    base_url: str | None,
    required_scopes: list[str] | None,
) -> AuthProvider | None:
    """Combine an optional interactive server with headless verifiers.

    Returns the single provider when only one is configured, a `MultiAuth`
    when several are, or `None` when nothing is configured.
    """
    if server is not None and verifiers:
        return MultiAuth(
            server=server,
            verifiers=verifiers,
            base_url=base_url,
            required_scopes=required_scopes,
        )
    if server is not None:
        return server
    if len(verifiers) == 1 and not required_scopes:
        return verifiers[0]
    if verifiers:
        return MultiAuth(
            verifiers=verifiers,
            base_url=base_url,
            required_scopes=required_scopes,
        )
    return None


def build_mcp_auth(
    *,
    oidc: OIDCAuthConfig | None = None,
    jwt: JWTAuthConfig | None = None,
    introspection: IntrospectionAuthConfig | None = None,
    static_tokens: Mapping[str, dict[str, Any]] | None = None,
    base_url: str | None = None,
    required_scopes: list[str] | None = None,
) -> AuthProvider | None:
    """Build a FastMCP `AuthProvider` from the configured auth methods.

    Any combination may be supplied:

    - `oidc`: interactive Authorization Code + PKCE (`OIDCProxy`) for humans.
    - `jwt`: headless JWT bearer verification (`JWTVerifier`).
    - `introspection`: headless opaque-token verification (RFC 7662).
    - `static_tokens`: fixed tokens for local dev / CI (`StaticTokenVerifier`).

    When both an interactive server and one or more headless verifiers are
    given, they are combined with `MultiAuth` so a single server accepts both
    kinds of client. Returns `None` when nothing is configured, so callers can
    fall back to header-based credential resolution.
    """
    server = _build_oidc_proxy(oidc, base_url) if oidc is not None else None

    verifiers: list[TokenVerifier] = []
    if jwt is not None:
        verifiers.append(_build_jwt_verifier(jwt))
    if introspection is not None:
        verifiers.append(_build_introspection_verifier(introspection))
    if static_tokens:
        verifiers.append(StaticTokenVerifier(dict(static_tokens)))

    return _assemble_auth(
        server=server,
        verifiers=verifiers,
        base_url=base_url,
        required_scopes=required_scopes,
    )


def _split_scopes(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    scopes = [s.strip() for s in raw.replace(",", " ").split()]
    scopes = [s for s in scopes if s]
    return scopes or None


def resolve_mcp_auth(
    env: Mapping[str, str] | None = None,
) -> AuthProvider | None:
    """Build an `AuthProvider` from a standard set of environment variables.

    This is the convenience entry point that lets any server opt into headless
    auth "for free". It reads the following variables and delegates to
    `build_mcp_auth`:

    Interactive OIDC (`OIDCProxy`):

    - `OIDC_CONFIG_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
    - `MCP_SERVER_URL` (base URL for redirect callbacks)
    - `OIDC_AUDIENCE` (optional)

    Headless JWT bearer verification (`JWTVerifier`):

    - `MCP_AUTH_JWKS_URI` (or `MCP_AUTH_JWT_PUBLIC_KEY`)
    - `MCP_AUTH_ISSUER`, `MCP_AUTH_AUDIENCE` (optional but recommended)
    - `MCP_AUTH_ALGORITHM` (optional)

    Headless opaque-token introspection (`IntrospectionTokenVerifier`):

    - `MCP_AUTH_INTROSPECTION_URL`
    - `MCP_AUTH_INTROSPECTION_CLIENT_ID` / `MCP_AUTH_INTROSPECTION_CLIENT_SECRET`
      (falling back to `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`)

    Shared:

    - `MCP_AUTH_REQUIRED_SCOPES` (comma or space separated)

    Returns `None` when no auth is configured.
    """
    env = os.environ if env is None else env
    base_url = env.get("MCP_SERVER_URL") or None
    required_scopes = _split_scopes(env.get("MCP_AUTH_REQUIRED_SCOPES"))

    oidc: OIDCAuthConfig | None = None
    if env.get("OIDC_CONFIG_URL") and env.get("OIDC_CLIENT_ID"):
        oidc = OIDCAuthConfig(
            config_url=env["OIDC_CONFIG_URL"],
            client_id=env["OIDC_CLIENT_ID"],
            client_secret=env.get("OIDC_CLIENT_SECRET") or None,
            base_url=base_url,
            audience=env.get("OIDC_AUDIENCE") or None,
        )

    jwt: JWTAuthConfig | None = None
    if env.get("MCP_AUTH_JWKS_URI") or env.get("MCP_AUTH_JWT_PUBLIC_KEY"):
        jwt = JWTAuthConfig(
            jwks_uri=env.get("MCP_AUTH_JWKS_URI") or None,
            public_key=env.get("MCP_AUTH_JWT_PUBLIC_KEY") or None,
            issuer=env.get("MCP_AUTH_ISSUER") or None,
            audience=env.get("MCP_AUTH_AUDIENCE") or None,
            algorithm=env.get("MCP_AUTH_ALGORITHM") or None,
        )

    introspection: IntrospectionAuthConfig | None = None
    if env.get("MCP_AUTH_INTROSPECTION_URL"):
        client_id = env.get("MCP_AUTH_INTROSPECTION_CLIENT_ID") or env.get(
            "OIDC_CLIENT_ID"
        )
        client_secret = env.get("MCP_AUTH_INTROSPECTION_CLIENT_SECRET") or env.get(
            "OIDC_CLIENT_SECRET"
        )
        if not client_id or not client_secret:
            raise ValueError(
                "MCP_AUTH_INTROSPECTION_URL is set but no introspection client "
                "credentials were found. Set MCP_AUTH_INTROSPECTION_CLIENT_ID "
                "and MCP_AUTH_INTROSPECTION_CLIENT_SECRET (or OIDC_CLIENT_ID / "
                "OIDC_CLIENT_SECRET)."
            )
        introspection = IntrospectionAuthConfig(
            introspection_url=env["MCP_AUTH_INTROSPECTION_URL"],
            client_id=client_id,
            client_secret=client_secret,
        )

    return build_mcp_auth(
        oidc=oidc,
        jwt=jwt,
        introspection=introspection,
        base_url=base_url,
        required_scopes=required_scopes,
    )


@dataclass
class ClientCredentials:
    """Parameters for an OAuth 2.0 client credentials grant.

    `token_url` is the issuer's token endpoint. `scope` and `audience` are sent
    when set (some issuers, e.g. Auth0, require `audience`). `auth_method`
    controls how the client authenticates: `client_secret_post` sends the
    credentials in the request body (default), `client_secret_basic` sends them
    via HTTP Basic auth.
    """

    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    audience: str | None = None
    auth_method: str = "client_secret_post"
    extra_params: dict[str, str] = field(default_factory=dict)


def fetch_client_credentials_token(
    credentials: ClientCredentials,
    *,
    http_client: httpx.Client | None = None,
    timeout_seconds: int = DEFAULT_CLIENT_CREDENTIALS_TIMEOUT_SECONDS,
) -> str:
    """Perform a client credentials grant and return the access token.

    Headless clients call this to mint their own short-lived bearer token from
    stable `client_id` / `client_secret` — no browser and no refresh token
    required. The returned token is sent as `Authorization: Bearer <token>`.

    Raises `httpx.HTTPStatusError` if the token endpoint returns an error,
    `ValueError` if the response contains no `access_token`, and `ValueError`
    if `credentials.auth_method` is not one of `SUPPORTED_CLIENT_AUTH_METHODS`.
    """
    if credentials.auth_method not in SUPPORTED_CLIENT_AUTH_METHODS:
        raise ValueError(
            f"Unsupported auth_method {credentials.auth_method!r}; expected one "
            f"of {SUPPORTED_CLIENT_AUTH_METHODS}."
        )

    data: dict[str, str] = {"grant_type": "client_credentials"}
    if credentials.scope:
        data["scope"] = credentials.scope
    if credentials.audience:
        data["audience"] = credentials.audience
    data.update(credentials.extra_params)

    post_kwargs: dict[str, Any] = {"data": data}
    if credentials.auth_method == "client_secret_basic":
        post_kwargs["auth"] = httpx.BasicAuth(
            credentials.client_id, credentials.client_secret
        )
    else:
        data["client_id"] = credentials.client_id
        data["client_secret"] = credentials.client_secret

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds)
    try:
        response = client.post(credentials.token_url, **post_kwargs)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            client.close()

    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise ValueError("Token endpoint response did not contain an 'access_token'.")
    return access_token
