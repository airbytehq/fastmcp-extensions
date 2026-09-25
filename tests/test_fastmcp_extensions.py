# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the fastmcp_extensions module."""

import warnings

import pytest
from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from mcp.types import Tool as McpTool
from mcp.types import ToolAnnotations

import fastmcp_extensions
import fastmcp_extensions.capability_tokens as capability_tokens
from fastmcp_extensions import (
    interactive_ui_filter,
    mcp_prompt,
    mcp_provider,
    mcp_resource,
    mcp_tool,
    register_mcp_tools,
)
from fastmcp_extensions.annotations import (
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
    READ_ONLY_HINT,
)
from fastmcp_extensions.decorators import (
    _REGISTERED_PROMPTS,
    _REGISTERED_PROVIDERS,
    _REGISTERED_RESOURCES,
    _REGISTERED_TOOLS,
    _clear_registrations,
)
from fastmcp_extensions.tool_filters import get_annotation


@pytest.mark.parametrize(
    "constant,expected_value",
    [
        pytest.param(READ_ONLY_HINT, "readOnlyHint", id="read_only_hint"),
        pytest.param(DESTRUCTIVE_HINT, "destructiveHint", id="destructive_hint"),
        pytest.param(IDEMPOTENT_HINT, "idempotentHint", id="idempotent_hint"),
        pytest.param(OPEN_WORLD_HINT, "openWorldHint", id="open_world_hint"),
    ],
)
@pytest.mark.unit
def test_annotation_constants(constant: str, expected_value: str) -> None:
    """Test that annotation constants have correct values."""
    assert constant == expected_value


@pytest.mark.unit
def test_all_exports() -> None:
    """Test that __all__ contains expected exports."""
    expected_exports = [
        "mcp_tool",
        "mcp_provider",
        "mcp_prompt",
        "mcp_resource",
        "register_mcp_tools",
        "register_mcp_prompts",
        "register_mcp_resources",
    ]
    assert hasattr(fastmcp_extensions, "__all__")
    for item in expected_exports:
        assert item in fastmcp_extensions.__all__, f"Missing export: {item}"


@pytest.mark.unit
def test_mcp_tool_decorator() -> None:
    """Test that mcp_tool decorator registers tools with auto-inferred mcp_module."""
    _clear_registrations()

    @mcp_tool(read_only=True)
    def my_test_tool() -> str:
        """A test tool."""
        return "test"

    assert len(_REGISTERED_TOOLS) == 1
    func, annotations = _REGISTERED_TOOLS[0]
    assert func.__name__ == "my_test_tool"
    # mcp_module is auto-inferred from module name (test_fastmcp_extensions)
    assert annotations["mcp_module"] == "test_fastmcp_extensions"
    assert annotations[READ_ONLY_HINT] is True

    _clear_registrations()


@pytest.mark.unit
def test_mcp_provider_decorator() -> None:
    """Test that mcp_provider decorator registers provider factories."""
    _clear_registrations()

    class TestProvider(Provider):
        pass

    @mcp_provider(annotations={"custom-flag": True})
    def my_test_provider() -> Provider:
        """A test provider."""
        return TestProvider()

    assert len(_REGISTERED_PROVIDERS) == 1
    func, annotations = _REGISTERED_PROVIDERS[0]
    assert func.__name__ == "my_test_provider"
    assert annotations["mcp_module"] == "test_fastmcp_extensions"
    assert annotations["custom-flag"] is True

    _clear_registrations()


def test_mcp_provider_interactive_ui_argument_is_accepted_noop() -> None:
    """`mcp_provider(interactive_ui=True)` is accepted but writes nothing.

    Provider tools are gated via the standard `_meta.ui` marker each tool
    carries, not a provider-level annotation.
    """
    _clear_registrations()

    class TestProvider(Provider):
        pass

    @mcp_provider(interactive_ui=True)
    def ui_provider() -> Provider:
        return TestProvider()

    assert _REGISTERED_PROVIDERS[0][1] == {"mcp_module": "test_fastmcp_extensions"}

    _clear_registrations()


@pytest.mark.parametrize(
    ("declares_ui_extension", "expected_visible"),
    [
        pytest.param(False, False, id="client-does-not-declare-ui"),
        pytest.param(True, True, id="client-declares-ui"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_tool_interactive_ui_argument_uses_standard_filter(
    monkeypatch: pytest.MonkeyPatch,
    declares_ui_extension: bool,
    expected_visible: bool,
) -> None:
    """Test that the typed tool argument reaches the standard UI filter."""
    _clear_registrations()

    @mcp_tool(
        interactive_ui=True,
        app=AppConfig(resource_uri="ui://test/dashboard.html"),
    )
    def show_dashboard() -> str:
        """Return dashboard data."""
        return "dashboard data"

    app = FastMCP("test")
    register_mcp_tools(app)
    tool = await app.get_tool("show_dashboard")
    assert tool is not None
    assert (tool.meta or {})["ui"]["resourceUri"] == "ui://test/dashboard.html"

    def no_context() -> None:
        raise RuntimeError

    monkeypatch.setattr(capability_tokens, "get_context", no_context)
    monkeypatch.setattr(
        capability_tokens,
        "get_http_headers",
        lambda **_: (
            {"x-mcp-extensions": "io.modelcontextprotocol/ui"}
            if declares_ui_extension
            else {}
        ),
    )

    assert interactive_ui_filter(tool, app) is expected_visible

    _clear_registrations()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_register_mcp_tools_registers_providers_with_missing_annotations() -> (
    None
):
    """Test that provider annotations fill missing provider tool annotations."""
    _clear_registrations()

    class TestProvider(Provider):
        async def _list_tools(self) -> list[Tool]:
            def provider_tool() -> str:
                return "test"

            return [
                Tool.from_function(
                    provider_tool,
                    name="provider_tool",
                    annotations=ToolAnnotations.model_validate(
                        {
                            "readOnlyHint": True,
                        }
                    ),
                    meta={"provider-owned": True},
                )
            ]

    @mcp_provider(
        annotations={
            "custom-flag": True,
            "provider-owned": False,
        }
    )
    def my_test_provider() -> Provider:
        return TestProvider()

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module="test_fastmcp_extensions")

    tool = await app.get_tool("provider_tool")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.meta == {
        "custom-flag": True,
        "mcp_module": "test_fastmcp_extensions",
        "provider-owned": True,
    }

    _clear_registrations()


@pytest.mark.unit
def test_mcp_prompt_decorator() -> None:
    """Test that mcp_prompt decorator registers prompts with auto-inferred mcp_module."""
    _clear_registrations()

    @mcp_prompt("test_prompt", "A test prompt")
    def my_test_prompt() -> list[dict[str, str]]:
        """A test prompt."""
        return [{"role": "user", "content": "Hello"}]

    assert len(_REGISTERED_PROMPTS) == 1
    func, annotations = _REGISTERED_PROMPTS[0]
    assert func.__name__ == "my_test_prompt"
    assert annotations["name"] == "test_prompt"
    assert annotations["description"] == "A test prompt"
    # mcp_module is auto-inferred from module name (test_fastmcp_extensions)
    assert annotations["mcp_module"] == "test_fastmcp_extensions"

    _clear_registrations()


@pytest.mark.unit
def test_mcp_resource_decorator() -> None:
    """Test that mcp_resource decorator registers resources with auto-inferred mcp_module."""
    _clear_registrations()

    @mcp_resource(
        uri="test://resource",
        description="A test resource",
        mime_type="application/json",
    )
    def my_test_resource() -> dict[str, str]:
        """A test resource."""
        return {"key": "value"}

    assert len(_REGISTERED_RESOURCES) == 1
    func, annotations = _REGISTERED_RESOURCES[0]
    assert func.__name__ == "my_test_resource"
    assert annotations["uri"] == "test://resource"
    assert annotations["description"] == "A test resource"
    assert annotations["mime_type"] == "application/json"
    # mcp_module is auto-inferred from module name (test_fastmcp_extensions)
    assert annotations["mcp_module"] == "test_fastmcp_extensions"

    _clear_registrations()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_register_mcp_tools_exclude_args_hides_param_and_injects_default() -> (
    None
):
    """Excluded params leave the tool schema but still receive their default."""
    _clear_registrations()

    @mcp_tool()
    def list_things(prefix: str, workspace_id: str = "ws-default") -> str:
        """List things."""
        return f"{prefix}:{workspace_id}"

    app = FastMCP("test")
    register_mcp_tools(
        app, mcp_module="test_fastmcp_extensions", exclude_args=["workspace_id"]
    )

    tool = await app.get_tool("list_things")
    assert tool is not None
    assert "workspace_id" not in tool.parameters["properties"]
    result = await tool.run({"prefix": "p"})
    assert result.structured_content == {"result": "p:ws-default"}

    _clear_registrations()


@pytest.mark.unit
def test_register_mcp_tools_exclude_args_requires_default() -> None:
    """Excluding a parameter without a default raises a clear ValueError."""
    _clear_registrations()

    @mcp_tool()
    def needs_arg(workspace_id: str) -> str:
        """Needs an argument."""
        return workspace_id

    app = FastMCP("test")
    with pytest.raises(ValueError, match="workspace_id"):
        register_mcp_tools(
            app, mcp_module="test_fastmcp_extensions", exclude_args=["workspace_id"]
        )

    _clear_registrations()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_register_mcp_tools_routes_annotations_and_meta() -> None:
    """Standard hints land on `tool.annotations`; custom keys land on `tool.meta`."""
    _clear_registrations()

    @mcp_tool(
        read_only=True,
        interactive_ui=True,
        requires_client_filesystem=True,
        app=AppConfig(resource_uri="ui://test/annotated.html"),
        meta={"custom-flag": True},
    )
    def annotated_tool() -> str:
        """Annotated tool."""
        return "ok"

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module="test_fastmcp_extensions")

    tool = await app.get_tool("annotated_tool")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.meta == {
        "ui": {"resourceUri": "ui://test/annotated.html"},
        "custom-flag": True,
        "requiresClientFilesystem": True,
        "mcp_module": "test_fastmcp_extensions",
    }
    assert get_annotation(tool, "readOnlyHint") is True
    assert get_annotation(tool, "mcp_module") == "test_fastmcp_extensions"
    assert get_annotation(tool, "missing", default=False) is False

    _clear_registrations()


@pytest.mark.unit
def test_mcp_tool_interactive_ui_requires_app() -> None:
    """`interactive_ui=True` without `app=` is a configuration error."""
    _clear_registrations()

    with pytest.raises(ValueError, match="interactive_ui=True requires app="):

        @mcp_tool(interactive_ui=True)
        def ui_tool() -> str:
            return "ok"

    _clear_registrations()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_mcp_tool_meta_arg_lands_in_tool_meta() -> None:
    """Explicit `meta=` merges with custom annotation keys in `tool.meta`."""
    _clear_registrations()

    @mcp_tool(meta={"foo": 1})
    def meta_tool() -> str:
        """Meta tool."""
        return "ok"

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module="test_fastmcp_extensions")

    tool = await app.get_tool("meta_tool")
    assert tool is not None
    assert tool.meta == {"foo": 1, "mcp_module": "test_fastmcp_extensions"}

    _clear_registrations()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_interactive_ui_filter_hides_app_tool_without_flag() -> None:
    """A tool with `app=` is gated even when `interactive_ui` was not passed."""
    _clear_registrations()

    @mcp_tool(app=AppConfig(resource_uri="ui://test/plain.html"))
    def app_tool() -> str:
        """App tool."""
        return "ok"

    app = FastMCP("test")
    register_mcp_tools(app, mcp_module="test_fastmcp_extensions")

    tool = await app.get_tool("app_tool")
    assert tool is not None
    assert (tool.meta or {})["ui"]["resourceUri"] == "ui://test/plain.html"

    def no_context() -> None:
        raise RuntimeError

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(capability_tokens, "get_context", no_context)
    monkeypatch.setattr(capability_tokens, "get_http_headers", lambda **_: {})
    assert interactive_ui_filter(tool, app) is False
    monkeypatch.undo()

    _clear_registrations()


@pytest.mark.unit
def test_get_annotation_camelcase_hint_emits_no_deprecation_warning() -> None:
    """`readOnlyHint` lookups resolve without the camelCase deprecation shim."""
    tool = McpTool(
        name="tool",
        description="tool",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert get_annotation(tool, "readOnlyHint") is True
