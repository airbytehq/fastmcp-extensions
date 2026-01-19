# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the fastmcp_extensions module."""

import pytest

import fastmcp_extensions
from fastmcp_extensions import (
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
    READ_ONLY_HINT,
    mcp_prompt,
    mcp_resource,
    mcp_tool,
)
from fastmcp_extensions.decorators import (
    _REGISTERED_PROMPTS,
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
