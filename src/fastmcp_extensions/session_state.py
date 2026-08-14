# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Explicit encoded state handles for stateless MCP tools."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, TypeVar, cast

import msgspec
from fastmcp.server.dependencies import get_http_request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_STATE_TTL = timedelta(days=30)
DEFAULT_STATE_SECRET_ENV_VAR = "FASTMCP_SESSION_STATE_SECRET"
_TOKEN_VERSION = 2
_SIGNATURE_SIZE = hashlib.sha256().digest_size
_STATE_TYPES: dict[type[ToolStateBase], timedelta] = {}

StateT = TypeVar("StateT", bound="ToolStateBase")


class _ToolStateMeta(type(msgspec.Struct)):
    def __new__(
        metaclass: type[_ToolStateMeta],
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> _ToolStateMeta:
        state_ttl_value = kwargs.pop("state_ttl", DEFAULT_STATE_TTL)
        cls = super().__new__(metaclass, name, bases, namespace, **kwargs)
        if name != "ToolStateBase":
            if not isinstance(
                state_ttl_value, timedelta
            ) or state_ttl_value <= timedelta(0):
                raise ValueError("state_ttl must be a positive timedelta")
            _STATE_TYPES[cast(type[ToolStateBase], cls)] = state_ttl_value
        return cls


class ToolStateBase(msgspec.Struct, metaclass=_ToolStateMeta):
    """Base class for state carried between calls to a stateless MCP tool."""


@dataclass(frozen=True, kw_only=True)
class EncodedSessionStateConfig:
    """Server-level policy for encoded session-state handles."""

    signing: Literal["required", "disabled"]
    secret: str | bytes | None = None
    previous_secrets: Mapping[str, str | bytes] = field(default_factory=dict)
    key_id: str = "current"
    secret_env_var: str = DEFAULT_STATE_SECRET_ENV_VAR
    principal_binding: bool = False
    enable_state_inspection_tool: bool = True

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("key_id must not be empty")
        if self.key_id in self.previous_secrets:
            raise ValueError(
                "key_id must not also appear in previous_secrets; active and previous "
                "signing keys must be distinct"
            )
        if self.signing == "required":
            self.resolve_secrets()

    def resolve_secrets(self) -> dict[str, bytes]:
        """Resolve the active and previous signing secrets."""
        if self.signing == "disabled":
            if self.secret is not None or self.previous_secrets:
                raise ValueError(
                    "Encoded session-state signing is disabled, but signing secrets were configured"
                )
            return {}

        secret = self.secret
        if secret is None:
            secret = os.getenv(self.secret_env_var)
        if not secret:
            raise ValueError(
                f"Encoded session-state signing is required, but {self.secret_env_var} is not set"
            )
        secrets = {self.key_id: _as_bytes(secret)}
        secrets.update(
            {
                key_id: _as_bytes(value)
                for key_id, value in self.previous_secrets.items()
            }
        )
        return secrets


class EncodedSessionStateError(ValueError):
    """An actionable encoded session-state validation error."""


class DecodedSessionState(BaseModel):
    """Decoded state-handle fields and validation metadata."""

    valid: bool
    state_type: str | None = None
    expires_at: int | None = None
    key_id: str | None = None
    signed: bool | None = None
    seconds_remaining: int | None = None
    state: dict[str, Any] | None = None
    error: str | None = None


class _TokenEnvelope(msgspec.Struct, kw_only=True):
    expires_at: int
    principal: str | None
    key_id: str | None
    state_type: str
    state: dict[str, Any]


def state_ttl(state_type: type[ToolStateBase]) -> timedelta:
    """Return the TTL configured for a `ToolStateBase` subclass."""
    try:
        return _STATE_TYPES[state_type]
    except KeyError as exc:
        raise TypeError(
            f"{state_type.__name__} must inherit from ToolStateBase"
        ) from exc


def _state_type_name(state_type: type[ToolStateBase]) -> str:
    return f"{state_type.__module__}.{state_type.__qualname__}"


def encode_session_state(
    state: StateT,
    config: EncodedSessionStateConfig,
    *,
    principal: str | None,
    now: int | None = None,
) -> str:
    """Encode and optionally sign a state object."""
    secrets = config.resolve_secrets()
    key_id = config.key_id if secrets else None
    bound_principal = principal if config.principal_binding else None
    envelope = _TokenEnvelope(
        expires_at=(now if now is not None else int(time.time()))
        + int(state_ttl(type(state)).total_seconds()),
        principal=bound_principal,
        key_id=key_id,
        state_type=_state_type_name(type(state)),
        state=msgspec.to_builtins(state),
    )
    payload = msgspec.msgpack.encode(envelope)
    token_version = _TOKEN_VERSION | (0x80 if secrets else 0)
    token_body = bytes([token_version]) + payload
    if secrets:
        token_body += hmac.new(
            secrets[config.key_id], token_body, hashlib.sha256
        ).digest()
    return base64.urlsafe_b64encode(token_body).decode("ascii").rstrip("=")


def _decode_token_envelope(
    token: str,
    config: EncodedSessionStateConfig,
) -> tuple[_TokenEnvelope, bool]:
    try:
        token_body = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeError) as exc:
        raise EncodedSessionStateError(
            "This state handle has an invalid format; create new state."
        ) from exc
    if not token_body or token_body[0] & 0x7F != _TOKEN_VERSION:
        raise EncodedSessionStateError(
            "This state handle uses an unsupported version; create new state."
        )

    secrets = config.resolve_secrets()
    signed = bool(token_body[0] & 0x80)
    if bool(secrets) != signed:
        mode = "signed" if secrets else "unsigned"
        raise EncodedSessionStateError(
            f"This state handle is not {mode} as required by this server; create new state."
        )
    if signed:
        signed_body = token_body[:-_SIGNATURE_SIZE]
        signature = token_body[-_SIGNATURE_SIZE:]
        try:
            envelope = msgspec.msgpack.decode(signed_body[1:], type=_TokenEnvelope)
        except (msgspec.DecodeError, TypeError, ValueError) as exc:
            raise EncodedSessionStateError(
                "This state handle has an invalid format; create new state."
            ) from exc
        signing_secret = secrets.get(envelope.key_id or "")
        if signing_secret is None or not hmac.compare_digest(
            signature,
            hmac.new(signing_secret, signed_body, hashlib.sha256).digest(),
        ):
            raise EncodedSessionStateError(
                "This state handle has an invalid signature; create new state."
            )
    else:
        try:
            envelope = msgspec.msgpack.decode(token_body[1:], type=_TokenEnvelope)
        except (msgspec.DecodeError, TypeError, ValueError) as exc:
            raise EncodedSessionStateError(
                "This state handle has an invalid format; create new state."
            ) from exc
    return envelope, signed


def _validate_token_metadata(
    envelope: _TokenEnvelope,
    config: EncodedSessionStateConfig,
    *,
    principal: str | None,
    now: int | None = None,
) -> int:
    current_time = now if now is not None else int(time.time())
    if envelope.expires_at <= current_time:
        raise EncodedSessionStateError("This state has expired; create new state.")
    if config.principal_binding and envelope.principal != principal:
        raise EncodedSessionStateError(
            "This state belongs to another user; create new state."
        )
    if not config.principal_binding and envelope.principal is not None:
        raise EncodedSessionStateError(
            "This state handle was minted with principal binding enabled; create new state."
        )
    return current_time


def decode_session_state(
    token: str,
    state_type: type[StateT],
    config: EncodedSessionStateConfig,
    *,
    principal: str | None,
    now: int | None = None,
) -> StateT:
    """Decode, authenticate, and validate an encoded state handle."""
    envelope, _ = _decode_token_envelope(token, config)
    _validate_token_metadata(envelope, config, principal=principal, now=now)
    if envelope.state_type != _state_type_name(state_type):
        raise EncodedSessionStateError(
            "This state handle is for a different tool; create new state."
        )
    try:
        return msgspec.convert(envelope.state, type=state_type)
    except (msgspec.ValidationError, TypeError, ValueError) as exc:
        raise EncodedSessionStateError(
            "This state handle contains incompatible state; create new state."
        ) from exc


def inspect_session_state(
    token: str,
    config: EncodedSessionStateConfig,
    state_types: Collection[type[ToolStateBase]],
    *,
    principal: str | None,
    now: int | None = None,
) -> DecodedSessionState:
    """Inspect and validate an encoded state handle without a declared type."""
    try:
        envelope, signed = _decode_token_envelope(token, config)
        current_time = _validate_token_metadata(
            envelope,
            config,
            principal=principal,
            now=now,
        )
    except EncodedSessionStateError as exc:
        return DecodedSessionState(valid=False, error=str(exc))

    known_types = {
        _state_type_name(state_type): state_type for state_type in state_types
    }
    result = DecodedSessionState(
        valid=True,
        state_type=envelope.state_type,
        expires_at=envelope.expires_at,
        key_id=envelope.key_id,
        signed=signed,
        seconds_remaining=envelope.expires_at - current_time,
        state=envelope.state,
    )
    if envelope.state_type not in known_types:
        result.valid = False
        result.error = (
            "This state handle names a state type this server does not know; "
            "create new state."
        )
    else:
        try:
            msgspec.convert(envelope.state, type=known_types[envelope.state_type])
        except (msgspec.ValidationError, TypeError, ValueError):
            result.valid = False
            result.error = (
                "This state handle contains incompatible state; create new state."
            )
    return result


def current_principal(*, required: bool) -> str | None:
    """Read the authenticated principal from the active FastMCP request."""
    principal: object | None = None
    try:
        request = get_http_request()
    except RuntimeError:
        request = None
    if request is not None:
        user = request.scope.get("user")
        claims = getattr(user, "claims", {})
        for claim in ("sub", "user_id", "client_id"):
            value = claims.get(claim) if isinstance(claims, Mapping) else None
            if value:
                principal = value
                break
        for attribute in ("sub", "user_id", "client_id", "identity"):
            if principal is not None:
                break
            value = getattr(user, attribute, None)
            if value:
                principal = value
                break
    if required and principal is None:
        raise EncodedSessionStateError(
            "This server requires an authenticated principal for state; authenticate and create new state."
        )
    return None if principal is None else str(principal)


def _as_bytes(value: str | bytes) -> bytes:
    return value.encode() if isinstance(value, str) else value
