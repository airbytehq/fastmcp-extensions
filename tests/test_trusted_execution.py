# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the generic trusted-execution master gate and header cleanup."""

import os
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import Tool, ToolAnnotations

from fastmcp_extensions import (
    MCPServerConfigArg,
    assert_http_trusted_execution_disabled,
    is_trusted_execution_enabled,
    mcp_server,
)
from fastmcp_extensions.tool_filters import (
    CONFIG_TRUSTED_EXECUTION,
    EXCLUDE_MODULES_CONFIG_ARG,
    EXCLUDE_TOOLS_CONFIG_ARG,
    HEADER_EXCLUDE_MODULES,
    HEADER_EXCLUDE_TOOLS,
    HEADER_INCLUDE_MODULES,
    HEADER_NO_DESTRUCTIVE_TOOLS,
    HEADER_READONLY_MODE,
    INCLUDE_MODULES_CONFIG_ARG,
    NO_CLIENT_FILESYSTEM_CONFIG_ARG,
    NO_DESTRUCTIVE_TOOLS_CONFIG_ARG,
    READONLY_MODE_CONFIG_ARG,
    TRUSTED_EXECUTION_CONFIG_ARG,
    module_filter,
    trusted_execution_filter,
)

_HTTP_REQUEST_PATH = "fastmcp_extensions.tool_filters.get_http_request"


def _make_tool(*, requires_client_filesystem: bool) -> Tool:
    """Build a `Tool`, optionally annotated `requiresClientFilesystem=True`."""
    annotations_kwargs: dict[str, object] = {}
    if requires_client_filesystem:
        annotations_kwargs["requiresClientFilesystem"] = True
    return Tool(
        name="local_tool",
        description="A tool that may require client filesystem access",
        inputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(**annotations_kwargs),
    )


def _stdio_transport() -> object:
    """Patch the HTTP-request lookup to simulate the stdio (non-HTTP) transport."""
    return patch(
        _HTTP_REQUEST_PATH, side_effect=RuntimeError("No active HTTP request found.")
    )


def _http_transport() -> object:
    """Patch the HTTP-request lookup to simulate an active HTTP request."""
    return patch(_HTTP_REQUEST_PATH, return_value=MagicMock())


# =============================================================================
# Config-arg registration and precedence
# =============================================================================


@pytest.mark.unit
def test_standard_filters_include_trusted_execution_config_arg() -> None:
    """`include_standard_tool_filters=True` registers the `trusted_execution` config."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    config_names = [arg.name for arg in app.x_mcp_server_config.config_args]
    assert CONFIG_TRUSTED_EXECUTION in config_names


@pytest.mark.unit
def test_host_supplied_config_arg_overrides_standard_env_var() -> None:
    """A host-supplied `trusted_execution` arg backs the gate with its own env var."""
    custom = MCPServerConfigArg(
        name=CONFIG_TRUSTED_EXECUTION,
        env_var="AIRBYTE_MCP_TRUSTED_EXECUTION",
        default="0",
        required=False,
    )
    app = mcp_server(
        "test-server",
        server_config_args=[custom],
        include_standard_tool_filters=True,
    )
    trusted_args = [
        arg
        for arg in app.x_mcp_server_config.config_args
        if arg.name == CONFIG_TRUSTED_EXECUTION
    ]
    assert len(trusted_args) == 1
    assert trusted_args[0].env_var == "AIRBYTE_MCP_TRUSTED_EXECUTION"

    with patch.dict(os.environ, {"AIRBYTE_MCP_TRUSTED_EXECUTION": "1"}):
        assert is_trusted_execution_enabled(app) is True


# =============================================================================
# Footprint-changing configs must have no HTTP header source
# =============================================================================


@pytest.mark.parametrize(
    "config_arg",
    [
        pytest.param(TRUSTED_EXECUTION_CONFIG_ARG, id="trusted_execution"),
        pytest.param(NO_CLIENT_FILESYSTEM_CONFIG_ARG, id="no_client_filesystem"),
    ],
)
@pytest.mark.unit
def test_footprint_config_args_have_no_http_header(
    config_arg: MCPServerConfigArg,
) -> None:
    """Configs that change the host's exposure profile must not be caller-set."""
    assert config_arg.http_header_key is None


# =============================================================================
# Caller self-restriction headers are preserved (and normalized)
# =============================================================================


@pytest.mark.parametrize(
    "config_arg,expected_header",
    [
        pytest.param(READONLY_MODE_CONFIG_ARG, HEADER_READONLY_MODE, id="readonly"),
        pytest.param(
            NO_DESTRUCTIVE_TOOLS_CONFIG_ARG,
            HEADER_NO_DESTRUCTIVE_TOOLS,
            id="no_destructive",
        ),
        pytest.param(
            EXCLUDE_MODULES_CONFIG_ARG, HEADER_EXCLUDE_MODULES, id="exclude_modules"
        ),
        pytest.param(
            INCLUDE_MODULES_CONFIG_ARG, HEADER_INCLUDE_MODULES, id="include_modules"
        ),
        pytest.param(
            EXCLUDE_TOOLS_CONFIG_ARG, HEADER_EXCLUDE_TOOLS, id="exclude_tools"
        ),
    ],
)
@pytest.mark.unit
def test_self_restriction_headers_preserved(
    config_arg: MCPServerConfigArg,
    expected_header: str,
) -> None:
    """Caller self-restriction filters keep their HTTP header source."""
    assert config_arg.http_header_key == expected_header


@pytest.mark.unit
def test_no_destructive_tools_header_normalized() -> None:
    """The destructive-tools header follows the `X-MCP-` convention."""
    assert HEADER_NO_DESTRUCTIVE_TOOLS == "X-MCP-No-Destructive-Tools"


@pytest.mark.unit
def test_no_client_filesystem_header_constant_removed() -> None:
    """The `X-MCP-No-Client-Filesystem` header constant no longer exists."""
    from fastmcp_extensions import tool_filters

    assert not hasattr(tool_filters, "HEADER_NO_CLIENT_FILESYSTEM")


# =============================================================================
# is_trusted_execution_enabled
# =============================================================================


@pytest.mark.parametrize(
    "env_value,expected",
    [
        pytest.param(None, False, id="unset-defaults-off"),
        pytest.param("0", False, id="zero-off"),
        pytest.param("false", False, id="false-off"),
        pytest.param("", False, id="empty-off"),
        pytest.param("1", True, id="one-on"),
        pytest.param("true", True, id="true-on"),
        pytest.param("YES", True, id="yes-on"),
    ],
)
@pytest.mark.unit
def test_is_trusted_execution_enabled(env_value: str | None, expected: bool) -> None:
    """The gate reads truthiness from the server environment, defaulting off."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    env_patch = {} if env_value is None else {"MCP_TRUSTED_EXECUTION": env_value}
    with patch.dict(os.environ, env_patch, clear=False):
        if env_value is None:
            os.environ.pop("MCP_TRUSTED_EXECUTION", None)
        assert is_trusted_execution_enabled(app) is expected


# =============================================================================
# trusted_execution_filter
# =============================================================================


@pytest.mark.parametrize(
    "trusted_value,requires_fs,expected_visible",
    [
        pytest.param("0", True, False, id="off-fs-tool-hidden"),
        pytest.param("0", False, True, id="off-plain-tool-visible"),
        pytest.param("1", True, True, id="on-fs-tool-visible"),
        pytest.param("1", False, True, id="on-plain-tool-visible"),
    ],
)
@pytest.mark.unit
def test_trusted_execution_filter_stdio(
    trusted_value: str,
    requires_fs: bool,
    expected_visible: bool,
) -> None:
    """On stdio the gate hides filesystem tools unless trusted execution is on."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    tool = _make_tool(requires_client_filesystem=requires_fs)
    with patch.dict(
        os.environ, {"MCP_TRUSTED_EXECUTION": trusted_value}
    ), _stdio_transport():
        assert trusted_execution_filter(tool, app) is expected_visible


@pytest.mark.unit
def test_trusted_execution_filter_forced_off_under_http() -> None:
    """Even with trusted execution enabled, HTTP requests never expose FS tools."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    tool = _make_tool(requires_client_filesystem=True)
    with patch.dict(os.environ, {"MCP_TRUSTED_EXECUTION": "1"}), _http_transport():
        assert trusted_execution_filter(tool, app) is False


# =============================================================================
# assert_http_trusted_execution_disabled (permanent HTTP gate)
# =============================================================================


@pytest.mark.unit
def test_assert_http_trusted_execution_disabled_raises_when_enabled() -> None:
    """Enabling trusted execution on an HTTP entrypoint hard-fails at startup."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    with patch.dict(os.environ, {"MCP_TRUSTED_EXECUTION": "1"}), pytest.raises(
        RuntimeError, match="permanently incompatible with the HTTP transport"
    ):
        assert_http_trusted_execution_disabled(app)


@pytest.mark.unit
def test_assert_http_trusted_execution_disabled_passes_when_off() -> None:
    """The startup gate is a no-op when trusted execution is disabled."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    with patch.dict(os.environ, {"MCP_TRUSTED_EXECUTION": "0"}):
        assert assert_http_trusted_execution_disabled(app) is None


# =============================================================================
# module_filter incompatible-config hard-fail
# =============================================================================


@pytest.mark.unit
def test_module_filter_incompatible_config_hard_fails() -> None:
    """Setting both include and exclude modules hard-fails with remediation text."""
    app = mcp_server("test-server", include_standard_tool_filters=True)
    tool = _make_tool(requires_client_filesystem=False)
    with patch.dict(
        os.environ,
        {"MCP_EXCLUDE_MODULES": "mod_a", "MCP_INCLUDE_MODULES": "mod_b"},
    ), patch(
        "fastmcp_extensions.server_config.get_http_headers", return_value=None
    ), pytest.raises(ValueError, match="mutually exclusive"):
        module_filter(tool, app)
