# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the reusable MCP auth factory and token-exchange helper."""

import httpx
import pytest
from fastmcp.server.auth import AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.providers.jwt import (
    JWTVerifier,
    RSAKeyPair,
    StaticTokenVerifier,
)

from fastmcp_extensions.auth import (
    ClientCredentials,
    IntrospectionAuthConfig,
    JWTAuthConfig,
    _assemble_auth,
    build_mcp_auth,
    fetch_client_credentials_token,
    resolve_mcp_auth,
)

_PUBLIC_KEY = RSAKeyPair.generate().public_key


def _static_verifier(name: str = "tok") -> StaticTokenVerifier:
    return StaticTokenVerifier({name: {"client_id": "test", "scopes": []}})


@pytest.mark.unit
def test_build_mcp_auth_returns_none_when_unconfigured() -> None:
    assert build_mcp_auth() is None


@pytest.mark.unit
def test_build_mcp_auth_single_jwt_returns_verifier_directly() -> None:
    auth = build_mcp_auth(
        jwt=JWTAuthConfig(public_key=_PUBLIC_KEY, issuer="iss", audience="aud")
    )
    assert isinstance(auth, JWTVerifier)


@pytest.mark.unit
def test_build_mcp_auth_single_introspection_returns_verifier_directly() -> None:
    auth = build_mcp_auth(
        introspection=IntrospectionAuthConfig(
            introspection_url="https://idp.example/introspect",
            client_id="cid",
            client_secret="sec",
        )
    )
    assert isinstance(auth, TokenVerifier)
    assert not isinstance(auth, MultiAuth)


@pytest.mark.unit
def test_build_mcp_auth_multiple_verifiers_returns_multiauth() -> None:
    auth = build_mcp_auth(
        jwt=JWTAuthConfig(public_key=_PUBLIC_KEY),
        static_tokens={"tok": {"client_id": "test", "scopes": []}},
    )
    assert isinstance(auth, MultiAuth)


@pytest.mark.unit
def test_jwt_config_requires_key_material() -> None:
    with pytest.raises(ValueError, match=r"jwks_uri.*public_key"):
        JWTAuthConfig()


@pytest.mark.unit
@pytest.mark.parametrize(
    "has_server,num_verifiers,required_scopes,expected",
    [
        pytest.param(False, 0, None, type(None), id="nothing"),
        pytest.param(True, 0, None, "server", id="server-only"),
        pytest.param(True, 2, None, MultiAuth, id="server-plus-verifiers"),
        pytest.param(False, 1, None, "verifier", id="single-verifier"),
        pytest.param(False, 2, None, MultiAuth, id="multiple-verifiers"),
        pytest.param(False, 1, ["scope"], MultiAuth, id="single-verifier-with-scopes"),
    ],
)
def test_assemble_auth_branches(
    has_server: bool,
    num_verifiers: int,
    required_scopes: list[str] | None,
    expected: object,
) -> None:
    server: AuthProvider | None = _static_verifier("server") if has_server else None
    verifiers = [_static_verifier(f"v{i}") for i in range(num_verifiers)]

    result = _assemble_auth(
        server=server,
        verifiers=verifiers,  # type: ignore[arg-type]
        base_url=None,
        required_scopes=required_scopes,
    )

    if expected == "server":
        assert result is server
    elif expected == "verifier":
        assert result is verifiers[0]
    elif expected is type(None):
        assert result is None
    else:
        assert isinstance(result, expected)  # type: ignore[arg-type]


@pytest.mark.unit
def test_resolve_mcp_auth_none_when_env_empty() -> None:
    assert resolve_mcp_auth(env={}) is None


@pytest.mark.unit
def test_resolve_mcp_auth_headless_jwt_from_env() -> None:
    auth = resolve_mcp_auth(
        env={
            "MCP_AUTH_JWKS_URI": "https://idp.example/.well-known/jwks.json",
            "MCP_AUTH_ISSUER": "https://idp.example/",
            "MCP_AUTH_AUDIENCE": "mcp-api",
        }
    )
    assert isinstance(auth, JWTVerifier)


@pytest.mark.unit
def test_resolve_mcp_auth_introspection_falls_back_to_oidc_client() -> None:
    auth = resolve_mcp_auth(
        env={
            "MCP_AUTH_INTROSPECTION_URL": "https://idp.example/introspect",
            "OIDC_CLIENT_ID": "cid",
            "OIDC_CLIENT_SECRET": "sec",
        }
    )
    assert isinstance(auth, TokenVerifier)


@pytest.mark.unit
def test_resolve_mcp_auth_introspection_missing_creds_raises() -> None:
    with pytest.raises(ValueError, match="introspection client"):
        resolve_mcp_auth(
            env={"MCP_AUTH_INTROSPECTION_URL": "https://idp.example/introspect"}
        )


def _token_transport(captured: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"access_token": "minted-token"})

    return httpx.MockTransport(handler)


@pytest.mark.unit
def test_fetch_client_credentials_token_post_includes_body_creds() -> None:
    captured: dict[str, object] = {}
    client = httpx.Client(transport=_token_transport(captured))

    token = fetch_client_credentials_token(
        ClientCredentials(
            token_url="https://idp.example/token",
            client_id="cid",
            client_secret="sec",
            scope="read:things",
            audience="mcp-api",
        ),
        http_client=client,
    )

    assert token == "minted-token"
    body = str(captured["body"])
    assert "grant_type=client_credentials" in body
    assert "client_id=cid" in body
    assert "client_secret=sec" in body
    assert "scope=read" in body
    assert "audience=mcp-api" in body
    assert captured["authorization"] is None


@pytest.mark.unit
def test_fetch_client_credentials_token_basic_auth() -> None:
    captured: dict[str, object] = {}
    client = httpx.Client(transport=_token_transport(captured))

    token = fetch_client_credentials_token(
        ClientCredentials(
            token_url="https://idp.example/token",
            client_id="cid",
            client_secret="sec",
            auth_method="client_secret_basic",
        ),
        http_client=client,
    )

    assert token == "minted-token"
    assert str(captured["authorization"]).startswith("Basic ")
    assert "client_secret=sec" not in str(captured["body"])


@pytest.mark.unit
def test_fetch_client_credentials_token_missing_token_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="access_token"):
        fetch_client_credentials_token(
            ClientCredentials(
                token_url="https://idp.example/token",
                client_id="cid",
                client_secret="sec",
            ),
            http_client=client,
        )


@pytest.mark.unit
def test_fetch_client_credentials_token_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_client_credentials_token(
            ClientCredentials(
                token_url="https://idp.example/token",
                client_id="cid",
                client_secret="bad",
            ),
            http_client=client,
        )
