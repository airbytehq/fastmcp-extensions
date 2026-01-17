# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the mcp_server() helper function."""

import os
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from fastmcp_extensions import (
    MCPServerConfig,
    MCPServerConfigArg,
    mcp_server,
    resolve_config,
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
    assert hasattr(app, "_mcp_server_config")
    assert isinstance(app._mcp_server_config, MCPServerConfig)


@pytest.mark.unit
def test_mcp_server_config_stores_name() -> None:
    """Test that the config stores the server name."""
    app = mcp_server("my-test-server")
    config: MCPServerConfig = app._mcp_server_config
    assert config.name == "my-test-server"


@pytest.mark.unit
def test_mcp_server_config_stores_advertised_properties() -> None:
    """Test that the config stores advertised properties."""
    props = {
        "package_name": "my-package",
        "docs_url": "https://example.com/docs",
    }
    app = mcp_server("test-server", advertised_properties=props)
    config: MCPServerConfig = app._mcp_server_config
    assert config.advertised_properties == props


@pytest.mark.unit
def test_mcp_server_config_stores_config_args() -> None:
    """Test that the config stores server config args."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            header="X-API-Key",
            env_var="MY_API_KEY",
            required=True,
            sensitive=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)
    config: MCPServerConfig = app._mcp_server_config
    assert len(config.config_args) == 1
    assert config.config_args[0].name == "api_key"


@pytest.mark.parametrize(
    "name,header,env_var,required,sensitive",
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
    name: str, header: str, env_var: str, required: bool, sensitive: bool
) -> None:
    """Test MCPServerConfigArg stores all attributes correctly."""
    arg = MCPServerConfigArg(
        name=name,
        header=header,
        env_var=env_var,
        required=required,
        sensitive=sensitive,
    )
    assert arg.name == name
    assert arg.header == header
    assert arg.env_var == env_var
    assert arg.required == required
    assert arg.sensitive == sensitive


@pytest.mark.unit
def test_resolve_config_from_env_var() -> None:
    """Test resolving config from environment variable."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            header="X-API-Key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch.dict(os.environ, {"TEST_API_KEY": "secret-key-123"}):
        value = resolve_config(app, "api_key")
        assert value == "secret-key-123"


@pytest.mark.unit
def test_resolve_config_from_http_header() -> None:
    """Test resolving config from HTTP header (takes precedence over env var)."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            header="X-API-Key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch.dict(os.environ, {"TEST_API_KEY": "env-key"}), patch(
        "fastmcp_extensions.server.get_http_headers",
        return_value={"X-API-Key": "header-key"},
    ):
        value = resolve_config(app, "api_key")
        assert value == "header-key"


@pytest.mark.unit
def test_resolve_config_header_case_insensitive() -> None:
    """Test that HTTP header resolution is case-insensitive."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            header="X-API-Key",
            env_var="TEST_API_KEY",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch(
        "fastmcp_extensions.server.get_http_headers",
        return_value={"x-api-key": "lowercase-header-key"},
    ):
        value = resolve_config(app, "api_key")
        assert value == "lowercase-header-key"


@pytest.mark.unit
def test_resolve_config_unknown_name_raises_key_error() -> None:
    """Test that resolving unknown config name raises KeyError."""
    app = mcp_server("test-server")

    with pytest.raises(KeyError, match="Unknown config argument"):
        resolve_config(app, "nonexistent")


@pytest.mark.unit
def test_resolve_config_required_missing_raises_value_error() -> None:
    """Test that missing required config raises ValueError."""
    config_args = [
        MCPServerConfigArg(
            name="api_key",
            header="X-API-Key",
            env_var="TEST_NONEXISTENT_VAR_12345",
            required=True,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    mock_headers = patch(
        "fastmcp_extensions.server.get_http_headers", return_value=None
    )
    with mock_headers, pytest.raises(ValueError, match="Required config"):
        resolve_config(app, "api_key")


@pytest.mark.unit
def test_resolve_config_optional_missing_returns_empty_string() -> None:
    """Test that missing optional config returns empty string."""
    config_args = [
        MCPServerConfigArg(
            name="optional_key",
            header="X-Optional",
            env_var="NONEXISTENT_OPTIONAL_VAR",
            required=False,
        ),
    ]
    app = mcp_server("test-server", server_config_args=config_args)

    with patch("fastmcp_extensions.server.get_http_headers", return_value=None):
        value = resolve_config(app, "optional_key")
        assert value == ""


@pytest.mark.unit
def test_mcp_server_passes_kwargs_to_fastmcp() -> None:
    """Test that additional kwargs are passed to FastMCP constructor."""
    app = mcp_server("test-server", instructions="Test instructions")
    assert app.instructions == "Test instructions"


@pytest.mark.unit
def test_mcp_server_config_default_values() -> None:
    """Test MCPServerConfigArg default values."""
    arg = MCPServerConfigArg(
        name="test",
        header="X-Test",
        env_var="TEST_VAR",
    )
    assert arg.required is True
    assert arg.sensitive is False
