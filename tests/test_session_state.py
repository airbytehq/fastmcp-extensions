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
    mcp_tool,
    register_mcp_tools,
)
from fastmcp_extensions.decorators import _clear_registrations


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


def test_expired_state_is_actionable() -> None:
    config = EncodedSessionStateConfig(signing="disabled")
    token = encode_session_state(BasketState(), config, principal=None, now=100)

    with pytest.raises(EncodedSessionStateError, match="expired; create new state"):
        decode_session_state(token, BasketState, config, principal=None, now=131)


def test_tampered_state_and_wrong_principal_are_rejected() -> None:
    config = EncodedSessionStateConfig(
        signing="required",
        secret="secret",
        principal_binding=True,
    )
    token = encode_session_state(BasketState(), config, principal="alice", now=100)
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[-1] ^= 1
    tampered = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    with pytest.raises(EncodedSessionStateError, match="invalid signature"):
        decode_session_state(tampered, BasketState, config, principal="alice", now=100)
    with pytest.raises(EncodedSessionStateError, match="another user"):
        decode_session_state(token, BasketState, config, principal="bob", now=100)


def test_signed_and_unsigned_tokens_do_not_cross_configurations() -> None:
    signed = EncodedSessionStateConfig(signing="required", secret="secret")
    unsigned = EncodedSessionStateConfig(signing="disabled")
    signed_token = encode_session_state(BasketState(), signed, principal=None, now=100)
    unsigned_token = encode_session_state(
        BasketState(), unsigned, principal=None, now=100
    )

    with pytest.raises(EncodedSessionStateError, match="not unsigned"):
        decode_session_state(
            signed_token, BasketState, unsigned, principal=None, now=100
        )
    with pytest.raises(EncodedSessionStateError, match="not signed"):
        decode_session_state(
            unsigned_token, BasketState, signed, principal=None, now=100
        )


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


def test_state_ttl_is_class_configuration_only() -> None:
    class DefaultState(ToolStateBase):
        _state_tt1 = timedelta(seconds=1)

    assert not hasattr(DefaultState, "state_ttl")
    token = encode_session_state(
        DefaultState(), EncodedSessionStateConfig(), principal=None, now=0
    )
    with pytest.raises(EncodedSessionStateError, match="expired"):
        decode_session_state(
            token,
            DefaultState,
            EncodedSessionStateConfig(),
            principal=None,
            now=30 * 24 * 60 * 60 + 1,
        )

    with pytest.raises(TypeError):

        class InvalidState(ToolStateBase, unknown=True):
            pass


def test_stateful_tool_schema_and_result() -> None:
    _clear_registrations()

    @mcp_tool(with_state=BasketState)
    def increment(*, input_state: BasketState) -> str:
        input_state.count += 1
        return str(input_state.count)

    app = FastMCP("test")
    app.x_mcp_extensions_session_state = EncodedSessionStateConfig()  # type: ignore[attr-defined]
    register_mcp_tools(app, mcp_module="test_session_state")
    tool = next(
        component
        for component in app._local_provider._components.values()  # type: ignore[attr-defined]
        if component.name == "increment"
    )
    schema = tool.parameters
    assert "encoded_session_state" in schema["properties"]
    assert "input_state" not in schema["properties"]


def test_required_principal_without_authentication_fails() -> None:
    with pytest.raises(EncodedSessionStateError, match="authenticate"):
        from fastmcp_extensions.session_state import current_principal

        current_principal(required=True)


@pytest.mark.asyncio
async def test_stateful_tool_returns_named_result_and_threads_state() -> None:
    _clear_registrations()

    @mcp_tool(with_state=BasketState)
    def increment(*, input_state: BasketState) -> str:
        input_state.count += 1
        return str(input_state.count)

    app = FastMCP("test")
    app.x_mcp_extensions_session_state = EncodedSessionStateConfig()  # type: ignore[attr-defined]
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
