# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Hash and obfuscation processes for anonymized telemetry.

Servers can provide a salt shared across every server whose surrogates should
be comparable. A salt derived from per-process state makes the surrogates
identify instances rather than callers.

The `caller` HMAC scope label is part of the telemetry wire contract. Caller
surrogates prefer verified token subjects, then OAuth client IDs, then IPs.
IP fallback is disabled by default and must be explicitly enabled.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Sequence
from typing import NamedTuple, TypeVar
from urllib.parse import urlsplit

from fastmcp.server.dependencies import (
    get_access_token,
    get_context,
    get_http_request,
)

_T = TypeVar("_T")


class _CallerIdentity(NamedTuple):
    value: str
    id_type: str


class _ClientInfo(NamedTuple):
    name: str | None
    version: str | None


def _safe_value(resolver: Callable[[], _T]) -> _T | None:
    try:
        value = resolver()
    except RuntimeError:
        return None
    return value or None


def _hash_value(
    value: str,
    scope_label: str,
    endpoint: str = "local",
    salt: str | None = None,
) -> str | None:
    if salt is None:
        return None
    message = f"{scope_label}|{endpoint}|{value}".encode()
    return hmac.new(salt.encode(), message, hashlib.sha256).hexdigest()[:16]


def _session_id() -> str | None:
    return get_context().session_id


def _caller_ip() -> str | None:
    request = get_http_request()
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    return request.client.host if request.client else None


def _auth_identity() -> _CallerIdentity | None:
    access_token = get_access_token()
    if access_token is None:
        return None
    claims = access_token.claims or {}
    subject = claims.get("sub")
    if isinstance(subject, str) and subject:
        return _CallerIdentity(subject, "subject")
    client_id = claims.get("client_id") or access_token.client_id
    if isinstance(client_id, str) and client_id:
        return _CallerIdentity(client_id, "client")
    return None


def _request_host() -> str | None:
    request = get_http_request()
    host = request.headers.get("host")
    if not host:
        return None
    host = host.strip().lower()
    if not host:
        return None
    if host.startswith("["):
        closing_bracket = host.find("]")
        if closing_bracket == -1:
            return host
        hostname = host[: closing_bracket + 1].rstrip(".")
        port = host[closing_bracket + 1 :]
    else:
        hostname, separator, port = host.rpartition(":")
        if not separator:
            hostname, port = host, ""
        hostname = hostname.rstrip(".")
    if port in {":80", ":443"} or port == "80" or port == "443":
        port = ""
    return f"{hostname}{port}"


def _request_endpoint(host: str) -> str:
    request = get_http_request()
    path = request.url.path
    return f"{host}{path}" if path and path != "/" else host


def _client_info() -> _ClientInfo:
    context = get_context()
    client_params = context.session.client_params
    if client_params is None or client_params.clientInfo is None:
        return _ClientInfo(None, None)
    client_info = client_params.clientInfo
    return _ClientInfo(client_info.name, client_info.version)


def _is_owned_endpoint(host: str, domains: Sequence[str]) -> bool:
    hostname = urlsplit(f"//{host}").hostname
    if not hostname:
        return False
    return any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in domains
    )


class _AnonymizedAttribution:
    """Resolve privacy-safe attribution properties for one telemetry middleware."""

    def __init__(
        self,
        *,
        known_public_mcp_domains: Sequence[str] = (),
        anonymization_salt: str | Callable[[], str | None] | None = None,
        caller_ip_fallback: bool = False,
    ) -> None:
        self._known_public_mcp_domains = tuple(
            domain.rstrip(".").lower() for domain in known_public_mcp_domains
        )
        self._anonymization_salt = anonymization_salt
        self._caller_ip_fallback = caller_ip_fallback
        self._salt_resolved = False
        self._salt: str | None = None

    def _get_salt(self) -> str | None:
        if not self._salt_resolved:
            self._salt = _safe_value(self._resolve_salt)
            self._salt_resolved = True
        return self._salt

    def _resolve_salt(self) -> str | None:
        if isinstance(self._anonymization_salt, str):
            return self._anonymization_salt
        if self._anonymization_salt is not None:
            return self._anonymization_salt()
        return None

    def __call__(self) -> dict[str, object]:
        properties: dict[str, object] = {}
        salt = self._get_salt()
        if salt is None:
            return properties

        host = _safe_value(_request_host)
        endpoint_context = host or "local"

        session_id = _safe_value(_session_id)
        if session_id is not None:
            session_id_hash = _hash_value(
                session_id,
                "session",
                endpoint_context,
                salt,
            )
            if session_id_hash is not None:
                properties["session_id_hash"] = session_id_hash

        caller_identity = _safe_value(_auth_identity)
        caller_id_type: str | None = None
        if caller_identity is not None:
            caller_value = caller_identity.value
            caller_id_type = caller_identity.id_type
        elif self._caller_ip_fallback:
            caller_value = _safe_value(_caller_ip)
            if caller_value is not None:
                caller_id_type = "ip"
        else:
            caller_value = None
        if caller_value is not None and caller_id_type is not None:
            caller_hash = _hash_value(
                caller_value,
                "caller",
                endpoint_context,
                salt,
            )
            if caller_hash is not None:
                properties["caller_hash"] = caller_hash
                properties["caller_id_type"] = caller_id_type

        if host is not None:
            endpoint_hash = _hash_value(host, "endpoint", host, salt)
            if endpoint_hash is not None:
                properties["mcp_endpoint_hash"] = endpoint_hash
            endpoint = _safe_value(lambda: _request_endpoint(host)) or host
            if _safe_value(
                lambda: _is_owned_endpoint(host, self._known_public_mcp_domains)
            ):
                properties["mcp_endpoint"] = endpoint

        client_data = _safe_value(_client_info)
        if client_data is not None:
            client_name = client_data.name
            client_version = client_data.version
            if client_name:
                properties["mcp_client_name"] = client_name
            if client_version:
                properties["mcp_client_version"] = client_version

        return properties
