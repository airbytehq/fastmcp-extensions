# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the FastMCP HTTP serving helper."""

from typing import Any

import pytest
from fastmcp import FastMCP

import fastmcp_extensions.http_server as http_server


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
