# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Hash and obfuscation processes for anonymized telemetry.

Deployments set `AIRBYTE_TELEMETRY_ANONYMIZATION_SALT` to a secret value shared
across every server whose surrogates should be comparable. Servers may supply a
fallback for environments without that secret, but a fallback derived from
per-process state makes the surrogates identify instances rather than callers.

The `caller` HMAC scope label is part of the telemetry wire contract. Caller
surrogates prefer verified token subjects, then OAuth client IDs, then IPs.
IP fallback is disabled by default and must be explicitly enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Sequence
from typing import TypeVar
from urllib.parse import urlsplit

from fastmcp.server.dependencies import (
    get_access_token,
    get_context,
    get_http_request,
)

_TELEMETRY_ANONYMIZATION_SALT_ENV = "AIRBYTE_TELEMETRY_ANONYMIZATION_SALT"
_T = TypeVar("_T")


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


def _auth_identity() -> tuple[str, str] | None:
    access_token = get_access_token()
    if access_token is None:
        return None
    claims = access_token.claims or {}
    subject = claims.get("sub")
    if isinstance(subject, str) and subject:
        return subject, "subject"
    client_id = claims.get("client_id") or access_token.client_id
    if isinstance(client_id, str) and client_id:
        return client_id, "client"
    return None


def _request_host() -> str | None:
    request = get_http_request()
    host = request.headers.get("host")
    if not host:
        return None
    host = host.strip()
    return host or None


def _request_endpoint(host: str) -> str:
    request = get_http_request()
    path = request.url.path
    return f"{host}{path}" if path and path != "/" else host


def _client_info() -> tuple[str | None, str | None]:
    context = get_context()
    client_params = context.session.client_params
    if client_params is None or client_params.clientInfo is None:
        return None, None
    client_info = client_params.clientInfo
    return client_info.name, client_info.version


def _is_owned_endpoint(host: str, domains: Sequence[str]) -> bool:
    hostname = urlsplit(f"//{host}").hostname
    if not hostname:
        return False
    hostname = hostname.rstrip(".").lower()
    return any(
        hostname == domain.rstrip(".").lower()
        or hostname.endswith(f".{domain.rstrip('.').lower()}")
        for domain in domains
    )


class _AnonymizedAttribution:
    """Resolve privacy-safe attribution properties for one telemetry middleware."""

    def __init__(
        self,
        *,
        known_public_mcp_domains: Sequence[str] = (),
        anonymization_salt_fallback: Callable[[], str | None] | None = None,
        caller_ip_fallback: bool = False,
    ) -> None:
        self._known_public_mcp_domains = tuple(known_public_mcp_domains)
        self._anonymization_salt_fallback = anonymization_salt_fallback
        self._caller_ip_fallback = caller_ip_fallback
        self._salt_resolved = False
        self._salt: str | None = None

    def _get_salt(self) -> str | None:
        if not self._salt_resolved:
            self._salt = _safe_value(self._resolve_salt)
            self._salt_resolved = True
        return self._salt

    def _resolve_salt(self) -> str | None:
        salt = os.environ.get(_TELEMETRY_ANONYMIZATION_SALT_ENV)
        if salt is None and self._anonymization_salt_fallback is not None:
            salt = self._anonymization_salt_fallback()
        return salt

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
            caller_value, caller_id_type = caller_identity
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
            client_name, client_version = client_data
            if client_name:
                properties["mcp_client_name"] = client_name
            if client_version:
                properties["mcp_client_version"] = client_version

        return properties
