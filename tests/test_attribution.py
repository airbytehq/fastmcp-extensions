# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for privacy-safe telemetry attribution."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult

from fastmcp_extensions import TelemetryConfig
from fastmcp_extensions._attribution import (
    _AnonymizedAttribution,
    _hash_value,
)
from fastmcp_extensions._telemetry_middleware import (
    ToolCallTelemetryMiddleware,
)


def _context(
    *,
    session_id: str = "session-secret",
    client_name: str | None = None,
    client_version: str | None = None,
) -> SimpleNamespace:
    client_info = (
        SimpleNamespace(name=client_name, version=client_version)
        if client_name is not None and client_version is not None
        else None
    )
    return SimpleNamespace(
        session_id=session_id,
        session=SimpleNamespace(
            client_params=(
                SimpleNamespace(clientInfo=client_info)
                if client_info is not None
                else None
            )
        ),
    )


def _request(
    *,
    host: str = "preview.airbyte.ai",
    path: str = "/mcp",
    forwarded_for: str | None = None,
) -> SimpleNamespace:
    headers = {"host": host}
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(path=path),
    )


def _tool_context() -> MiddlewareContext:
    context = MagicMock(spec=MiddlewareContext)
    context.message = MagicMock()
    context.message.name = "test_tool"
    return context


def _tool_result() -> ToolResult:
    return ToolResult(content="ok")


def _no_http_request() -> None:
    raise RuntimeError("no HTTP request")


def _no_access_token() -> None:
    return None


def test_hosted_attribution_includes_all_available_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted requests include scoped identities and owned endpoint metadata."""
    from fastmcp_extensions import _attribution as attribution

    monkeypatch.setenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", "test-salt")
    monkeypatch.setattr(
        attribution,
        "get_context",
        lambda: _context(
            client_name="Cloud Client",
            client_version="1.2.3",
        ),
    )
    monkeypatch.setattr(
        attribution,
        "get_http_request",
        lambda: _request(forwarded_for="198.51.100.23, 192.0.2.10"),
    )
    monkeypatch.setattr(
        attribution,
        "get_access_token",
        lambda: SimpleNamespace(
            claims={"sub": "subject-secret", "client_id": "client-id"},
            client_id="fallback-client-id",
        ),
    )

    properties = _AnonymizedAttribution(known_public_mcp_domains=("airbyte.ai",))()

    assert properties == {
        "session_id_hash": _hash_value(
            "session-secret", "session", "preview.airbyte.ai", "test-salt"
        ),
        "caller_hash": _hash_value(
            "subject-secret", "caller", "preview.airbyte.ai", "test-salt"
        ),
        "caller_id_type": "subject",
        "mcp_endpoint_hash": _hash_value(
            "preview.airbyte.ai",
            "endpoint",
            "preview.airbyte.ai",
            "test-salt",
        ),
        "mcp_endpoint": "preview.airbyte.ai/mcp",
        "mcp_client_name": "Cloud Client",
        "mcp_client_version": "1.2.3",
    }


def test_stdio_attribution_includes_session_and_client_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stdio requests use local context attribution without HTTP-only fields."""
    from fastmcp_extensions import _attribution as attribution

    monkeypatch.setenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", "test-salt")
    monkeypatch.setattr(
        attribution,
        "get_context",
        lambda: _context(client_name="Claude Desktop", client_version="9.8.7"),
    )
    monkeypatch.setattr(attribution, "get_http_request", _no_http_request)
    monkeypatch.setattr(attribution, "get_access_token", _no_access_token)

    properties = _AnonymizedAttribution()()

    assert properties == {
        "session_id_hash": _hash_value(
            "session-secret", "session", "local", "test-salt"
        ),
        "mcp_client_name": "Claude Desktop",
        "mcp_client_version": "9.8.7",
    }
    assert "caller_hash" not in properties
    assert "mcp_endpoint_hash" not in properties
    assert "mcp_endpoint" not in properties


@pytest.mark.parametrize(
    ("host", "expected_endpoint"),
    [
        pytest.param(
            "Preview.Airbyte.AI.",
            "Preview.Airbyte.AI./mcp",
            id="owned-subdomain-case-and-trailing-dot",
        ),
        pytest.param("customer.example.com", None, id="third-party-host"),
    ],
)
def test_endpoint_attribution_respects_plaintext_domain_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    expected_endpoint: str | None,
) -> None:
    """Only configured owned domains receive plaintext endpoint metadata."""
    from fastmcp_extensions import _attribution as attribution

    monkeypatch.setenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", "test-salt")
    monkeypatch.setattr(attribution, "get_context", _context)
    monkeypatch.setattr(attribution, "get_http_request", lambda: _request(host=host))
    monkeypatch.setattr(attribution, "get_access_token", _no_access_token)

    properties = _AnonymizedAttribution(known_public_mcp_domains=("airbyte.ai",))()

    assert properties["mcp_endpoint_hash"] == _hash_value(
        host, "endpoint", host, "test-salt"
    )
    if expected_endpoint is None:
        assert "mcp_endpoint" not in properties
    else:
        assert properties["mcp_endpoint"] == expected_endpoint


@pytest.mark.parametrize(
    ("token", "caller_ip_fallback", "expected_value", "expected_type"),
    [
        pytest.param(
            SimpleNamespace(
                claims={"sub": "subject-secret", "client_id": "client-id"},
                client_id="fallback-client-id",
            ),
            False,
            "subject-secret",
            "subject",
            id="verified-subject",
        ),
        pytest.param(
            SimpleNamespace(claims={"client_id": "client-id"}, client_id=None),
            False,
            "client-id",
            "client",
            id="oauth-client-id",
        ),
        pytest.param(
            None,
            False,
            None,
            None,
            id="unauthenticated-ip-fallback-disabled",
        ),
        pytest.param(
            None,
            True,
            "198.51.100.23",
            "ip",
            id="unauthenticated-forwarded-ip-fallback",
        ),
    ],
)
def test_caller_attribution_uses_identity_precedence(
    monkeypatch: pytest.MonkeyPatch,
    token: SimpleNamespace | None,
    caller_ip_fallback: bool,
    expected_value: str | None,
    expected_type: str | None,
) -> None:
    """Caller attribution uses subject, client, then IP without competing fields."""
    from fastmcp_extensions import _attribution as attribution

    monkeypatch.setenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", "test-salt")
    monkeypatch.setattr(attribution, "get_context", _context)
    monkeypatch.setattr(
        attribution,
        "get_http_request",
        lambda: _request(forwarded_for="198.51.100.23, 192.0.2.10"),
    )
    monkeypatch.setattr(attribution, "get_access_token", lambda: token)
    if token is not None or not caller_ip_fallback:
        monkeypatch.setattr(
            attribution,
            "_caller_ip",
            lambda: pytest.fail("authenticated caller must not read IP"),
        )

    properties = _AnonymizedAttribution(caller_ip_fallback=caller_ip_fallback)()

    if expected_value is None:
        assert "caller_hash" not in properties
        assert "caller_id_type" not in properties
    else:
        assert properties["caller_hash"] == _hash_value(
            expected_value, "caller", "preview.airbyte.ai", "test-salt"
        )
        assert properties["caller_id_type"] == expected_type
    assert "auth_subject_hash" not in properties


def test_attribution_has_no_salt_and_instance_local_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing salt omits attribution and fallback salts stay instance-local."""
    monkeypatch.delenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", raising=False)
    first_calls = 0
    second_calls = 0

    def first_fallback() -> str:
        nonlocal first_calls
        first_calls += 1
        return "first-salt"

    def second_fallback() -> str:
        nonlocal second_calls
        second_calls += 1
        return "second-salt"

    first = _AnonymizedAttribution(anonymization_salt_fallback=first_fallback)
    second = _AnonymizedAttribution(anonymization_salt_fallback=second_fallback)
    assert first._get_salt() == "first-salt"
    assert first._get_salt() == "first-salt"
    assert second._get_salt() == "second-salt"
    assert first_calls == 1
    assert second_calls == 1

    no_salt = _AnonymizedAttribution(anonymization_salt_fallback=lambda: None)
    assert no_salt() == {}


@pytest.mark.asyncio
async def test_extra_properties_override_attribution_and_can_disable_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server properties override attribution and the config can disable it."""
    from fastmcp_extensions import _attribution as attribution

    monkeypatch.setenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", "test-salt")
    monkeypatch.setattr(attribution, "get_context", _context)
    monkeypatch.setattr(
        attribution,
        "get_http_request",
        lambda: _request(forwarded_for="198.51.100.23"),
    )
    monkeypatch.setattr(attribution, "get_access_token", _no_access_token)

    middleware = ToolCallTelemetryMiddleware(
        known_public_mcp_domains=("airbyte.ai",),
        caller_ip_fallback=True,
        extra_properties={"caller_hash": "server-value"},
    )
    emit = MagicMock()
    middleware._sinks.emit = emit

    async def call_next(_: MiddlewareContext) -> ToolResult:
        return _tool_result()

    await middleware.on_call_tool(_tool_context(), call_next)
    assert emit.call_args.args[0].to_dict()["caller_hash"] == "server-value"

    disabled = TelemetryConfig(anonymized_attribution=False)
    disabled_middleware = ToolCallTelemetryMiddleware(
        anonymized_attribution=disabled.anonymized_attribution,
        extra_properties={"is_hosted_mcp": True},
    )
    disabled_emit = MagicMock()
    disabled_middleware._sinks.emit = disabled_emit
    await disabled_middleware.on_call_tool(_tool_context(), call_next)
    assert disabled_emit.call_args.args[0].extra == {"is_hosted_mcp": True}


def test_attribution_payload_contains_no_raw_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw IP, session, subject, and third-party host never enter the payload."""
    from fastmcp_extensions import _attribution as attribution

    raw_ip = "198.51.100.42"
    raw_session = "session-secret"
    raw_subject = "subject-secret"
    monkeypatch.setenv("AIRBYTE_TELEMETRY_ANONYMIZATION_SALT", "test-salt")
    monkeypatch.setattr(
        attribution,
        "get_context",
        lambda: _context(session_id=raw_session),
    )
    monkeypatch.setattr(
        attribution,
        "get_http_request",
        lambda: _request(host="customer.example.com", forwarded_for=raw_ip),
    )
    monkeypatch.setattr(
        attribution,
        "get_access_token",
        lambda: SimpleNamespace(claims={"sub": raw_subject}, client_id=None),
    )

    payload = json.dumps(_AnonymizedAttribution()())

    assert raw_ip not in payload
    assert raw_session not in payload
    assert raw_subject not in payload
    assert "customer.example.com" not in payload


@pytest.mark.parametrize(
    "scope_label",
    [
        pytest.param("session", id="session-scope"),
        pytest.param("caller", id="caller-scope"),
        pytest.param("endpoint", id="endpoint-scope"),
    ],
)
def test_hash_is_keyed_and_stably_scoped(scope_label: str) -> None:
    """Attribution uses keyed HMACs and changing the salt changes the digest."""
    value = _hash_value("same-value", scope_label, "local", "first-salt")
    assert value == _hash_value("same-value", scope_label, "local", "first-salt")
    assert value != _hash_value("same-value", scope_label, "local", "second-salt")
    assert (
        value
        == hmac.new(
            b"first-salt",
            f"{scope_label}|local|same-value".encode(),
            hashlib.sha256,
        ).hexdigest()[:16]
    )
