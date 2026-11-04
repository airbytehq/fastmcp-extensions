# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Shared loader for the `--app module.path:attribute` CLI argument."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def import_app(app_path: str) -> FastMCP:
    """Import an app from a module:attribute path."""
    if ":" not in app_path:
        msg = f"Invalid app path '{app_path}'. Expected format: 'module.path:attribute'"
        raise ValueError(msg)

    module_path, attr_name = app_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr_name)
