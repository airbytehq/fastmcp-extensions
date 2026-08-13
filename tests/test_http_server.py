# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the FastMCP HTTP serving helper."""

from typing import Any

import pytest
from fastmcp import FastMCP

import fastmcp_extensions.http_server as http_server


@pytest.mark.unit
@pytest.mark.parametrize(
    "wrapper_setup",
    [
        pytest.param(
            lambda wrapper_calls, wrapped_app: (
                None,
                lambda captured, calls: captured["app"],
                0,
            ),
            id="without-wrapper",
        ),
        pytest.param(
            lambda wrapper_calls, wrapped_app: (
                (
                    lambda app: (
                        wrapper_calls.append(app),
                        wrapped_app,
                    )[1]
                ),
                lambda captured, calls: wrapped_app,
                1,
            ),
            id="with-wrapper",
        ),
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
def test_run_http_server_builds_and_serves_with_expected_config(
    wrapper_setup: Any,
    uvicorn_config: dict[str, Any] | None,
    expected_config: dict[str, Any],
    port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastMCP("test")
    captured: dict[str, Any] = {}
    wrapped_app = object()
    wrapper_calls: list[Any] = []

    wrapper, expected_app, expected_wrapper_calls = wrapper_setup(
        wrapper_calls,
        wrapped_app,
    )

    def capture_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(http_server.uvicorn, "run", capture_run)

    http_server.run_http_server(
        app,
        path="/mcp",
        transport="streamable-http",
        stateless_http=True,
        wrapper=wrapper,
        host="127.0.0.1",
        port=port,
        uvicorn_config=uvicorn_config,
    )

    assert captured["app"] is expected_app(captured, wrapper_calls)
    assert len(wrapper_calls) == expected_wrapper_calls
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == port
    assert {key: captured[key] for key in expected_config} == expected_config
