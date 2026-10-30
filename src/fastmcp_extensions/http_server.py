# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Helpers for serving FastMCP applications over HTTP.

`run_mcp_http_server` owns the application-building and Uvicorn-serving sequence
when an application needs an outer ASGI wrapper. Its Uvicorn defaults mirror
`FastMCP.run_http_async`: lifespan is pinned to `"on"` so a startup failure
stops the process instead of being treated as unsupported by Uvicorn's
`"auto"` mode, `timeout_graceful_shutdown` bounds shutdown time for streaming
connections, and `"websockets-sansio"` matches FastMCP's WebSocket
implementation. The helper intentionally does not enter FastMCP's private
lifespan manager because the application returned by `http_app` owns lifespan
through its `_lifespan_proxy` reference counting.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl

import fastmcp
import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from fastmcp_extensions.capability_tokens import (
    CapabilityTokenMiddleware,
    RejectEventStreamGetMiddleware,
)
from fastmcp_extensions.server_config import (
    MCPServerConfigArg,
    validate_url_query_params,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


DEFAULT_UVICORN_CONFIG: Mapping[str, Any] = {
    "timeout_graceful_shutdown": 2,
    "lifespan": "on",
    "ws": "websockets-sansio",
}


class RejectInvalidUrlParamsMiddleware:
    """Reject invalid or unsupported URL query parameters on HTTP requests."""

    def __init__(
        self,
        app: ASGIApp,
        config_args: Iterable[MCPServerConfigArg],
    ) -> None:
        self.app = app
        self.config_args = tuple(config_args)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        query_string = scope.get("query_string", b"")
        query_params = parse_qsl(
            query_string.decode("latin-1"),
            keep_blank_values=True,
        )
        error = validate_url_query_params(query_params, self.config_args)
        if error is not None:
            response = JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": error,
                },
                status_code=400,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def run_mcp_http_server(
    server: FastMCP,
    *,
    path: str | None = None,
    transport: Literal["http", "streamable-http", "sse"] = "http",
    stateless_http: bool | None = None,
    wrapper: Callable[[ASGIApp], ASGIApp] | None = None,
    enable_stateless_capability_middleware: bool = True,
    host: str | None = None,
    port: int | None = None,
    uvicorn_config: Mapping[str, Any] | None = None,
) -> None:
    """Build and serve a FastMCP HTTP application.

    Stateless HTTP servers get `CapabilityTokenMiddleware` and
    `RejectEventStreamGetMiddleware` by default, so client extension
    declarations survive per-request session recreation without any per-server
    wiring. Set `enable_stateless_capability_middleware` to `False` to opt out.
    `wrapper`,
    when provided, is the innermost of those layers and remains the
    outermost layer everywhere else. Values in `uvicorn_config` override the
    parity defaults and the resolved log level. Host and port are controlled by
    their dedicated arguments.
    """
    host = fastmcp.settings.host if host is None else host
    port = fastmcp.settings.port if port is None else port
    app = server.http_app(
        path=path,
        transport=transport,
        stateless_http=stateless_http,
    )
    if wrapper is not None:
        app = wrapper(app)
    resolved_stateless_http = (
        stateless_http
        if stateless_http is not None
        else fastmcp.settings.stateless_http
    )
    is_http_transport = transport in {"http", "streamable-http"}
    apply_stateless_layers = (
        is_http_transport
        and enable_stateless_capability_middleware
        and resolved_stateless_http
    )
    if apply_stateless_layers:
        app = CapabilityTokenMiddleware(app)
    resolved_path = path if path is not None else fastmcp.settings.streamable_http_path
    if apply_stateless_layers:
        app = RejectEventStreamGetMiddleware(app, path=resolved_path)

    server_config = getattr(server, "x_mcp_server_config", None)
    config_args = getattr(server_config, "config_args", None)
    if config_args:
        app = RejectInvalidUrlParamsMiddleware(app, config_args)

    config = dict(DEFAULT_UVICORN_CONFIG)
    config.update(uvicorn_config or {})
    if "log_config" not in config and "log_level" not in config:
        config["log_level"] = fastmcp.settings.log_level.lower()

    uvicorn.run(app, host=host, port=port, **config)
