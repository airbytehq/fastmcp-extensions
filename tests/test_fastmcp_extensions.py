# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the fastmcp_extensions module."""

import pytest
from fastmcp import FastMCP
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
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
    ANNOTATION_INTERACTIVE_UI,
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

    @mcp_provider(annotations={"interactive-ui": True})
    def my_test_provider() -> Provider:
        """A test provider."""
        return TestProvider()

    assert len(_REGISTERED_PROVIDERS) == 1
    func, annotations = _REGISTERED_PROVIDERS[0]
    assert func.__name__ == "my_test_provider"
    assert annotations["mcp_module"] == "test_fastmcp_extensions"
    assert annotations["interactive-ui"] is True

    _clear_registrations()


def test_mcp_provider_interactive_ui_argument_respects_explicit_annotation() -> None:
    """Test that provider UI annotations are type-safe and caller-overridable."""
    _clear_registrations()

    class TestProvider(Provider):
        pass

    @mcp_provider(interactive_ui=True)
    def ui_provider() -> Provider:
        return TestProvider()

    @mcp_provider(
        interactive_ui=True,
        annotations={ANNOTATION_INTERACTIVE_UI: False},
    )
    def overridden_provider() -> Provider:
        return TestProvider()

    assert _REGISTERED_PROVIDERS[0][1][ANNOTATION_INTERACTIVE_UI] is True
    assert _REGISTERED_PROVIDERS[1][1][ANNOTATION_INTERACTIVE_UI] is False

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

    @mcp_tool(interactive_ui=True)
    def show_dashboard() -> str:
        """Return dashboard data."""
        return "dashboard data"

    app = FastMCP("test")
    register_mcp_tools(app)
    tool = await app.get_tool("show_dashboard")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.model_extra[ANNOTATION_INTERACTIVE_UI] is True

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
                            "provider-owned": True,
                        }
                    ),
                )
            ]

    @mcp_provider(
        annotations={
            "interactive-ui": True,
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
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.model_extra == {
        "interactive-ui": True,
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
