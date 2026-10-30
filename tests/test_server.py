# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the mcp_server() helper function."""

import importlib.metadata
import json
import os
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from mcp.types import Tool, ToolAnnotations
from starlette.requests import Request

from fastmcp_extensions import (
    MCPServerConfig,
    MCPServerConfigArg,
    TelemetryConfig,
    ToolCallTelemetryMiddleware,
    get_mcp_config,
    mcp_server,
    near_miss_url_param_error,
    validate_url_query_params,
)
from fastmcp_extensions._middleware import ToolFilterMiddleware
from fastmcp_extensions._telemetry import resolve_version
from fastmcp_extensions.server import _derive_package_name, _distribution_name
from fastmcp_extensions.tool_filters import (
    ENV_READONLY_MODE,
    HEADER_READONLY_MODE,
    READONLY_MODE_CONFIG_ARG,
    _parse_csv_config,
    no_client_filesystem_filter,
)
from fastmcp_extensions.tool_traits import (
    Capability,
    ToolTraits,
    set_tool_traits,
)


def _request_with_query(query_string: bytes) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": query_string,
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


@pytest.mark.unit
def test_mcp_server_returns_fastmcp_instance() -> None:
    """Test that mcp_server() returns a FastMCP instance."""
    app = mcp_server("test-server")
    assert isinstance(app, FastMCP)


@pytest.mark.parametrize(
    "kwargs,expected_count",
    [
        pytest.param({}, 1, id="default"),
        pytest.param({"telemetry": False}, 0, id="disabled_bool"),
        pytest.param(
            {"telemetry": TelemetryConfig(enabled=False)},
            0,
            id="disabled_config",
        ),
    ],
)
@pytest.mark.unit
def test_mcp_server_telemetry_registration(
    kwargs: dict[str, Any], expected_count: int
) -> None:
    app = mcp_server("test-server", **kwargs)

    assert (
        sum(
            isinstance(middleware, ToolCallTelemetryMiddleware)
            for middleware in app.middleware
        )
        == expected_count
    )


@pytest.mark.unit
def test_mcp_server_telemetry_inherits_package_and_precedes_tool_filters() -> None:
    def allow_all_filter(tool: Tool, app: FastMCP) -> bool:
        return True

    app = mcp_server(
        "test-server",
        package_name="fastmcp-extensions",
        tool_filters=[allow_all_filter],
    )
    telemetry = next(
        middleware
        for middleware in app.middleware
        if isinstance(middleware, ToolCallTelemetryMiddleware)
    )
    telemetry_index = next(
        index
        for index, middleware in enumerate(app.middleware)
        if isinstance(middleware, ToolCallTelemetryMiddleware)
    )
    filter_index = next(
        index
        for index, middleware in enumerate(app.middleware)
        if isinstance(middleware, ToolFilterMiddleware)
    )

    assert telemetry._package_version == resolve_version("fastmcp-extensions")
    assert telemetry_index < filter_index


@pytest.mark.unit
def test_mcp_server_passes_attribution_configuration() -> None:
    config = TelemetryConfig(
        known_public_mcp_domains=("airbyte.ai",),
        anonymization_salt=lambda: "fallback-salt",
        anonymized_attribution=True,
    )
    app = mcp_server("test-server", telemetry=config)
    telemetry = next(
        middleware
        for middleware in app.middleware
        if isinstance(middleware, ToolCallTelemetryMiddleware)
    )

    assert telemetry._attribution is not None
    assert telemetry._attribution._known_public_mcp_domains == ("airbyte.ai",)
    assert telemetry._attribution._anonymization_salt is config.anonymization_salt


@pytest.mark.unit
def test_mcp_server_has_config_attached() -> None:
    """Test that mcp_server() attaches config to the app."""
    app = mcp_server("test-server")
    assert hasattr(app, "x_mcp_server_config")
    assert isinstance(app.x_mcp_server_config, MCPServerConfig)


@pytest.mark.unit
def test_mcp_server_config_stores_name() -> None:
    """Test that the config stores the server name."""
    app = mcp_server("my-test-server")
    config: MCPServerConfig = app.x_mcp_server_config
    assert config.name == "my-test-server"


@pytest.mark.unit
def test_mcp_server_name_is_deprecated_alias_for_display_name() -> None:
    """Test that `name=` still works but warns, and both or neither raise."""
    with pytest.warns(DeprecationWarning, match="display_name"):
        app = mcp_server(name="legacy-server")
    assert app.x_mcp_server_config.name == "legacy-server"

    with pytest.raises(TypeError, match="both"):
        mcp_server("new-server", name="legacy-server")
    with pytest.raises(TypeError, match="missing"):
        mcp_server()


@pytest.mark.unit
def test_derive_package_name_lookup_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test one distribution wins, then a direct version match, else None."""
    distributions = {"pkg": ["my-dist"]}
    versions: dict[str, str] = {}

    def fake_version(name: str) -> str:
        if name not in versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr(
        importlib.metadata, "packages_distributions", lambda: distributions
    )
    monkeypatch.setattr(importlib.metadata, "version", fake_version)
    lookup = _distribution_name.__wrapped__  # Bypass the cache
    assert lookup("pkg") == "my-dist"

    distributions["pkg"] = ["dist-a", "dist-b"]
    assert lookup("pkg") is None

    del distributions["pkg"]
    versions["pkg"] = "1.2.3"
    assert lookup("pkg") == "pkg"

    # The caller's top-level import package is what gets looked up
    with patch("fastmcp_extensions.server._distribution_name", side_effect=str.upper):
        assert _derive_package_name() == __name__.split(".")[0].upper()


@pytest.mark.unit
def test_mcp_server_explicit_package_name_is_unchanged() -> None:
    """Test that an explicit `package_name` skips derivation entirely."""
    with patch("fastmcp_extensions.server._derive_package_name") as derive:
        app = mcp_server("test-server", package_name="not-an-installed-dist")
    assert app.x_mcp_server_config.package_name == "not-an-installed-dist"
    derive.assert_not_called()


@pytest.mark.unit
def test_mcp_server_config_stores_advertised_properties() -> None:
    """Test that the config stores advertised properties."""
    props = {
        "package_name": "my-package",
        "docs_url": "https://example.com/docs",
    }
    app = mcp_server("test-server", advertised_properties=props)
    config: MCPServerConfig = app.x_mcp_server_config
    assert config.advertised_properties == props


@pytest.mark.asyncio
@pytest.mark.unit
async def test_server_info_includes_dynamic_properties() -> None:
    app = mcp_server(
        "test-server",
        server_info_provider=lambda: {"authenticated_principal": "user@example.com"},
    )

    result = await app.read_resource("test-server://server/info")

    assert json.loads(result.contents[0].content)["authenticated_principal"] == (
        "user@example.com"
    )


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("provider_result", [None, ["invalid"]])
async def test_server_info_provider_requires_dict(provider_result: object) -> None:
    app = mcp_server(
        "test-server",
        server_info_provider=lambda: provider_result,  # ty: ignore[invalid-argument-type]  # The fixture intentionally returns a narrower test value.
    )

    with pytest.raises(
        ResourceError,
        match=r"server_info_provider must return a dict, got (NoneType|list)",
    ):
        await app.read_resource("test-server://server/info")


@pytest.mark.unit
def test_mcp_server_config_stores_config_args() -> None:
    """Test that the config stores server config args."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            env_var="MY_API_KEY",
            required=True,
            sensitive=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)
    config: MCPServerConfig = app.x_mcp_server_config
    assert len(config.config_args) == 1
    assert config.config_args[0].name == "api_key"


@pytest.mark.parametrize(
    "name,http_header_key,env_var,required,sensitive",
    [
        pytest.param(
            "api_key", "X-API-Key", "API_KEY", True, True, id="required_sensitive"
        ),
        pytest.param(
            "workspace",
            "X-Workspace",
            "WORKSPACE_ID",
            False,
            False,
            id="optional_not_sensitive",
        ),
        pytest.param(
            "token",
            "Authorization",
            "AUTH_TOKEN",
            True,
            False,
            id="required_not_sensitive",
        ),
    ],
)
@pytest.mark.unit
def test_mcp_server_config_arg_attributes(
    name: str, http_header_key: str, env_var: str, required: bool, sensitive: bool
) -> None:
    """Test MCPServerConfigArg stores all attributes correctly."""
    arg = MCPServerConfigArg(
        name=name,
        http_header_key=http_header_key,
        env_var=env_var,
        required=required,
        sensitive=sensitive,
    )
    assert arg.name == name
    assert arg.http_header_key == http_header_key
    assert arg.url_param_key is None
    assert arg.env_var == env_var
    assert arg.required == required
    assert arg.sensitive == sensitive


@pytest.mark.unit
def test_mcp_server_config_arg_preserves_positional_env_var() -> None:
    arg = MCPServerConfigArg("n", "X-H", "ENV_N")

    assert arg.env_var == "ENV_N"
    assert arg.url_param_key is None


@pytest.mark.unit
def test_mcp_server_config_arg_requires_url_param_key_for_disallowed_keys() -> None:
    with pytest.raises(
        ValueError,
        match="disallowed URL query params require url_param_key",
    ):
        MCPServerConfigArg(
            name="mode",
            disallowed_url_param_keys={"readonly": "unsupported"},
        )


@pytest.mark.unit
def test_validate_url_query_params_rejects_readonly_near_misses() -> None:
    assert near_miss_url_param_error("readonly", "read-only") == (
        "URL query param 'readonly' is not supported by the standard. "
        "Did you mean 'read-only'?"
    )
    assert validate_url_query_params(
        [("readonly", "true")], [READONLY_MODE_CONFIG_ARG]
    ) == (
        "URL query param 'readonly' is not supported by the standard. "
        "Did you mean 'read-only'?"
    )
    assert validate_url_query_params(
        [("READ_ONLY", "1")], [READONLY_MODE_CONFIG_ARG]
    ) == (
        "URL query param 'read_only' is not supported by the standard. "
        "Did you mean 'read-only'?"
    )


@pytest.mark.unit
def test_validate_url_query_params_rejects_invalid_readonly_values() -> None:
    assert validate_url_query_params(
        [("read-only", "yes")], [READONLY_MODE_CONFIG_ARG]
    ) == (
        "Invalid value for URL query param 'read-only'. Use one of: true, false, 1, 0."
    )


@pytest.mark.parametrize("value", ["TRUE", "0", ""])
@pytest.mark.unit
def test_validate_url_query_params_accepts_allowed_readonly_values(
    value: str,
) -> None:
    assert (
        validate_url_query_params([("read-only", value)], [READONLY_MODE_CONFIG_ARG])
        is None
    )


@pytest.mark.unit
def test_validate_url_query_params_ignores_unrelated_params() -> None:
    assert (
        validate_url_query_params(
            [("utm_source", "x"), ("foo", "bar")],
            [READONLY_MODE_CONFIG_ARG],
        )
        is None
    )


@pytest.mark.unit
def test_validate_url_query_params_uses_custom_disallowed_key_message() -> None:
    custom_arg = MCPServerConfigArg(
        name="custom",
        url_param_key="custom-mode",
        disallowed_url_param_keys={"mode": "Custom unsupported query parameter."},
    )

    assert validate_url_query_params([("MODE", "anything")], [custom_arg]) == (
        "Custom unsupported query parameter."
    )


@pytest.mark.unit
def test_get_mcp_config_from_env_var() -> None:
    """Test resolving config from environment variable."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch.dict(os.environ, {"TEST_API_KEY": "secret-key-123"}):
        value = get_mcp_config(app, "api_key")
        assert value == "secret-key-123"


@pytest.mark.unit
def test_get_mcp_config_from_http_header() -> None:
    """Test resolving config from HTTP header (takes precedence over env var)."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch.dict(os.environ, {"TEST_API_KEY": "env-key"}), patch(
        "fastmcp_extensions.server_config.get_http_headers",
        return_value={"X-API-Key": "header-key"},
    ):
        value = get_mcp_config(app, "api_key")
        assert value == "header-key"


@pytest.mark.unit
def test_get_mcp_config_header_case_insensitive() -> None:
    """Test that HTTP header resolution is case-insensitive."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch(
        "fastmcp_extensions.server_config.get_http_headers",
        return_value={"x-api-key": "lowercase-header-key"},
    ):
        value = get_mcp_config(app, "api_key")
        assert value == "lowercase-header-key"


@pytest.mark.unit
def test_get_mcp_config_from_url_param() -> None:
    """Test resolving config from a URL query parameter."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(name="mode", url_param_key="mode", required=True)
        ],
    )

    with patch(
        "fastmcp_extensions.server_config.get_http_request",
        return_value=_request_with_query(b"mode=query-value"),
    ):
        assert get_mcp_config(app, "mode") == "query-value"


@pytest.mark.unit
def test_get_mcp_config_http_header_takes_precedence_over_url_param() -> None:
    """Test that an HTTP header takes precedence over a URL query parameter."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(
                name="mode",
                http_header_key="X-Mode",
                url_param_key="mode",
                required=True,
            )
        ],
    )

    with patch(
        "fastmcp_extensions.server_config.get_http_headers",
        return_value={"X-Mode": "header-value"},
    ), patch(
        "fastmcp_extensions.server_config.get_http_request",
        return_value=_request_with_query(b"mode=query-value"),
    ):
        assert get_mcp_config(app, "mode") == "header-value"


@pytest.mark.unit
def test_get_mcp_config_url_param_takes_precedence_over_env_var() -> None:
    """Test that a URL query parameter takes precedence over an env var."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(
                name="mode",
                url_param_key="mode",
                env_var="TEST_MODE",
                required=True,
            )
        ],
    )

    with patch.dict(os.environ, {"TEST_MODE": "env-value"}), patch(
        "fastmcp_extensions.server_config.get_http_request",
        return_value=_request_with_query(b"mode=query-value"),
    ):
        assert get_mcp_config(app, "mode") == "query-value"


@pytest.mark.unit
def test_get_mcp_config_url_param_case_insensitive() -> None:
    """Test that URL query parameter matching is case-insensitive."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(name="mode", url_param_key="read-only", required=True)
        ],
    )

    with patch(
        "fastmcp_extensions.server_config.get_http_request",
        return_value=_request_with_query(b"Read-Only=true"),
    ):
        assert get_mcp_config(app, "mode") == "true"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        pytest.param("env-value", "env-value", id="empty-param-falls-through-to-env"),
        pytest.param("", "default-value", id="empty-param-falls-through-to-default"),
    ],
)
@pytest.mark.unit
def test_get_mcp_config_empty_url_param_falls_through(
    env_value: str, expected: str
) -> None:
    """Test that an empty URL query parameter is treated as unset."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(
                name="mode",
                url_param_key="mode",
                env_var="TEST_MODE",
                default="default-value",
                required=False,
            )
        ],
    )

    with patch.dict(os.environ, {"TEST_MODE": env_value}), patch(
        "fastmcp_extensions.server_config.get_http_request",
        return_value=_request_with_query(b"mode="),
    ):
        assert get_mcp_config(app, "mode") == expected


@pytest.mark.unit
def test_get_mcp_config_normalizes_url_param() -> None:
    """Test that normalize_fn is applied to a URL query parameter."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(
                name="mode",
                url_param_key="mode",
                normalize_fn=str.upper,
                required=True,
            )
        ],
    )

    with patch(
        "fastmcp_extensions.server_config.get_http_request",
        return_value=_request_with_query(b"mode=query-value"),
    ):
        assert get_mcp_config(app, "mode") == "QUERY-VALUE"


@pytest.mark.unit
def test_get_mcp_config_without_http_request_falls_through_to_default() -> None:
    """Test that a missing HTTP request falls through to the default."""
    app = mcp_server(
        "test-server",
        server_config_args=[
            MCPServerConfigArg(
                name="mode",
                url_param_key="mode",
                default="default-value",
                required=False,
            )
        ],
    )

    with patch(
        "fastmcp_extensions.server_config.get_http_request",
        side_effect=RuntimeError("No active HTTP request found."),
    ):
        assert get_mcp_config(app, "mode") == "default-value"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_readonly_mode_is_server_enforced_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP request values cannot disable read-only mode set by the server."""
    monkeypatch.delenv(ENV_READONLY_MODE, raising=False)
    app = mcp_server("test-server", include_standard_tool_filters=True)

    @app.tool(annotations={"readOnlyHint": True})
    def read_only_tool() -> str:
        return "read"

    @app.tool(annotations={"readOnlyHint": False})
    def writable_tool() -> str:
        return "write"

    http_app = app.http_app(stateless_http=True)
    transport = httpx.ASGITransport(app=http_app)
    headers = {
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    request_body = {
        "jsonrpc": "2.0",
        "id": "tools-list",
        "method": "tools/list",
        "params": {},
    }
    async with http_app.router.lifespan_context(http_app), httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        readonly_response = await client.post(
            "/mcp?read-only=true", headers=headers, json=request_body
        )
        all_tools_response = await client.post(
            "/mcp", headers=headers, json=request_body
        )
        monkeypatch.setenv(ENV_READONLY_MODE, "1")
        query_disable_response = await client.post(
            "/mcp?read-only=0", headers=headers, json=request_body
        )
        header_disable_response = await client.post(
            "/mcp",
            headers={**headers, HEADER_READONLY_MODE: "0"},
            json=request_body,
        )

    assert readonly_response.status_code == 200
    assert all_tools_response.status_code == 200
    assert query_disable_response.status_code == 200
    assert header_disable_response.status_code == 200

    def tool_names(response: httpx.Response) -> set[str]:
        result = json.loads(
            response.text.split("data: ", maxsplit=1)[1].splitlines()[0]
        )
        return {tool["name"] for tool in result["result"]["tools"]}

    assert tool_names(readonly_response) == {"read_only_tool"}
    assert tool_names(all_tools_response) == {"read_only_tool", "writable_tool"}
    assert tool_names(query_disable_response) == {"read_only_tool"}
    assert tool_names(header_disable_response) == {"read_only_tool"}


@pytest.mark.unit
def test_mcp_server_config_arg_rejects_sensitive_url_param() -> None:
    """Sensitive config args must not be read from URL query parameters."""
    with pytest.raises(
        ValueError,
        match=(
            "sensitive config args can't be read from URL query params "
            "because URLs are commonly logged"
        ),
    ):
        MCPServerConfigArg(name="secret", sensitive=True, url_param_key="x")


@pytest.mark.unit
def test_get_mcp_config_unknown_name_raises_key_error() -> None:
    """Test that resolving unknown config name raises KeyError."""
    app = mcp_server("test-server")

    with pytest.raises(KeyError, match="Unknown config argument"):
        get_mcp_config(app, "nonexistent")


@pytest.mark.unit
def test_get_mcp_config_required_missing_raises_value_error() -> None:
    """Test that missing required config raises ValueError."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            url_param_key="api_key",
            env_var="TEST_NONEXISTENT_VAR_12345",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    mock_headers = patch(
        "fastmcp_extensions.server_config.get_http_headers", return_value=None
    )
    mock_request = patch(
        "fastmcp_extensions.server_config.get_http_request",
        side_effect=RuntimeError("No active HTTP request found."),
    )
    with mock_headers, mock_request, pytest.raises(
        ValueError, match=r"Required config.*URL query param 'api_key'"
    ):
        get_mcp_config(app, "api_key")


@pytest.mark.unit
def test_get_mcp_config_optional_missing_returns_empty_string() -> None:
    """Test that missing optional config returns empty string."""
    config_args = [
        MCPServerConfigArg(
            name="optional_key",
            http_header_key="X-Optional",
            env_var="NONEXISTENT_OPTIONAL_VAR",
            required=False,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch("fastmcp_extensions.server_config.get_http_headers", return_value=None):
        value = get_mcp_config(app, "optional_key")
        assert value == ""


@pytest.mark.unit
def test_mcp_server_passes_kwargs_to_fastmcp() -> None:
    """Test that additional kwargs are passed to FastMCP constructor."""
    app = mcp_server("test-server", instructions="Test instructions")
    assert app.instructions == "Test instructions"


@pytest.mark.unit
def test_mcp_server_config_default_values() -> None:
    """Test MCPServerConfigArg default values."""
    arg = MCPServerConfigArg(name="test")
    assert arg.http_header_key is None
    assert arg.env_var is None
    assert arg.default is None
    assert arg.required is True
    assert arg.sensitive is False


@pytest.mark.unit
def test_get_mcp_config_with_string_default() -> None:
    """Test resolving config with a string default value."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            env_var="NONEXISTENT_VAR_12345",
            default="default-value",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch("fastmcp_extensions.server_config.get_http_headers", return_value=None):
        value = get_mcp_config(app, "api_key")
        assert value == "default-value"


@pytest.mark.unit
def test_get_mcp_config_with_callable_default() -> None:
    """Test resolving config with a callable default value."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            env_var="NONEXISTENT_VAR_12345",
            default=lambda: "callable-default",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch("fastmcp_extensions.server_config.get_http_headers", return_value=None):
        value = get_mcp_config(app, "api_key")
        assert value == "callable-default"


@pytest.mark.unit
def test_get_mcp_config_env_var_takes_precedence_over_default() -> None:
    """Test that env var takes precedence over default value."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            env_var="TEST_API_KEY",
            default="default-value",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch.dict(os.environ, {"TEST_API_KEY": "env-value"}):
        value = get_mcp_config(app, "api_key")
        assert value == "env-value"


@pytest.mark.unit
def test_get_mcp_config_with_only_env_var() -> None:
    """Test resolving config with only env_var set (no http_header_key)."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch.dict(os.environ, {"TEST_API_KEY": "env-only-value"}):
        value = get_mcp_config(app, "api_key")
        assert value == "env-only-value"


@pytest.mark.unit
def test_get_mcp_config_with_only_http_header() -> None:
    """Test resolving config with only http_header_key set (no env_var)."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch(
        "fastmcp_extensions.server_config.get_http_headers",
        return_value={"X-API-Key": "header-only-value"},
    ):
        value = get_mcp_config(app, "api_key")
        assert value == "header-only-value"


@pytest.mark.unit
def test_mcp_server_with_tool_filters() -> None:
    """Test that mcp_server() registers tool filter middleware."""

    def allow_all_filter(tool: Tool, app: FastMCP) -> bool:
        return True

    app = mcp_server("test-server", tool_filters=[allow_all_filter])

    # Verify middleware was registered
    assert len(app.middleware) >= 1


@pytest.mark.unit
def test_mcp_server_with_multiple_tool_filters() -> None:
    """Test that mcp_server() registers multiple tool filter middleware."""

    def filter_one(tool: Tool, app: FastMCP) -> bool:
        return True

    def filter_two(tool: Tool, app: FastMCP) -> bool:
        return True

    app = mcp_server("test-server", tool_filters=[filter_one, filter_two])

    # Verify both middleware were registered
    assert len(app.middleware) >= 2


@pytest.mark.unit
def test_mcp_server_without_tool_filters() -> None:
    """Test that mcp_server() works without tool_filters parameter."""
    app = mcp_server("test-server")

    # Should work without any tool filters
    assert isinstance(app, FastMCP)


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_adds_config_args() -> None:
    """Test that include_standard_tool_filters=True adds standard config args."""
    app = mcp_server("test-server", include_standard_tool_filters=True)

    config = app.x_mcp_server_config
    config_names = [arg.name for arg in config.config_args]

    # Should include both standard config args
    assert "readonly_mode" in config_names
    assert "no_destructive_tools" in config_names


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_adds_middleware() -> None:
    """Test that include_standard_tool_filters=True adds filter middleware."""
    app = mcp_server("test-server", include_standard_tool_filters=True)

    # Should have at least 2 middleware (one for each standard filter)
    assert len(app.middleware) >= 2


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_false_skips_standard_filters() -> (
    None
):
    """Test that include_standard_tool_filters=False skips standard filters."""
    app = mcp_server("test-server", include_standard_tool_filters=False)

    config = app.x_mcp_server_config
    config_names = [arg.name for arg in config.config_args]

    # Should not include standard config args
    assert "readonly_mode" not in config_names
    assert "no_destructive_tools" not in config_names

    # Should have no ToolFilterMiddleware (FastMCP v3 may add built-in middleware)
    from fastmcp_extensions._middleware import ToolFilterMiddleware

    assert not any(isinstance(m, ToolFilterMiddleware) for m in app.middleware)


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_with_custom_config_args() -> None:
    """Test that include_standard_tool_filters=True works with custom config args."""
    custom_arg = MCPServerConfigArg(
        name="custom_config",
        http_header_key="X-Custom-Config",
        env_var="CUSTOM_CONFIG",
        default="default_value",
    )
    app = mcp_server(
        "test-server",
        server_config_args=[custom_arg],
        include_standard_tool_filters=True,
    )

    config = app.x_mcp_server_config
    config_names = [arg.name for arg in config.config_args]

    # Should include custom config arg
    assert "custom_config" in config_names
    # Should also include standard config args
    assert "readonly_mode" in config_names
    assert "no_destructive_tools" in config_names


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_with_custom_tool_filters() -> None:
    """Test that include_standard_tool_filters=True works with custom tool filters."""

    def custom_filter(tool: Tool, app: FastMCP) -> bool:
        return True

    app = mcp_server(
        "test-server",
        tool_filters=[custom_filter],
        include_standard_tool_filters=True,
    )

    # Should have at least 3 middleware (1 custom + 2 standard)
    assert len(app.middleware) >= 3


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_includes_module_config_args() -> None:
    """Test that include_standard_tool_filters=True adds module filtering config args."""
    app = mcp_server("test-server", include_standard_tool_filters=True)

    config = app.x_mcp_server_config
    config_names = [arg.name for arg in config.config_args]

    # Should include module filtering config args
    assert "exclude_modules" in config_names
    assert "include_modules" in config_names
    assert "exclude_tools" in config_names


@pytest.mark.unit
def test_mcp_server_include_standard_tool_filters_includes_no_client_filesystem() -> (
    None
):
    """Test that `include_standard_tool_filters=True` adds `no_client_filesystem` config arg."""
    app = mcp_server("test-server", include_standard_tool_filters=True)

    config = app.x_mcp_server_config
    config_names = [arg.name for arg in config.config_args]

    assert "no_client_filesystem" in config_names


@pytest.mark.parametrize(
    "config_value,has_annotation,expected_visible",
    [
        pytest.param("", True, True, id="no_config_set-annotated-visible"),
        pytest.param("", False, True, id="no_config_set-unannotated-visible"),
        pytest.param("1", True, False, id="config_1-annotated-hidden"),
        pytest.param("true", True, False, id="config_true-annotated-hidden"),
        pytest.param("TRUE", True, False, id="config_TRUE-annotated-hidden"),
        pytest.param("1", False, True, id="config_1-unannotated-visible"),
        pytest.param("0", True, True, id="config_0-annotated-visible"),
        pytest.param("false", True, True, id="config_false-annotated-visible"),
    ],
)
@pytest.mark.unit
def test_no_client_filesystem_filter(
    config_value: str,
    has_annotation: bool,
    expected_visible: bool,
) -> None:
    """Test `no_client_filesystem_filter` hides annotated tools when config is enabled."""
    app = mcp_server("test-server", include_standard_tool_filters=True)

    if has_annotation:
        set_tool_traits(
            app,
            "local_tool",
            ToolTraits(required_capabilities=frozenset({Capability.CLIENT_FILESYSTEM})),
        )

    tool = Tool(
        name="local_tool",
        description="A tool requiring client filesystem",
        inputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(),
    )

    env_patch = {"MCP_NO_CLIENT_FILESYSTEM": config_value} if config_value else {}
    with patch.dict(os.environ, env_patch, clear=False), patch(
        "fastmcp_extensions.server_config.get_http_headers",
        return_value=None,
    ):
        result = no_client_filesystem_filter(tool, app)

    assert result is expected_visible


@pytest.mark.unit
def test_parse_csv_config_empty_string() -> None:
    """Test `_parse_csv_config` with empty string."""
    result = _parse_csv_config("")
    assert result == []


@pytest.mark.unit
def test_parse_csv_config_single_value() -> None:
    """Test `_parse_csv_config` with single value."""
    result = _parse_csv_config("module1")
    assert result == ["module1"]


@pytest.mark.unit
def test_parse_csv_config_multiple_values() -> None:
    """Test `_parse_csv_config` with multiple values."""
    result = _parse_csv_config("module1,module2,module3")
    assert result == ["module1", "module2", "module3"]


@pytest.mark.unit
def test_parse_csv_config_trims_whitespace() -> None:
    """Test `_parse_csv_config` trims whitespace from values."""
    result = _parse_csv_config("  module1  ,  module2  ,  module3  ")
    assert result == ["module1", "module2", "module3"]


@pytest.mark.unit
def test_parse_csv_config_filters_empty_values() -> None:
    """Test `_parse_csv_config` filters out empty values."""
    result = _parse_csv_config("module1,,module2,  ,module3")
    assert result == ["module1", "module2", "module3"]
