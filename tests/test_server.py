# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the mcp_server() helper function."""

import os
from unittest.mock import patch

import pytest
from fastmcp import FastMCP
from mcp.types import Tool

from fastmcp_extensions import (
    MCPServerConfig,
    MCPServerConfigArg,
    get_mcp_config,
    mcp_server,
)


@pytest.mark.unit
def test_mcp_server_returns_fastmcp_instance() -> None:
    """Test that mcp_server() returns a FastMCP instance."""
    app = mcp_server("test-server")
    assert isinstance(app, FastMCP)


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
def test_mcp_server_config_stores_advertised_properties() -> None:
    """Test that the config stores advertised properties."""
    props = {
        "package_name": "my-package",
        "docs_url": "https://example.com/docs",
    }
    app = mcp_server("test-server", advertised_properties=props)
    config: MCPServerConfig = app.x_mcp_server_config
    assert config.advertised_properties == props


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
    assert arg.env_var == env_var
    assert arg.required == required
    assert arg.sensitive == sensitive


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
        "fastmcp_extensions.server.get_http_headers",
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
        "fastmcp_extensions.server.get_http_headers",
        return_value={"x-api-key": "lowercase-header-key"},
    ):
        value = get_mcp_config(app, "api_key")
        assert value == "lowercase-header-key"


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
            env_var="TEST_NONEXISTENT_VAR_12345",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    mock_headers = patch(
        "fastmcp_extensions.server.get_http_headers", return_value=None
    )
    with mock_headers, pytest.raises(ValueError, match="Required config"):
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

    with patch("fastmcp_extensions.server.get_http_headers", return_value=None):
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

    with patch("fastmcp_extensions.server.get_http_headers", return_value=None):
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

    with patch("fastmcp_extensions.server.get_http_headers", return_value=None):
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
        "fastmcp_extensions.server.get_http_headers",
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

    # Should have no middleware
    assert len(app.middleware) == 0


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
def test_parse_csv_config_empty_string() -> None:
    """Test _parse_csv_config with empty string."""
    from fastmcp_extensions.server import _parse_csv_config

    result = _parse_csv_config("")
    assert result == []


@pytest.mark.unit
def test_parse_csv_config_single_value() -> None:
    """Test _parse_csv_config with single value."""
    from fastmcp_extensions.server import _parse_csv_config

    result = _parse_csv_config("module1")
    assert result == ["module1"]


@pytest.mark.unit
def test_parse_csv_config_multiple_values() -> None:
    """Test _parse_csv_config with multiple values."""
    from fastmcp_extensions.server import _parse_csv_config

    result = _parse_csv_config("module1,module2,module3")
    assert result == ["module1", "module2", "module3"]


@pytest.mark.unit
def test_parse_csv_config_trims_whitespace() -> None:
    """Test _parse_csv_config trims whitespace from values."""
    from fastmcp_extensions.server import _parse_csv_config

    result = _parse_csv_config("  module1  ,  module2  ,  module3  ")
    assert result == ["module1", "module2", "module3"]


@pytest.mark.unit
def test_parse_csv_config_filters_empty_values() -> None:
    """Test _parse_csv_config filters out empty values."""
    from fastmcp_extensions.server import _parse_csv_config

    result = _parse_csv_config("module1,,module2,  ,module3")
    assert result == ["module1", "module2", "module3"]
