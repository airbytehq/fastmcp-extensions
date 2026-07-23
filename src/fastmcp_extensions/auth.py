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
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp.server.auth import AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier, StaticTokenVerifier
from key_value.aio.protocols.key_value import AsyncKeyValue

from fastmcp_extensions.utils.env import get_env

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_CREDENTIALS_TIMEOUT_SECONDS = 30

OIDC_CLIENT_STORAGE_FACTORY_ENV = "MCP_OIDC_CLIENT_STORAGE_FACTORY"
"""Env var naming a zero-argument factory that builds the interactive
`OIDCProxy`'s durable OAuth-state store.

Format is a `pkgutil`-style target (`package.module:callable` or
`package.module.callable`). When set and no store is passed explicitly to
`resolve_mcp_auth`, the named callable is imported and invoked to produce an
`AsyncKeyValue` (or `None`). This keeps the library backend-agnostic: the
deployment owns the concrete backend and its infrastructure-specific config
(project, database, encryption, etc.) inside the factory, while this library
only resolves and calls it.
"""

SUPPORTED_CLIENT_AUTH_METHODS = ("client_secret_post", "client_secret_basic")


@dataclass(kw_only=True)
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
    enable_cimd: bool = False
    """Whether to advertise and accept the Client ID Metadata Document (CIMD)
    flow, in which a client passes a URL as its `client_id` and the server
    fetches that document to resolve the client.

    Defaults to `False`. CIMD is an experimental extension to Dynamic Client
    Registration (DCR): when enabled, `OIDCProxy` advertises
    `client_id_metadata_document_supported: true`, and compliant clients (e.g.
    Goose Desktop) will send a URL `client_id` instead of registering via DCR.
    On a proxied MCP deployment (e.g. Cloud Run behind a path-stripping load
    balancer), resolving the synthetic CIMD client — which has no fixed
    redirect URIs — fails and the `/authorize` request returns HTTP 500
    (observed regardless of the OAuth-proxy storage backend), so the
    advertised capability is a trap for those clients. Leaving CIMD off makes
    them fall back to DCR (`/register`), which is the mandated baseline and
    works on those deployments. Mirrors `OIDCProxy(enable_cimd=...)`, whose own
    default is `True`.

    Via the env-based entry point (`resolve_mcp_auth`), set this with the
    `OIDC_ENABLE_CIMD` environment variable (accepts `1`/`true`/`yes`/`on` and
    `0`/`false`/`no`/`off`, case-insensitive).
    """
    forward_resource: bool = False
    """Whether to forward the client's RFC 8707 `resource` indicator to the
    upstream IdP token request.

    MCP clients send `resource=<this MCP server's URL>` so the issued token is
    audience-bound to the MCP server. `OIDCProxy` swaps that MCP-facing token
    for the **upstream** access token and exposes the upstream token to tools
    (via `get_access_token`), which servers then reuse as the bearer for
    downstream first-party APIs. If the `resource` indicator is forwarded, the
    upstream IdP narrows the upstream token's audience to the MCP server, and
    the downstream API rejects it (`401`).

    Defaults to `False` so the upstream token keeps its default audience and
    stays valid for downstream API calls — the token-reuse pattern this factory
    is built for. Set to `True` only when the upstream token is never reused
    downstream and strict per-resource audience binding is required. Mirrors
    `OIDCProxy(forward_resource=...)`, whose own default is `True`.

    Via the env-based entry point (`resolve_mcp_auth`), set this with the
    `OIDC_FORWARD_RESOURCE` environment variable (accepts `1`/`true`/`yes`/`on`
    and `0`/`false`/`no`/`off`, case-insensitive).
    """
    client_storage: AsyncKeyValue | None = None
    """Durable backend for `OIDCProxy`'s OAuth state (upstream access + refresh
    tokens, JTI mappings, and dynamic client registrations).

    `OIDCProxy` defaults to an in-process store, so its refresh tokens are lost
    on restart and are not shared across replicas — every restart or scale event
    forces interactive users to re-authenticate. Supplying a shared, durable,
    encrypted `key_value.aio.protocols.key_value.AsyncKeyValue` (e.g. a
    Fernet-wrapped Redis store) makes long-lived sessions survive restarts and
    work across replicas. Leave `None` to keep `OIDCProxy`'s default in-memory
    behavior (fine for single-instance local dev). This library stays backend-
    agnostic: the caller constructs the store and injects it here (or via
    `resolve_mcp_auth`'s `oidc_client_storage` argument).
    """


@dataclass(kw_only=True)
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


@dataclass(kw_only=True)
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
    proxy_kwargs: dict[str, Any] = {
        "config_url": config.config_url,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "base_url": resolved_base_url,
        "audience": config.audience,
        "required_scopes": config.required_scopes,
        "enable_cimd": config.enable_cimd,
        "forward_resource": config.forward_resource,
    }
    if config.client_storage is not None:
        # Only override `OIDCProxy`'s default in-memory store when a durable
        # backend was supplied, so unconfigured callers keep the default.
        proxy_kwargs["client_storage"] = config.client_storage
    return OIDCProxy(**proxy_kwargs)


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


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_bool(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    """Parse a boolean env var, returning `default` when unset or empty.

    Accepts `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off` (case-insensitive).
    Raises `ValueError` for any other non-empty value so a typo fails loudly
    instead of silently falling back to the default.
    """
    raw = get_env(env, key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if not normalized:
        return default
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ValueError(
        f"Invalid boolean value for {key}: {raw!r}. "
        f"Expected one of {sorted(_TRUTHY | _FALSY)}."
    )


def _resolve_oidc_client_storage(
    env: Mapping[str, str],
    explicit: AsyncKeyValue | None,
) -> AsyncKeyValue | None:
    """Resolve the interactive `OIDCProxy`'s durable store.

    An `explicit` store passed by the caller always wins. Otherwise, if
    `MCP_OIDC_CLIENT_STORAGE_FACTORY` names a factory, it is imported and
    invoked to build one; a misconfigured target raises so a durability
    misconfiguration fails loudly at startup rather than silently degrading to
    the in-memory default. When neither is provided, returns `None` and
    `OIDCProxy` keeps its in-memory store.
    """
    if explicit is not None:
        return explicit
    factory_target = get_env(env, OIDC_CLIENT_STORAGE_FACTORY_ENV, "")
    if not factory_target:
        return None
    factory: Callable[[], AsyncKeyValue | None] = pkgutil.resolve_name(factory_target)
    logger.info(
        "Building OIDC client storage from factory %s (%s)",
        factory_target,
        OIDC_CLIENT_STORAGE_FACTORY_ENV,
    )
    return factory()


def resolve_mcp_auth(
    env: Mapping[str, str] | None = None,
    *,
    jwt_defaults: JWTAuthConfig | None = None,
    oidc_client_storage: AsyncKeyValue | None = None,
) -> AuthProvider | None:
    """Build an `AuthProvider` from a standard set of environment variables.

    This is the convenience entry point that lets any server opt into headless
    auth "for free". It reads the following variables and delegates to
    `build_mcp_auth`:

    Interactive OIDC (`OIDCProxy`):

    - `OIDC_CONFIG_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
    - `MCP_SERVER_URL` (base URL for redirect callbacks)
    - `OIDC_AUDIENCE` (optional)
    - `OIDC_ENABLE_CIMD` (optional bool, default off)
    - `OIDC_FORWARD_RESOURCE` (optional bool, default off)

    `oidc_client_storage` supplies a durable, shared backend for the interactive
    `OIDCProxy`'s OAuth state (see `OIDCAuthConfig.client_storage`). It is passed
    as an object rather than resolved from env because this library stays
    backend-agnostic — the caller constructs the store (e.g. a Fernet-wrapped
    Firestore store) and injects it. A caller that cannot inject the object
    (e.g. a shared entrypoint whose `app` is built at import time) can instead
    set `MCP_OIDC_CLIENT_STORAGE_FACTORY` to a `pkgutil`-style factory target
    (`package.module:callable`); it is imported and invoked to build the store,
    keeping all backend-specific config inside the deployment's factory. An
    explicit `oidc_client_storage` argument takes precedence over the env
    factory. When neither is provided, `OIDCProxy` keeps its default in-memory
    store, so refresh tokens do not survive restarts or span replicas.

    Headless JWT bearer verification (`JWTVerifier`):

    - `MCP_AUTH_JWKS_URI` (or `MCP_AUTH_JWT_PUBLIC_KEY`)
    - `MCP_AUTH_ISSUER`, `MCP_AUTH_AUDIENCE` (optional but recommended)
    - `MCP_AUTH_ALGORITHM` (optional)

    `jwt_defaults` lets a caller supply a batteries-included JWT verifier realm
    (issuer / JWKS URI / audience / algorithm) without baking any provider
    literals into this library. The headless verifier is configured whenever
    `jwt_defaults` is given *or* an `MCP_AUTH_JWKS_URI` / `MCP_AUTH_JWT_PUBLIC_KEY`
    is set; each `MCP_AUTH_*` variable overrides the matching `jwt_defaults`
    field, so a deployment can point at its own realm while still getting the
    supplied defaults for anything it leaves unset. Adopters gate this behind
    their own env flag by passing `jwt_defaults` only when that flag is set.

    Headless opaque-token introspection (`IntrospectionTokenVerifier`):

    - `MCP_AUTH_INTROSPECTION_URL`
    - `MCP_AUTH_INTROSPECTION_CLIENT_ID` / `MCP_AUTH_INTROSPECTION_CLIENT_SECRET`
      (falling back to `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`)

    Shared:

    - `MCP_AUTH_REQUIRED_SCOPES` (comma or space separated)

    Interactive OIDC requires all of `OIDC_CONFIG_URL`, `OIDC_CLIENT_ID`, and
    `OIDC_CLIENT_SECRET`; when `OIDC_CONFIG_URL` is set but the client
    credentials are missing a warning is logged and interactive auth is left
    disabled. (`OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` without `OIDC_CONFIG_URL`
    are treated as introspection fallback credentials, not partial OIDC, so
    they do not warn.) Returns `None` when no auth is configured, so an HTTP
    caller can decide whether to run unauthenticated.
    """
    env = os.environ if env is None else env
    base_url = get_env(env, "MCP_SERVER_URL")
    required_scopes = _split_scopes(get_env(env, "MCP_AUTH_REQUIRED_SCOPES"))

    oidc: OIDCAuthConfig | None = None
    oidc_config_url = get_env(env, "OIDC_CONFIG_URL", "")
    oidc_client_id = get_env(env, "OIDC_CLIENT_ID", "")
    oidc_client_secret = get_env(env, "OIDC_CLIENT_SECRET", "")
    if oidc_config_url and oidc_client_id and oidc_client_secret:
        logger.info(
            "Interactive OIDC auth enabled (config_url=%s, base_url=%s)",
            oidc_config_url,
            base_url,
        )
        oidc = OIDCAuthConfig(
            config_url=oidc_config_url,
            client_id=oidc_client_id,
            client_secret=oidc_client_secret,
            base_url=base_url,
            audience=get_env(env, "OIDC_AUDIENCE"),
            client_storage=_resolve_oidc_client_storage(env, oidc_client_storage),
            enable_cimd=_env_bool(env, "OIDC_ENABLE_CIMD", default=False),
            forward_resource=_env_bool(env, "OIDC_FORWARD_RESOURCE", default=False),
        )
    elif oidc_config_url:
        logger.warning(
            "Incomplete interactive OIDC configuration: OIDC_CONFIG_URL is set "
            "but OIDC_CLIENT_ID and/or OIDC_CLIENT_SECRET are missing. "
            "Interactive OIDC auth is disabled."
        )

    jwt: JWTAuthConfig | None = None
    jwks_uri = get_env(env, "MCP_AUTH_JWKS_URI", "")
    jwt_public_key = get_env(env, "MCP_AUTH_JWT_PUBLIC_KEY", "")
    if jwks_uri or jwt_public_key or jwt_defaults is not None:
        resolved_jwks = (
            jwks_uri or (jwt_defaults.jwks_uri if jwt_defaults else None) or None
        )
        resolved_public_key = (
            jwt_public_key
            or (jwt_defaults.public_key if jwt_defaults else None)
            or None
        )
        issuer = get_env(env, "MCP_AUTH_ISSUER") or (
            jwt_defaults.issuer if jwt_defaults else None
        )
        audience = get_env(env, "MCP_AUTH_AUDIENCE") or (
            jwt_defaults.audience if jwt_defaults else None
        )
        algorithm = get_env(env, "MCP_AUTH_ALGORITHM") or (
            jwt_defaults.algorithm if jwt_defaults else None
        )
        logger.info(
            "Headless bearer-token auth enabled (jwks_uri=%s, issuer=%s, audience=%s)",
            resolved_jwks or "<static public key>",
            issuer,
            audience,
        )
        jwt = JWTAuthConfig(
            jwks_uri=resolved_jwks,
            public_key=resolved_public_key,
            issuer=issuer,
            audience=audience,
            algorithm=algorithm,
            required_scopes=required_scopes
            or (jwt_defaults.required_scopes if jwt_defaults else None),
            base_url=base_url or (jwt_defaults.base_url if jwt_defaults else None),
        )

    introspection: IntrospectionAuthConfig | None = None
    introspection_url = get_env(env, "MCP_AUTH_INTROSPECTION_URL")
    if introspection_url:
        client_id = get_env(env, "MCP_AUTH_INTROSPECTION_CLIENT_ID") or get_env(
            env, "OIDC_CLIENT_ID"
        )
        client_secret = get_env(env, "MCP_AUTH_INTROSPECTION_CLIENT_SECRET") or get_env(
            env, "OIDC_CLIENT_SECRET"
        )
        if not client_id or not client_secret:
            raise ValueError(
                "MCP_AUTH_INTROSPECTION_URL is set but no introspection client "
                "credentials were found. Set MCP_AUTH_INTROSPECTION_CLIENT_ID "
                "and MCP_AUTH_INTROSPECTION_CLIENT_SECRET (or OIDC_CLIENT_ID / "
                "OIDC_CLIENT_SECRET)."
            )
        introspection = IntrospectionAuthConfig(
            introspection_url=introspection_url,
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


@dataclass(kw_only=True)
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


def build_client_credentials_post_kwargs(
    credentials: ClientCredentials,
) -> dict[str, Any]:
    """Return the `httpx.post` kwargs for a client credentials grant request.

    Shapes the request per `credentials.auth_method`: `client_secret_post` puts
    the credentials in the form body, `client_secret_basic` sends them via HTTP
    Basic auth. Scope, audience, and `extra_params` are added when set. Raises
    `ValueError` if `auth_method` is not one of `SUPPORTED_CLIENT_AUTH_METHODS`.
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
    return post_kwargs


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
    post_kwargs = build_client_credentials_post_kwargs(credentials)

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout_seconds)
    try:
        response = client.post(credentials.token_url, **post_kwargs)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            client.close()

    if not isinstance(payload, dict):
        raise ValueError("Token endpoint response was not a JSON object.")
    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise ValueError("Token endpoint response did not contain an 'access_token'.")
    return access_token
