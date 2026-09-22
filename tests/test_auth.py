# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the reusable MCP auth factory and token-exchange helper.

This library exposes a **pure, typed** auth-construction API (`build_mcp_auth`
plus the `*AuthConfig` dataclasses); it deliberately reads **no environment
variables**. Each MCP server owns its own env-var names and maps them into
these config objects, so these tests exercise the typed API directly.
"""

import functools
import inspect

import httpx
import pytest
from fastmcp.server.auth import AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
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
        verifiers=verifiers,  # ty: ignore[invalid-argument-type]  # The test passes a deliberately invalid verifier collection.
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
        assert isinstance(result, expected)  # ty: ignore[invalid-argument-type]  # The parametrized test supplies runtime type objects.


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
    return OIDCAuthConfig(**kwargs)  # ty: ignore[invalid-argument-type]  # The test builds configuration from intentionally broad values.


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
@pytest.mark.parametrize(
    "config_kwargs,expected",
    [
        pytest.param({}, True, id="default_true"),
        pytest.param(
            {"require_authorization_consent": "external"},
            "external",
            id="external",
        ),
        pytest.param(
            {"require_authorization_consent": False}, False, id="explicit_false"
        ),
    ],
)
def test_build_mcp_auth_forwards_require_authorization_consent(
    monkeypatch: pytest.MonkeyPatch,
    config_kwargs: dict[str, object],
    expected: object,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    auth = build_mcp_auth(oidc=_oidc_config(**config_kwargs))
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["require_authorization_consent"] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_kwargs,expected",
    [
        pytest.param({}, None, id="default_none"),
        pytest.param(
            {"extra_authorize_params": {"prompt": "consent"}},
            {"prompt": "consent"},
            id="prompt_consent",
        ),
    ],
)
def test_build_mcp_auth_forwards_extra_authorize_params(
    monkeypatch: pytest.MonkeyPatch,
    config_kwargs: dict[str, object],
    expected: dict[str, str] | None,
) -> None:
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    auth = build_mcp_auth(oidc=_oidc_config(**config_kwargs))
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["extra_authorize_params"] == expected


@pytest.mark.unit
def test_build_mcp_auth_proxy_factory_defaults_to_none() -> None:
    assert _oidc_config().proxy_factory is None


@pytest.mark.unit
def test_build_mcp_auth_uses_proxy_factory_with_same_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A subclass supplied via `proxy_factory` must get exactly the kwargs the
    # stock `OIDCProxy` would have, so it inherits the same wiring. The
    # factory itself is a build-time hook, not a proxy setting, so it is not
    # forwarded.
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    store = object()

    class _SubclassProxy(_CapturingOIDCProxy):
        pass

    baseline = build_mcp_auth(oidc=_oidc_config(client_storage=store))
    auth = build_mcp_auth(
        oidc=_oidc_config(client_storage=store, proxy_factory=_SubclassProxy)
    )
    assert isinstance(baseline, _CapturingOIDCProxy)
    assert isinstance(auth, _SubclassProxy)
    assert auth.kwargs == baseline.kwargs
    assert "proxy_factory" not in auth.kwargs


@pytest.mark.unit
def test_build_mcp_auth_proxy_factory_accepts_partial_with_bound_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The intended shape: `functools.partial(Subclass, extra=...)` binds the
    # subclass's own keyword-only arguments while the standard proxy kwargs
    # still arrive from `build_mcp_auth`.
    monkeypatch.setattr("fastmcp_extensions.auth.OIDCProxy", _CapturingOIDCProxy)
    factory = functools.partial(_CapturingOIDCProxy, realm_hint="acme")
    auth = build_mcp_auth(oidc=_oidc_config(proxy_factory=factory))
    assert isinstance(auth, _CapturingOIDCProxy)
    assert auth.kwargs["realm_hint"] == "acme"
    assert auth.kwargs["client_id"] == "cid"
    assert auth.kwargs["base_url"] == "https://mcp.example"


@pytest.mark.unit
def test_oidc_proxy_accepts_every_forwarded_kwarg() -> None:
    # The passthrough tests swap in `_CapturingOIDCProxy`, so they stay green
    # even if the resolved FastMCP drops one of these kwargs. This library
    # supports `fastmcp>=3.0,<4.0`, and `_build_oidc_proxy` passes them all
    # unconditionally, so a rename or removal is a `TypeError` at startup.
    params = inspect.signature(OIDCProxy.__init__).parameters
    for kwarg in (
        "audience",
        "required_scopes",
        "enable_cimd",
        "forward_resource",
        "require_authorization_consent",
        "extra_authorize_params",
        "client_storage",
    ):
        assert kwarg in params, f"OIDCProxy no longer accepts {kwarg!r}"


@pytest.mark.unit
def test_oidc_auth_config_is_keyword_only() -> None:
    # The auth config dataclasses are `kw_only=True`, so a boolean (or any
    # value) can never silently bind to the wrong field via positional args.
    with pytest.raises(TypeError):
        OIDCAuthConfig(  # ty: ignore[missing-argument]  # The test verifies rejection of an invalid configuration.
            "https://idp.example/.well-known/openid-configuration",  # ty: ignore[too-many-positional-arguments]  # The test intentionally supplies positional values to a keyword-only configuration.
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
