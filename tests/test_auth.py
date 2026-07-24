# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the reusable MCP auth factory and token-exchange helper.

This library exposes a **pure, typed** auth-construction API (`build_mcp_auth`
plus the `*AuthConfig` dataclasses); it deliberately reads **no environment
variables**. Each MCP server owns its own env-var names and maps them into
these config objects, so these tests exercise the typed API directly.
"""

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
    build_mcp_auth,
    fetch_client_credentials_token,
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


class _CapturingOIDCProxy:
    """Stand-in for `OIDCProxy` that records kwargs without network I/O.

    The real `OIDCProxy` fetches the OIDC discovery document at construction,
    so these plumbing tests substitute this fake to assert what
    `_build_oidc_proxy` passes through.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def _oidc_config(**overrides: object) -> OIDCAuthConfig:
    kwargs: dict[str, object] = {
        "config_url": "https://idp.example/.well-known/openid-configuration",
        "client_id": "cid",
        "client_secret": "sec",
        "base_url": "https://mcp.example",
    }
    kwargs.update(overrides)
    return OIDCAuthConfig(**kwargs)  # type: ignore[arg-type]


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
    auth = build_mcp_auth(oidc=_oidc_config(**config_kwargs))
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["forward_resource"] is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_kwargs,expected",
    [
        pytest.param({}, False, id="default_false"),
        pytest.param({"enable_cimd": True}, True, id="explicit_true"),
        pytest.param({"enable_cimd": False}, False, id="explicit_false"),
    ],
)
def test_build_mcp_auth_forwards_enable_cimd_flag(
    monkeypatch: pytest.MonkeyPatch,
    config_kwargs: dict[str, bool],
    expected: bool,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    auth = build_mcp_auth(oidc=_oidc_config(**config_kwargs))
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["enable_cimd"] is expected


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
    auth = build_mcp_auth(oidc=_oidc_config())
    assert isinstance(auth, _CapturingOIDCProxy)
    # Left unset so OIDCProxy keeps its own default in-memory store.
    assert "client_storage" not in auth.kwargs


@pytest.mark.unit
def test_build_mcp_auth_forwards_client_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    store = object()
    auth = build_mcp_auth(oidc=_oidc_config(client_storage=store))
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["client_storage"] is store


@pytest.mark.unit
def test_build_mcp_auth_oidc_requires_base_url() -> None:
    # `base_url` may live on the config or be passed to build_mcp_auth; with
    # neither, construction fails loudly rather than building a broken proxy.
    with pytest.raises(ValueError, match="base_url"):
        build_mcp_auth(
            oidc=OIDCAuthConfig(
                config_url="https://idp.example/.well-known/openid-configuration",
                client_id="cid",
                client_secret="sec",
            )
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
