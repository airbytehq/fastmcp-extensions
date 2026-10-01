# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Best-effort, fail-open user consent prompts for MCP tools.

Use `request_consent` (sync tools) or `request_consent_async` (async tools) to ask the
user to confirm an action before a tool performs it:

```python
from mcp.types import InputRequiredResult

from fastmcp_extensions import request_consent


@app.tool(annotations={"destructiveHint": True})
def delete_thing(ctx: Context, name: str) -> str | InputRequiredResult:
    consent = request_consent(ctx, f"Permanently delete '{name}'?")
    if isinstance(consent, InputRequiredResult):
        return consent
    if not consent:
        return "Not confirmed by the user. Nothing was deleted."
    ...
```

The prompt fails open: the action proceeds whenever the client cannot show a prompt,
and is stopped only when the user explicitly declines, cancels, or leaves the box
unchecked. This is a UX courtesy, not a security boundary; clients may auto-answer
elicitations. Hide destructive tools with a request header (for example
`X-MCP-No-Destructive-Tools: 1`) or a client-side tool block when a real guard is needed.

Clients on MCP `2026-07-28` and later are asked via Multi Round-Trip Requests (an
`InputRequiredResult` the tool must return as-is; the client retries the call with the
answer), which also works on stateless HTTP. Older clients are asked via a
server-initiated `elicitation/create` request, which needs a transport with a
back-channel (stdio or stateful HTTP); otherwise the action proceeds.
"""

from __future__ import annotations

import anyio.from_thread
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.shared.exceptions import MCPError
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from pydantic import BaseModel, Field, create_model

DEFAULT_CONSENT_FIELD_TITLE = "Confirm"
"""Default label of the consent checkbox."""

DEFAULT_CONSENT_REQUEST_KEY = "consent"
"""Default `input_requests` key used for the Multi Round-Trip Requests prompt."""

_MRTR_MIN_PROTOCOL_VERSION = "2026-07-28"
_CONSENT_FIELD = "confirm"


def request_consent(
    ctx: Context,
    message: str,
    *,
    field_title: str = DEFAULT_CONSENT_FIELD_TITLE,
    request_key: str = DEFAULT_CONSENT_REQUEST_KEY,
) -> bool | InputRequiredResult:
    """Ask the user to confirm an action from a sync tool, proceeding when the client cannot ask.

    Returns `True` to proceed, `False` when the user declined, cancelled, or did not check
    the box, or an `InputRequiredResult` that the tool must return as-is so the client can
    prompt the user and retry the call with the answer.

    Must be called from a sync tool body, which FastMCP runs in a worker thread.
    """
    prompt = _resolve_without_elicit(ctx, message, field_title, request_key)
    if prompt is not None:
        return prompt
    try:
        result = anyio.from_thread.run(ctx.elicit, message, _consent_model(field_title))
    except (ToolError, MCPError):
        return True
    return _is_confirmed(result)


async def request_consent_async(
    ctx: Context,
    message: str,
    *,
    field_title: str = DEFAULT_CONSENT_FIELD_TITLE,
    request_key: str = DEFAULT_CONSENT_REQUEST_KEY,
) -> bool | InputRequiredResult:
    """Ask the user to confirm an action from an async tool, proceeding when the client cannot ask.

    Same contract as `request_consent`.
    """
    prompt = _resolve_without_elicit(ctx, message, field_title, request_key)
    if prompt is not None:
        return prompt
    try:
        result = await ctx.elicit(message, _consent_model(field_title))
    except (ToolError, MCPError):
        return True
    return _is_confirmed(result)


def _resolve_without_elicit(
    ctx: Context,
    message: str,
    field_title: str,
    request_key: str,
) -> bool | InputRequiredResult | None:
    """Resolve consent without a server-initiated request, or return `None` to elicit."""
    if not isinstance(ctx, Context) or ctx.request_context is None:
        return True

    if ctx.request_context.protocol_version >= _MRTR_MIN_PROTOCOL_VERSION:
        responses = ctx.input_responses
        response = responses.get(request_key) if responses else None
        if isinstance(response, ElicitResult):
            return (
                response.action == "accept"
                and (response.content or {}).get(_CONSENT_FIELD) is True
            )
        if not _client_declares_elicitation(ctx):
            return True
        return InputRequiredResult(
            input_requests={
                request_key: ElicitRequest(
                    params=ElicitRequestFormParams(
                        message=message,
                        requested_schema=_consent_schema(field_title),
                    ),
                ),
            },
        )

    if not _client_declares_elicitation(ctx):
        return True
    return None


def _client_declares_elicitation(ctx: Context) -> bool:
    """Return whether the client declared the elicitation capability."""
    capabilities = ctx.session.client_capabilities
    return capabilities is not None and capabilities.elicitation is not None


def _consent_schema(field_title: str) -> dict[str, object]:
    """Return the form schema for the Multi Round-Trip Requests prompt."""
    return {
        "type": "object",
        "properties": {
            _CONSENT_FIELD: {"type": "boolean", "title": field_title},
        },
        "required": [_CONSENT_FIELD],
    }


def _consent_model(field_title: str) -> type[BaseModel]:
    """Return the form model for the server-initiated elicitation prompt."""
    return create_model("Consent", confirm=(bool, Field(title=field_title)))


def _is_confirmed(result: object) -> bool:
    """Return whether a server-initiated elicitation result confirms the action."""
    if not isinstance(result, AcceptedElicitation):
        return False
    data = result.data
    return isinstance(data, BaseModel) and data.model_dump().get(_CONSENT_FIELD) is True
