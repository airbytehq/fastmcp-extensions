# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""MCP prompt utilities.

This module provides utilities for working with MCP prompts, including
a generic get_prompt_text helper for agents that cannot otherwise access
prompt assets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp import Client

if TYPE_CHECKING:
    from fastmcp import FastMCP


async def get_prompt_text(
    app: FastMCP,
    prompt_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Get the text content of a prompt by name.

    This is a helper function for agents that cannot otherwise access prompt
    assets directly. It retrieves the prompt and returns its text content.

    Args:
        app: The FastMCP app instance
        prompt_name: Name of the prompt to retrieve
        arguments: Optional arguments to pass to the prompt

    Returns:
        The text content of the prompt

    Raises:
        ValueError: If the prompt is not found or has no content
    """
    async with Client(app) as client:
        result = await client.get_prompt(prompt_name, arguments or {})

        if not result.messages:
            raise ValueError(f"Prompt '{prompt_name}' returned no messages")

        text_parts = []
        for message in result.messages:
            if hasattr(message, "content"):
                content = message.content
                if isinstance(content, str):
                    text_parts.append(content)
                elif hasattr(content, "text"):
                    text_parts.append(content.text)

        if not text_parts:
            raise ValueError(f"Prompt '{prompt_name}' returned no text content")

        return "\n\n".join(text_parts)


async def list_prompts(app: FastMCP) -> list[dict[str, Any]]:
    """List all available prompts.

    Args:
        app: The FastMCP app instance

    Returns:
        List of prompt information dictionaries
    """
    async with Client(app) as client:
        prompts = await client.list_prompts()

        return [
            {
                "name": p.name,
                "description": p.description,
                "arguments": [
                    {
                        "name": arg.name,
                        "description": arg.description,
                        "required": arg.required,
                    }
                    for arg in (p.arguments or [])
                ],
            }
            for p in prompts
        ]
