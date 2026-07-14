# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Environment-variable helpers for resolving MCP server configuration."""

from __future__ import annotations

from collections.abc import Mapping


def get_env(
    env: Mapping[str, str],
    key: str,
    default: str | None = None,
) -> str | None:
    """Return `env[key]`, or `default` when the key is unset or empty.

    Treats an empty string the same as an absent key, so callers can replace the
    repetitive `env.get(key) or default` idiom with a single call.
    """
    value = env.get(key)
    return value if value else default
