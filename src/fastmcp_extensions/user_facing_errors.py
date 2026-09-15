# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Middleware for returning concise errors to MCP clients.

Use `UserFacingErrorMiddleware` when a server has a known set of exceptions
that should be shown to clients without a traceback:

```python
from fastmcp_extensions import UserFacingErrorMiddleware

app.add_middleware(UserFacingErrorMiddleware((ValueError,)))
```
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools import ToolResult

if TYPE_CHECKING:
    from mcp import types as mt


UserFacingErrorFormatter = Callable[[BaseException], str]


class UserFacingErrorMiddleware(Middleware):
    """Convert configured exception types into concise `ToolError`s.

    Exceptions matching `error_types` are re-raised as `ToolError(formatter(error))`
    with the traceback suppressed (`from None`), so the client sees only the
    message. All other exceptions pass through to FastMCP's default handling
    (logged with traceback, subject to `mask_error_details`).
    """

    def __init__(
        self,
        error_types: Sequence[type[BaseException]],
        *,
        formatter: UserFacingErrorFormatter = str,
    ) -> None:
        """Initialize the middleware with the exception types to convert."""
        if not error_types:
            raise ValueError("error_types must contain at least one exception type")
        self._error_types = tuple(error_types)
        self._formatter = formatter

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except self._error_types as error:
            cause = error.__cause__
            if isinstance(error, ToolError) and cause is not None:
                error = cause
            raise ToolError(self._formatter(error)) from None
        except ToolError as error:
            cause = error.__cause__
            if cause is not None and isinstance(cause, self._error_types):
                raise ToolError(self._formatter(cause)) from None
            raise
