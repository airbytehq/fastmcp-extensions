# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the reusable MCP auth factory and token-exchange helper."""

import logging

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
    OIDCAuthConfig,
    _assemble_auth,
    _env_bool,
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
def test_resolve_mcp_auth_jwt_defaults_applied() -> None:
    auth = resolve_mcp_auth(
        env={},
        jwt_defaults=JWTAuthConfig(
            jwks_uri="https://realm.example/jwks.json",
            issuer="https://realm.example/",
            audience="realm-aud",
        ),
    )
    assert isinstance(auth, JWTVerifier)
    assert auth.jwks_uri == "https://realm.example/jwks.json"
    assert auth.issuer == "https://realm.example/"
    assert auth.audience == "realm-aud"


@pytest.mark.unit
def test_resolve_mcp_auth_env_overrides_jwt_defaults() -> None:
    auth = resolve_mcp_auth(
        env={"MCP_AUTH_ISSUER": "https://custom.example/"},
        jwt_defaults=JWTAuthConfig(
            jwks_uri="https://realm.example/jwks.json",
            issuer="https://realm.example/",
            audience="realm-aud",
        ),
    )
    assert isinstance(auth, JWTVerifier)
    assert auth.jwks_uri == "https://realm.example/jwks.json"
    assert auth.issuer == "https://custom.example/"


@pytest.mark.unit
def test_resolve_mcp_auth_none_without_jwt_defaults_or_env() -> None:
    assert resolve_mcp_auth(env={}, jwt_defaults=None) is None


@pytest.mark.unit
def test_resolve_mcp_auth_partial_oidc_warns_and_disables(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        auth = resolve_mcp_auth(
            env={"OIDC_CONFIG_URL": "https://idp.example/.well-known/openid"}
        )
    assert auth is None
    assert any(
        "Incomplete interactive OIDC configuration" in record.message
        for record in caplog.records
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param(None, False, id="unset_uses_default"),
        pytest.param("", False, id="empty_uses_default"),
        pytest.param("   ", False, id="whitespace_only_uses_default"),
        pytest.param("true", True, id="true"),
        pytest.param("TRUE", True, id="true_uppercase"),
        pytest.param("1", True, id="one"),
        pytest.param("yes", True, id="yes"),
        pytest.param("on", True, id="on"),
        pytest.param("false", False, id="false"),
        pytest.param("0", False, id="zero"),
        pytest.param(" no ", False, id="no_whitespace"),
    ],
)
def test_env_bool(raw: str | None, expected: bool) -> None:
    env = {} if raw is None else {"FLAG": raw}
    assert _env_bool(env, "FLAG", default=False) is expected


@pytest.mark.unit
def test_env_bool_invalid_raises() -> None:
    with pytest.raises(ValueError, match="Invalid boolean value for FLAG"):
        _env_bool({"FLAG": "maybe"}, "FLAG", default=False)


class _CapturingOIDCProxy:
    """Stand-in for `OIDCProxy` that records kwargs without network I/O.

    The real `OIDCProxy` fetches the OIDC discovery document at construction,
    so these plumbing tests substitute this fake to assert what
    `_build_oidc_proxy` / `resolve_mcp_auth` pass through.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_kwargs,expected",
    [
        pytest.param({}, False, id="default_false"),
        pytest.param({"forward_resource": True}, True, id="explicit_true"),
        pytest.param({"forward_resource": False}, False, id="explicit_false"),
    ],
)
def test_build_mcp_auth_forwards_resource_flag(
    monkeypatch: pytest.MonkeyPatch,
    config_kwargs: dict[str, bool],
    expected: bool,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    auth = build_mcp_auth(
        oidc=OIDCAuthConfig(
            config_url="https://idp.example/.well-known/openid-configuration",
            client_id="cid",
            client_secret="sec",
            base_url="https://mcp.example",
            **config_kwargs,
        )
    )
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["forward_resource"] is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "env_value,expected",
    [
        pytest.param(None, False, id="unset_defaults_false"),
        pytest.param("true", True, id="env_true"),
        pytest.param("false", False, id="env_false"),
    ],
)
def test_resolve_mcp_auth_oidc_forward_resource_from_env(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    expected: bool,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    env = {
        "OIDC_CONFIG_URL": "https://idp.example/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "cid",
        "OIDC_CLIENT_SECRET": "sec",
        "MCP_SERVER_URL": "https://mcp.example",
    }
    if env_value is not None:
        env["OIDC_FORWARD_RESOURCE"] = env_value
    auth = resolve_mcp_auth(env=env)
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["forward_resource"] is expected


@pytest.mark.unit
def test_oidc_auth_config_is_keyword_only() -> None:
    # The auth config dataclasses are `kw_only=True`, so a boolean (or any
    # value) can never silently bind to the wrong field via positional args.
    with pytest.raises(TypeError):
        OIDCAuthConfig(  # type: ignore[misc]
            "https://idp.example/.well-known/openid-configuration",
            "cid",
        )


@pytest.mark.unit
def test_build_mcp_auth_omits_client_storage_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    auth = build_mcp_auth(
        oidc=OIDCAuthConfig(
            config_url="https://idp.example/.well-known/openid-configuration",
            client_id="cid",
            client_secret="sec",
            base_url="https://mcp.example",
        )
    )
    assert isinstance(auth, _CapturingOIDCProxy)
    # Left unset so OIDCProxy keeps its own default in-memory store.
    assert "client_storage" not in auth.kwargs


@pytest.mark.unit
def test_build_mcp_auth_forwards_client_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    store = object()
    auth = build_mcp_auth(
        oidc=OIDCAuthConfig(
            config_url="https://idp.example/.well-known/openid-configuration",
            client_id="cid",
            client_secret="sec",
            base_url="https://mcp.example",
            client_storage=store,  # type: ignore[arg-type]
        )
    )
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["client_storage"] is store


@pytest.mark.unit
def test_resolve_mcp_auth_forwards_client_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    store = object()
    auth = resolve_mcp_auth(
        env={
            "OIDC_CONFIG_URL": "https://idp.example/.well-known/openid-configuration",
            "OIDC_CLIENT_ID": "cid",
            "OIDC_CLIENT_SECRET": "sec",
            "MCP_SERVER_URL": "https://mcp.example",
        },
        oidc_client_storage=store,  # type: ignore[arg-type]
    )
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["client_storage"] is store


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

    with httpx.Client(transport=_token_transport(captured)) as client:
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

    with httpx.Client(transport=_token_transport(captured)) as client:
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
def test_fetch_client_credentials_token_unsupported_auth_method_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported auth_method"):
        fetch_client_credentials_token(
            ClientCredentials(
                token_url="https://idp.example/token",
                client_id="cid",
                client_secret="sec",
                auth_method="private_key_jwt",
            ),
        )


@pytest.mark.unit
def test_fetch_client_credentials_token_missing_token_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "bearer"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(
        ValueError, match="access_token"
    ):
        fetch_client_credentials_token(
            ClientCredentials(
                token_url="https://idp.example/token",
                client_id="cid",
                client_secret="sec",
            ),
            http_client=client,
        )


@pytest.mark.unit
def test_fetch_client_credentials_token_non_object_json_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(
        ValueError, match="JSON object"
    ):
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

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(
        httpx.HTTPStatusError
    ):
        fetch_client_credentials_token(
            ClientCredentials(
                token_url="https://idp.example/token",
                client_id="cid",
                client_secret="bad",
            ),
            http_client=client,
        )
