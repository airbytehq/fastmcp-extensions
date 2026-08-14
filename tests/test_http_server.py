# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the FastMCP HTTP serving helper."""

from typing import Any

import pytest
from fastmcp import FastMCP

import fastmcp_extensions.http_server as http_server
from fastmcp_extensions import (
    CapabilityTokenMiddleware,
    RejectEventStreamGetMiddleware,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "use_wrapper",
    [
        pytest.param(False, id="without-wrapper"),
        pytest.param(True, id="with-wrapper"),
    ],
)
@pytest.mark.parametrize(
    "uvicorn_config,expected_config",
    [
        pytest.param(
            None,
            {
                "timeout_graceful_shutdown": 2,
                "lifespan": "on",
                "ws": "websockets-sansio",
                "log_level": http_server.fastmcp.settings.log_level.lower(),
            },
            id="fastmcp-defaults",
        ),
        pytest.param(
            {
                "timeout_graceful_shutdown": 5,
                "lifespan": "auto",
                "ws": "websockets",
                "log_level": "debug",
            },
            {
                "timeout_graceful_shutdown": 5,
                "lifespan": "auto",
                "ws": "websockets",
                "log_level": "debug",
            },
            id="caller-overrides",
        ),
    ],
)
@pytest.mark.parametrize(
    "port",
    [
        pytest.param(9000, id="configured-port"),
        pytest.param(0, id="ephemeral-port"),
    ],
)
def test_run_mcp_http_server_builds_and_serves_with_expected_config(
    use_wrapper: bool,
    uvicorn_config: dict[str, Any] | None,
    expected_config: dict[str, Any],
    port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastMCP("test")
    captured: dict[str, Any] = {}
    built_app = object()
    wrapped_app = object()
    wrapper_calls: list[Any] = []

    def build_http_app(**kwargs: Any) -> object:
        captured["http_app_kwargs"] = kwargs
        return built_app

    monkeypatch.setattr(app, "http_app", build_http_app)

    def wrapper(app: Any) -> object:
        wrapper_calls.append(app)
        return wrapped_app

    def capture_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(http_server.uvicorn, "run", capture_run)

    http_server.run_mcp_http_server(
        app,
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
        wrapper=wrapper if use_wrapper else None,
        enable_stateless_capability_middleware=False,
        host="127.0.0.1",
        port=port,
        uvicorn_config=uvicorn_config,
    )

    assert (captured["app"] is built_app) == (not use_wrapper)
    assert (captured["app"] is wrapped_app) == use_wrapper
    assert wrapper_calls == [built_app] * int(use_wrapper)
    assert captured["http_app_kwargs"] == {
        "path": "/mcp",
        "transport": "streamable-http",
        "stateless_http": True,
    }
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == port
    assert {key: captured[key] for key in expected_config} == expected_config


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stateless_http", "configured_stateless", "enable_middleware", "wrapped"),
    [
        pytest.param(True, False, True, True, id="explicit-stateless"),
        pytest.param(None, True, True, True, id="settings-stateless"),
        pytest.param(False, True, True, False, id="explicit-stateful"),
        pytest.param(True, True, False, False, id="explicit-opt-out"),
    ],
)
def test_run_mcp_http_server_default_capability_middleware(
    stateless_http: bool | None,
    configured_stateless: bool,
    enable_middleware: bool,
    wrapped: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply capability middleware only to enabled stateless HTTP servers."""
    server = FastMCP("test")
    built_app = object()
    captured: dict[str, Any] = {}

    def build_http_app(**kwargs: Any) -> object:
        captured["http_app_kwargs"] = kwargs
        return built_app

    monkeypatch.setattr(server, "http_app", build_http_app)
    monkeypatch.setattr(
        http_server.fastmcp.settings, "stateless_http", configured_stateless
    )
    monkeypatch.setattr(
        http_server.uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )

    http_server.run_mcp_http_server(
        server,
        transport="streamable-http",
        stateless_http=stateless_http,
        enable_stateless_capability_middleware=enable_middleware,
    )

    if wrapped:
        assert isinstance(captured["app"], RejectEventStreamGetMiddleware)
        capability_app = captured["app"].app
        assert isinstance(capability_app, CapabilityTokenMiddleware)
        assert capability_app.app is built_app
        assert captured["app"].path == "/mcp"
    else:
        assert captured["app"] is built_app


@pytest.mark.unit
def test_run_mcp_http_server_scopes_sse_rejection_to_resolved_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve the default FastMCP path before scoping SSE rejection."""
    server = FastMCP("test")
    built_app = object()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(server, "http_app", lambda **_: built_app)
    monkeypatch.setattr(http_server.fastmcp.settings, "streamable_http_path", "/custom")
    monkeypatch.setattr(
        http_server.uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )

    http_server.run_mcp_http_server(
        server,
        transport="streamable-http",
        stateless_http=True,
    )

    assert isinstance(captured["app"], RejectEventStreamGetMiddleware)
    assert captured["app"].path == "/custom"
