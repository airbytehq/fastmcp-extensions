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
def test_run_http_server_uses_fastmcp_uvicorn_settings(
    use_wrapper: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastMCP("test")
    captured: dict[str, Any] = {}
    wrapped_app = object()
    wrapper_calls: list[Any] = []

    def wrapper(app: Any) -> Any:
        wrapper_calls.append(app)
        return wrapped_app

    def capture_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(http_server.uvicorn, "run", capture_run)

    http_server.run_http_server(
        app,
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
        wrapper=wrapper if use_wrapper else None,
        host="127.0.0.1",
        port=9000,
    )

    if use_wrapper:
        assert captured["app"] is wrapped_app
        assert len(wrapper_calls) == 1
    else:
        assert captured["app"] is not wrapped_app
        assert not wrapper_calls
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9000
    assert captured["timeout_graceful_shutdown"] == 2
    assert captured["lifespan"] == "on"
    assert captured["ws"] == "websockets-sansio"
    assert captured["log_level"] == http_server.fastmcp.settings.log_level.lower()


@pytest.mark.unit
def test_run_http_server_allows_uvicorn_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture_run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(http_server.uvicorn, "run", capture_run)

    http_server.run_http_server(
        FastMCP("test"),
        uvicorn_config={
            "timeout_graceful_shutdown": 5,
            "lifespan": "auto",
            "ws": "websockets",
            "log_level": "debug",
        },
    )

    assert captured["timeout_graceful_shutdown"] == 5
    assert captured["lifespan"] == "auto"
    assert captured["ws"] == "websockets"
    assert captured["log_level"] == "debug"
