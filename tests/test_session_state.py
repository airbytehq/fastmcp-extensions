from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from fastmcp import Client, FastMCP

from fastmcp_extensions import (
    EncodedSessionStateConfig,
    EncodedSessionStateError,
    ToolStateBase,
    decode_session_state,
    encode_session_state,
    mcp_server,
    mcp_tool,
    register_mcp_tools,
)
from fastmcp_extensions.decorators import _clear_registrations
from fastmcp_extensions.session_state import current_principal


class BasketState(ToolStateBase, state_ttl=timedelta(seconds=30)):
    count: int = 0
    label: str = ""


def test_state_round_trip_and_mutation() -> None:
    config = EncodedSessionStateConfig(
        signing="required",
        secret="secret",
        principal_binding=True,
    )
    original = BasketState(count=2, label="basket")
    token = encode_session_state(original, config, principal="alice", now=100)

    decoded = decode_session_state(
        token,
        BasketState,
        config,
        principal="alice",
        now=100,
    )

    assert decoded == original
    decoded.count += 1
    next_token = encode_session_state(decoded, config, principal="alice", now=101)
    assert (
        decode_session_state(
            next_token,
            BasketState,
            config,
            principal="alice",
            now=101,
        ).count
        == 3
    )


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        pytest.param("expired", "expired; create new state", id="expired"),
        pytest.param("tampered", "invalid signature", id="tampered_signature"),
        pytest.param("wrong_principal", "another user", id="wrong_principal"),
        pytest.param(
            "signed_for_unsigned", "not unsigned", id="signed_unsigned_mismatch"
        ),
        pytest.param(
            "unsigned_for_signed", "not signed", id="unsigned_signed_mismatch"
        ),
        pytest.param("unknown_version", "unsupported version", id="unknown_version"),
        pytest.param("malformed_base64", "invalid format", id="malformed_base64"),
    ],
)
def test_state_decode_rejections_are_actionable(
    case: str,
    expected_message: str,
) -> None:
    signed = EncodedSessionStateConfig(
        signing="required",
        secret="secret",
        principal_binding=True,
    )
    unsigned = EncodedSessionStateConfig(signing="disabled")
    signed_token = encode_session_state(
        BasketState(),
        signed,
        principal="alice",
        now=100,
    )
    unsigned_token = encode_session_state(
        BasketState(),
        unsigned,
        principal=None,
        now=100,
    )
    config = signed
    token = signed_token
    principal = "alice"
    now = 100
    if case == "expired":
        config, token, principal, now = unsigned, unsigned_token, None, 131
    elif case == "tampered":
        raw = bytearray(
            base64.urlsafe_b64decode(signed_token + "=" * (-len(signed_token) % 4))
        )
        raw[-1] ^= 1
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    elif case == "wrong_principal":
        principal = "bob"
    elif case == "signed_for_unsigned":
        config = unsigned
    elif case == "unsigned_for_signed":
        config, token, principal = signed, unsigned_token, None
    elif case == "unknown_version":
        raw = bytearray(
            base64.urlsafe_b64decode(unsigned_token + "=" * (-len(unsigned_token) % 4))
        )
        raw[0] = 2
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        config, principal = unsigned, None
    elif case == "malformed_base64":
        token = "not valid base64"
        config, principal = unsigned, None

    with pytest.raises(EncodedSessionStateError, match=expected_message):
        decode_session_state(token, BasketState, config, principal=principal, now=now)


def test_rotated_signing_key_is_accepted() -> None:
    old = EncodedSessionStateConfig(signing="required", secret="old", key_id="old")
    rotated = EncodedSessionStateConfig(
        signing="required",
        secret="new",
        key_id="new",
        previous_secrets={"old": "old"},
    )
    token = encode_session_state(BasketState(), old, principal=None, now=100)
    assert (
        decode_session_state(
            token,
            BasketState,
            rotated,
            principal=None,
            now=100,
        )
        == BasketState()
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("attribute_typo", id="attribute_typo_uses_default_ttl"),
        pytest.param("unknown_keyword", id="unknown_class_keyword_fails"),
    ],
)
def test_state_class_configuration_contract(case: str) -> None:
    if case == "attribute_typo":

        class DefaultState(ToolStateBase):
            _state_tt1 = timedelta(seconds=1)

        assert not hasattr(DefaultState, "state_ttl")
        token = encode_session_state(
            DefaultState(),
            EncodedSessionStateConfig(signing="disabled"),
            principal=None,
            now=0,
        )
        with pytest.raises(EncodedSessionStateError, match="expired"):
            decode_session_state(
                token,
                DefaultState,
                EncodedSessionStateConfig(signing="disabled"),
                principal=None,
                now=30 * 24 * 60 * 60 + 1,
            )
    else:
        with pytest.raises(TypeError):

            class InvalidState(ToolStateBase, unknown=True):
                pass


def test_active_signing_key_cannot_be_reused_as_previous() -> None:
    with pytest.raises(ValueError, match="active and previous signing keys"):
        EncodedSessionStateConfig(
            signing="required",
            secret="current",
            key_id="current",
            previous_secrets={"current": "old"},
        )


def test_stateful_tool_schema_and_result() -> None:
    _clear_registrations()

    @mcp_tool(with_state=BasketState)
    def increment(*, input_state: BasketState) -> str:
        input_state.count += 1
        return str(input_state.count)

    app = FastMCP("test")
    app.x_mcp_extensions_session_state = EncodedSessionStateConfig(  # type: ignore[attr-defined]
        signing="disabled"
    )
    register_mcp_tools(app, mcp_module="test_session_state")
    tool = next(
        component
        for component in app._local_provider._components.values()  # type: ignore[attr-defined]
        if component.name == "increment"
    )
    schema = tool.parameters
    assert "encoded_session_state" in schema["properties"]
    assert "input_state" not in schema["properties"]
    description = schema["properties"]["encoded_session_state"]["description"]
    assert "30 seconds" in description
    assert "0:00:30" not in description


def test_required_principal_without_authentication_fails() -> None:
    with pytest.raises(EncodedSessionStateError, match="authenticate"):
        current_principal(required=True)


@pytest.mark.asyncio
async def test_stateful_tool_returns_named_result_and_threads_state() -> None:
    _clear_registrations()

    @mcp_tool(with_state=BasketState)
    def increment(*, input_state: BasketState) -> str:
        input_state.count += 1
        return str(input_state.count)

    app = FastMCP("test")
    app.x_mcp_extensions_session_state = EncodedSessionStateConfig(  # type: ignore[attr-defined]
        signing="disabled"
    )
    register_mcp_tools(app, mcp_module="test_session_state")
    async with Client(app) as client:
        first = await client.call_tool(
            "increment",
            {"encoded_session_state": None},
        )
        second = await client.call_tool(
            "increment",
            {
                "encoded_session_state": first.structured_content[
                    "encoded_session_state"
                ]
            },
        )
    assert first.structured_content["result"] == "1"
    assert second.structured_content["result"] == "2"


def test_stateful_tool_without_server_config_fails_at_registration() -> None:
    _clear_registrations()

    @mcp_tool(with_state=BasketState)
    def increment(*, input_state: BasketState) -> str:
        input_state.count += 1
        return str(input_state.count)

    with pytest.raises(
        RuntimeError,
        match=r"signing='required'.*signing='disabled'",
    ):
        register_mcp_tools(FastMCP("test"), mcp_module="test_session_state")


def test_explicit_unsigned_state_config_warns(caplog: pytest.LogCaptureFixture) -> None:
    _clear_registrations()

    @mcp_tool(with_state=BasketState)
    def increment(*, input_state: BasketState) -> str:
        return str(input_state.count)

    app = mcp_server(
        "test",
        encoded_session_state=EncodedSessionStateConfig(signing="disabled"),
    )
    with caplog.at_level("WARNING"):
        register_mcp_tools(app, mcp_module="test_session_state")

    assert "signing is explicitly disabled" in caplog.text


def test_unrecognized_authenticated_user_fails_principal_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class User:
        def __init__(self) -> None:
            self.claims: dict[str, str] = {}

    class Request:
        def __init__(self) -> None:
            self.scope = {"user": User()}

    request = Request()
    monkeypatch.setattr(
        "fastmcp_extensions.session_state.get_http_request",
        lambda: request,
    )
    with pytest.raises(EncodedSessionStateError, match="authenticate"):
        current_principal(required=True)
