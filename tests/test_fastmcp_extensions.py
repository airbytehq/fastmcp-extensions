# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the fastmcp_extensions module."""

import inspect
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

import fastmcp_extensions
from fastmcp_extensions import (
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
    READ_ONLY_HINT,
    ArgExclusionFilterFn,
    ToolFilterFn,
    clear_registrations,
    mcp_prompt,
    mcp_resource,
    mcp_tool,
    register_mcp_tools,
)
from fastmcp_extensions.decorators import (
    get_registered_prompts,
    get_registered_resources,
    get_registered_tools,
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
        "DESTRUCTIVE_HINT",
        "IDEMPOTENT_HINT",
        "OPEN_WORLD_HINT",
        "READ_ONLY_HINT",
        "mcp_tool",
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
    clear_registrations()

    @mcp_tool(read_only=True)
    def my_test_tool() -> str:
        """A test tool."""
        return "test"

    tools = get_registered_tools()
    assert len(tools) == 1
    func, annotations = tools[0]
    assert func.__name__ == "my_test_tool"
    # mcp_module is auto-inferred from module name (test_fastmcp_extensions)
    assert annotations["mcp_module"] == "test_fastmcp_extensions"
    assert annotations[READ_ONLY_HINT] is True

    clear_registrations()


@pytest.mark.unit
def test_mcp_prompt_decorator() -> None:
    """Test that mcp_prompt decorator registers prompts with auto-inferred mcp_module."""
    clear_registrations()

    @mcp_prompt("test_prompt", "A test prompt")
    def my_test_prompt() -> list[dict[str, str]]:
        """A test prompt."""
        return [{"role": "user", "content": "Hello"}]

    prompts = get_registered_prompts()
    assert len(prompts) == 1
    func, annotations = prompts[0]
    assert func.__name__ == "my_test_prompt"
    assert annotations["name"] == "test_prompt"
    assert annotations["description"] == "A test prompt"
    # mcp_module is auto-inferred from module name (test_fastmcp_extensions)
    assert annotations["mcp_module"] == "test_fastmcp_extensions"

    clear_registrations()


@pytest.mark.unit
def test_mcp_resource_decorator() -> None:
    """Test that mcp_resource decorator registers resources with auto-inferred mcp_module."""
    clear_registrations()

    @mcp_resource(
        uri="test://resource",
        description="A test resource",
        mime_type="application/json",
    )
    def my_test_resource() -> dict[str, str]:
        """A test resource."""
        return {"key": "value"}

    resources = get_registered_resources()
    assert len(resources) == 1
    func, annotations = resources[0]
    assert func.__name__ == "my_test_resource"
    assert annotations["uri"] == "test://resource"
    assert annotations["description"] == "A test resource"
    assert annotations["mime_type"] == "application/json"
    # mcp_module is auto-inferred from module name (test_fastmcp_extensions)
    assert annotations["mcp_module"] == "test_fastmcp_extensions"

    clear_registrations()


@pytest.mark.unit
def test_register_mcp_tools_with_tool_filter() -> None:
    """Test that tool_filter can dynamically exclude tools from registration."""
    clear_registrations()

    @mcp_tool(read_only=True)
    def readonly_tool() -> str:
        """A read-only tool."""
        return "readonly"

    @mcp_tool(read_only=False)
    def write_tool() -> str:
        """A write tool."""
        return "write"

    # Create a mock FastMCP app
    mock_app = MagicMock()
    registered_tools: list[str] = []

    def capture_tool(
        func: Callable[..., Any],
        annotations: dict[str, Any] | None = None,
        exclude_args: list[str] | None = None,
    ) -> None:
        registered_tools.append(func.__name__)

    mock_app.tool = capture_tool

    # Filter to only include read-only tools
    def only_readonly(func: Callable[..., Any], annotations: dict[str, Any]) -> bool:
        return annotations.get(READ_ONLY_HINT, False)

    register_mcp_tools(
        mock_app,
        mcp_module="test_fastmcp_extensions",
        tool_filter=only_readonly,
    )

    assert "readonly_tool" in registered_tools
    assert "write_tool" not in registered_tools

    clear_registrations()


@pytest.mark.unit
def test_register_mcp_tools_with_arg_exclusion_filter() -> None:
    """Test that arg_exclusion_filter can dynamically exclude args from tool schema."""
    clear_registrations()

    @mcp_tool()
    def tool_with_workspace(workspace_id: str, other_arg: str) -> str:
        """A tool with workspace_id parameter."""
        return f"{workspace_id}:{other_arg}"

    # Create a mock FastMCP app
    mock_app = MagicMock()
    captured_exclude_args: list[list[str] | None] = []

    def capture_tool(
        func: Callable[..., Any],
        annotations: dict[str, Any] | None = None,
        exclude_args: list[str] | None = None,
    ) -> None:
        captured_exclude_args.append(exclude_args)

    mock_app.tool = capture_tool

    # Filter to exclude workspace_id
    def exclude_workspace_id(
        func: Callable[..., Any], annotations: dict[str, Any]
    ) -> list[str] | None:
        params = set(inspect.signature(func).parameters.keys())
        return [name for name in ["workspace_id"] if name in params] or None

    register_mcp_tools(
        mock_app,
        mcp_module="test_fastmcp_extensions",
        arg_exclusion_filter=exclude_workspace_id,
    )

    assert len(captured_exclude_args) == 1
    assert captured_exclude_args[0] is not None
    assert "workspace_id" in captured_exclude_args[0]
    assert "other_arg" not in captured_exclude_args[0]

    clear_registrations()


@pytest.mark.unit
def test_register_mcp_tools_combines_static_and_dynamic_exclusions() -> None:
    """Test that static exclude_args and dynamic arg_exclusion_filter are combined."""
    clear_registrations()

    @mcp_tool()
    def tool_with_many_args(arg_a: str, arg_b: str, arg_c: str) -> str:
        """A tool with multiple parameters."""
        return f"{arg_a}:{arg_b}:{arg_c}"

    # Create a mock FastMCP app
    mock_app = MagicMock()
    captured_exclude_args: list[list[str] | None] = []

    def capture_tool(
        func: Callable[..., Any],
        annotations: dict[str, Any] | None = None,
        exclude_args: list[str] | None = None,
    ) -> None:
        captured_exclude_args.append(exclude_args)

    mock_app.tool = capture_tool

    # Dynamic filter excludes arg_b
    def exclude_arg_b(
        func: Callable[..., Any], annotations: dict[str, Any]
    ) -> list[str] | None:
        return ["arg_b"]

    # Static exclusion for arg_a, dynamic exclusion for arg_b
    register_mcp_tools(
        mock_app,
        mcp_module="test_fastmcp_extensions",
        exclude_args=["arg_a"],
        arg_exclusion_filter=exclude_arg_b,
    )

    assert len(captured_exclude_args) == 1
    assert captured_exclude_args[0] is not None
    # Both arg_a (static) and arg_b (dynamic) should be excluded
    assert "arg_a" in captured_exclude_args[0]
    assert "arg_b" in captured_exclude_args[0]
    assert "arg_c" not in captured_exclude_args[0]

    clear_registrations()


@pytest.mark.unit
def test_register_mcp_tools_filter_returns_none() -> None:
    """Test that arg_exclusion_filter returning None results in no exclusions."""
    clear_registrations()

    @mcp_tool()
    def simple_tool(arg: str) -> str:
        """A simple tool."""
        return arg

    # Create a mock FastMCP app
    mock_app = MagicMock()
    captured_exclude_args: list[list[str] | None] = []

    def capture_tool(
        func: Callable[..., Any],
        annotations: dict[str, Any] | None = None,
        exclude_args: list[str] | None = None,
    ) -> None:
        captured_exclude_args.append(exclude_args)

    mock_app.tool = capture_tool

    # Filter that returns None (no exclusions)
    def no_exclusions(
        func: Callable[..., Any], annotations: dict[str, Any]
    ) -> list[str] | None:
        return None

    register_mcp_tools(
        mock_app,
        mcp_module="test_fastmcp_extensions",
        arg_exclusion_filter=no_exclusions,
    )

    assert len(captured_exclude_args) == 1
    assert captured_exclude_args[0] is None

    clear_registrations()


@pytest.mark.unit
def test_type_aliases_exported() -> None:
    """Test that ToolFilterFn and ArgExclusionFilterFn type aliases are exported."""
    assert "ToolFilterFn" in fastmcp_extensions.__all__
    assert "ArgExclusionFilterFn" in fastmcp_extensions.__all__

    # Verify they are callable types (type aliases exist)
    assert ToolFilterFn is not None
    assert ArgExclusionFilterFn is not None
