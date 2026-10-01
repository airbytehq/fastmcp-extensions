# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Best-effort, fail-open user approval and consent prompts for MCP tools.

Approval prompts
----------------

`request_approval` (sync tools) and `request_approval_async` (async tools) ask the user to
approve a list of actions before a tool performs them. The prompt layout is fixed:

```python
from mcp.types import InputRequiredResult

from fastmcp_extensions import format_not_approved_message, request_approval


@app.tool(annotations={"destructiveHint": True})
def delete_thing(ctx: Context, name: str) -> str | InputRequiredResult:
    actions_to_take = [f"Permanently delete '{name}'"]
    approval = request_approval(ctx, actions_to_take, "This cannot be undone.")
    if isinstance(approval, InputRequiredResult):
        return approval
    if not approval:
        return format_not_approved_message(actions_to_take)
    ...
```

The user sees:

```text
Your agent is attempting to perform the following actions:

- Permanently delete 'delete-me-1'

This cannot be undone.

Do you approve these actions? To approve, check "Yes, I approve." and submit. To decline,
submit without checking it.

[ ] Yes, I approve.
```

Generic consent prompts
-----------------------

`request_consent` and `request_consent_async` take a caller-written `message` and a
`form`: `ConsentCheckbox` (one checkbox that must be checked) or `ConsentChoice` (a select
between a reject option, selected by default, and an approve option). MCP forms have no
button-label field, so the client picks the submit button's label (Goose shows a single
"Submit"); the message and form fields must carry the meaning.

Semantics
---------

Prompts fail open: the tool proceeds whenever the client cannot show a prompt, and is
stopped only when the user declines, cancels, or submits without approving. This is a UX
courtesy, not a security boundary; clients may auto-answer elicitations. Hide destructive
tools with a request header (for example `X-MCP-No-Destructive-Tools: 1`) or a
client-side tool block when a real guard is needed.

Clients on MCP `2026-07-28` and later are asked via Multi Round-Trip Requests (an
`InputRequiredResult` the tool must return as-is; the client retries the call with the
answer), which also works on stateless HTTP. Older clients are asked via a
server-initiated `elicitation/create` request, which needs a transport with a
back-channel (stdio or stateful HTTP); otherwise the tool proceeds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import anyio.from_thread
from fastmcp import Context
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)

DEFAULT_CONSENT_REQUEST_KEY = "consent"
"""Default `input_requests` key used for the Multi Round-Trip Requests prompt."""

_MRTR_MIN_PROTOCOL_VERSION = "2026-07-28"
_TITLED_ONE_OF_MIN_PROTOCOL_VERSION = "2025-11-25"
_CHECKBOX_FIELD = "approve"
_CHOICE_FIELD = "decision"


@dataclass(frozen=True)
class ConsentCheckbox:
    """A single checkbox that must be checked to approve. Unchecked by default."""

    label: str = "Yes, I approve."
    """Text shown next to the checkbox.

    Sent as both the field title and description, because some clients (for example
    Goose) show only the description next to a checkbox.
    """

    def requested_schema(self, protocol_version: str) -> dict[str, object]:
        """Return the elicitation form schema."""
        del protocol_version
        return _object_schema(
            _CHECKBOX_FIELD,
            {
                "type": "boolean",
                "title": self.label,
                "description": self.label,
                "default": False,
            },
        )

    def is_approved(self, content: Mapping[str, object]) -> bool:
        """Return whether the submitted form approves the actions."""
        return content.get(_CHECKBOX_FIELD) is True


@dataclass(frozen=True)
class ConsentChoice:
    """A select between a reject option (selected by default) and an approve option.

    The option labels are also the submitted values, so clients that show raw enum values
    still show readable options.
    """

    approve_label: str = "Approve"
    """Option that lets the actions proceed."""

    reject_label: str = "Decline"
    """Option that stops the actions. Selected by default."""

    title: str = "Decision"
    """Label of the select field."""

    def __post_init__(self) -> None:
        """Reject identical option labels, which would make the answer ambiguous."""
        if self.approve_label == self.reject_label:
            raise ValueError("approve_label and reject_label must differ.")

    def requested_schema(self, protocol_version: str) -> dict[str, object]:
        """Return the elicitation form schema.

        Uses titled `oneOf` options on MCP `2025-11-25` and later, and `enum` with
        `enumNames` otherwise.
        """
        options = [self.reject_label, self.approve_label]
        field: dict[str, object] = {"type": "string", "title": self.title}
        if protocol_version >= _TITLED_ONE_OF_MIN_PROTOCOL_VERSION:
            field["oneOf"] = [{"const": option, "title": option} for option in options]
        else:
            field["enum"] = options
            field["enumNames"] = options
        field["default"] = self.reject_label
        return _object_schema(_CHOICE_FIELD, field)

    def is_approved(self, content: Mapping[str, object]) -> bool:
        """Return whether the submitted form approves the actions."""
        return content.get(_CHOICE_FIELD) == self.approve_label


ConsentForm = ConsentCheckbox | ConsentChoice
"""A consent form layout accepted by `request_consent` and `request_consent_async`."""


_APPROVAL_CHECKBOX = ConsentCheckbox(label="Yes, I approve.")


def format_approval_message(
    actions_to_take: Sequence[str],
    additional_notes: str | None = None,
) -> str:
    """Return the prompt text shown above the approval checkbox."""
    if isinstance(actions_to_take, str) or not actions_to_take:
        raise ValueError("actions_to_take must be a non-empty list of strings.")
    label = _APPROVAL_CHECKBOX.label
    sections = [
        "Your agent is attempting to perform the following actions:",
        "\n".join(f"- {action}" for action in actions_to_take),
    ]
    if additional_notes:
        sections.append(additional_notes)
    sections.append(
        f'Do you approve these actions? To approve, check "{label}" and submit. '
        "To decline, submit without checking it."
    )
    return "\n\n".join(sections)


def format_not_approved_message(actions_to_take: Sequence[str]) -> str:
    """Return a tool result telling the agent the user did not approve the actions."""
    actions = "\n".join(f"- {action}" for action in actions_to_take)
    return (
        f"The user did not approve these actions, so nothing was done:\n{actions}\n\n"
        "Do not retry them. Ask the user how they would like to proceed instead."
    )


def request_approval(
    ctx: Context,
    actions_to_take: Sequence[str],
    additional_notes: str | None = None,
    *,
    request_key: str = DEFAULT_CONSENT_REQUEST_KEY,
) -> bool | InputRequiredResult:
    """Ask the user to approve actions from a sync tool, proceeding when the client cannot ask.

    The prompt lists `actions_to_take`, then `additional_notes`, then asks for approval
    with a "Yes, I approve." checkbox (see `format_approval_message`). Return values match
    `request_consent`.
    """
    return request_consent(
        ctx,
        format_approval_message(actions_to_take, additional_notes),
        form=_APPROVAL_CHECKBOX,
        request_key=request_key,
    )


async def request_approval_async(
    ctx: Context,
    actions_to_take: Sequence[str],
    additional_notes: str | None = None,
    *,
    request_key: str = DEFAULT_CONSENT_REQUEST_KEY,
) -> bool | InputRequiredResult:
    """Ask the user to approve actions from an async tool, proceeding when the client cannot ask.

    Same contract as `request_approval`.
    """
    return await request_consent_async(
        ctx,
        format_approval_message(actions_to_take, additional_notes),
        form=_APPROVAL_CHECKBOX,
        request_key=request_key,
    )


def request_consent(
    ctx: Context,
    message: str,
    *,
    form: ConsentForm | None = None,
    request_key: str = DEFAULT_CONSENT_REQUEST_KEY,
) -> bool | InputRequiredResult:
    """Show `message` and `form` from a sync tool, proceeding when the client cannot ask.

    `form` defaults to `ConsentCheckbox()`.

    Returns `True` to proceed, `False` when the user declined, cancelled, or submitted
    without approving, or an `InputRequiredResult` that the tool must return as-is so the
    client can prompt the user and retry the call with the answer.

    Must be called from a sync tool body, which FastMCP runs in a worker thread.
    """
    form = form or ConsentCheckbox()
    prompt = _resolve_without_elicit(ctx, message, form, request_key)
    if prompt is not None:
        return prompt
    schema = form.requested_schema(_protocol_version(ctx))
    try:
        result = anyio.from_thread.run(_elicit, ctx, message, schema)
    except (ToolError, MCPError):
        return True
    return _is_approved(form, result)


async def request_consent_async(
    ctx: Context,
    message: str,
    *,
    form: ConsentForm | None = None,
    request_key: str = DEFAULT_CONSENT_REQUEST_KEY,
) -> bool | InputRequiredResult:
    """Show `message` and `form` from an async tool, proceeding when the client cannot ask.

    Same contract as `request_consent`.
    """
    form = form or ConsentCheckbox()
    prompt = _resolve_without_elicit(ctx, message, form, request_key)
    if prompt is not None:
        return prompt
    schema = form.requested_schema(_protocol_version(ctx))
    try:
        result = await _elicit(ctx, message, schema)
    except (ToolError, MCPError):
        return True
    return _is_approved(form, result)


def _resolve_without_elicit(
    ctx: Context,
    message: str,
    form: ConsentForm,
    request_key: str,
) -> bool | InputRequiredResult | None:
    """Resolve consent without a server-initiated request, or return `None` to elicit."""
    if not isinstance(ctx, Context) or ctx.request_context is None:
        return True

    protocol_version = ctx.request_context.protocol_version
    if protocol_version >= _MRTR_MIN_PROTOCOL_VERSION:
        responses = ctx.input_responses
        response = responses.get(request_key) if responses else None
        if isinstance(response, ElicitResult):
            return _is_approved(form, response)
        if not _client_declares_elicitation(ctx):
            return True
        return InputRequiredResult(
            input_requests={
                request_key: ElicitRequest(
                    params=ElicitRequestFormParams(
                        message=message,
                        requested_schema=form.requested_schema(protocol_version),
                    ),
                ),
            },
        )

    if ctx.is_background_task or not _client_declares_elicitation(ctx):
        return True
    return None


async def _elicit(
    ctx: Context, message: str, requested_schema: dict[str, object]
) -> ElicitResult:
    """Send a server-initiated `elicitation/create` request."""
    return await ctx.session.elicit(
        message=message,
        requested_schema=requested_schema,
        related_request_id=ctx.request_id,
    )


def _protocol_version(ctx: Context) -> str:
    """Return the negotiated protocol version of the current request."""
    request_context = ctx.request_context
    assert request_context is not None
    return request_context.protocol_version


def _client_declares_elicitation(ctx: Context) -> bool:
    """Return whether the client declared the elicitation capability."""
    capabilities = ctx.session.client_capabilities
    return capabilities is not None and capabilities.elicitation is not None


def _object_schema(name: str, field: dict[str, object]) -> dict[str, object]:
    """Return a single-field object schema."""
    return {"type": "object", "properties": {name: field}, "required": [name]}


def _is_approved(form: ConsentForm, result: ElicitResult) -> bool:
    """Return whether an elicitation result approves the actions."""
    return result.action == "accept" and form.is_approved(result.content or {})
