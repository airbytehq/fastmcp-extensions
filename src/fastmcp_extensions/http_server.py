# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Helpers for serving FastMCP applications over HTTP.

`run_http_server` owns the application-building and Uvicorn-serving sequence
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

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

import fastmcp
import uvicorn
from fastmcp import FastMCP

if TYPE_CHECKING:
    from starlette.types import ASGIApp


DEFAULT_UVICORN_CONFIG: Mapping[str, Any] = {
    "timeout_graceful_shutdown": 2,
    "lifespan": "on",
    "ws": "websockets-sansio",
}


def run_http_server(
    server: FastMCP,
    *,
    path: str | None = None,
    transport: Literal["http", "streamable-http", "sse"] = "http",
    stateless_http: bool | None = None,
    wrapper: Callable[[ASGIApp], ASGIApp] | None = None,
    host: str | None = None,
    port: int | None = None,
    uvicorn_config: Mapping[str, Any] | None = None,
) -> None:
    """Build and serve a FastMCP HTTP application.

    `wrapper`, when provided, is applied as the outermost ASGI layer. Values
    in `uvicorn_config` override the parity defaults and the resolved log
    level. Host and port are controlled by their dedicated arguments.
    """
    host = host or fastmcp.settings.host
    port = port or fastmcp.settings.port
    app = server.http_app(
        path=path,
        transport=transport,
        stateless_http=stateless_http,
    )
    if wrapper is not None:
        app = wrapper(app)

    config = dict(DEFAULT_UVICORN_CONFIG)
    config.update(uvicorn_config or {})
    if "log_config" not in config and "log_level" not in config:
        config["log_level"] = fastmcp.settings.log_level.lower()

    uvicorn.run(app, host=host, port=port, **config)
