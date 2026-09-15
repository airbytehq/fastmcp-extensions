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
        self._caught_types = (ToolError, *self._error_types)
        self._formatter = formatter

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except self._caught_types as error:
            user_error = self._match(error)
            if user_error is None:
                raise
            raise ToolError(self._formatter(user_error)) from None

    def _match(self, error: BaseException) -> BaseException | None:
        """Return the configured exception behind `error`, if any.

        FastMCP may wrap tool exceptions in `ToolError` before middleware sees them;
        the original exception is then available as `__cause__`.
        """
        cause = error.__cause__
        if isinstance(error, ToolError) and isinstance(cause, self._error_types):
            return cause
        if isinstance(error, self._error_types):
            return error
        return None
