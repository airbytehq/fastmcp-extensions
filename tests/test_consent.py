# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for best-effort, fail-open approval and consent prompts."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.elicitation import ElicitResult
from mcp.types import InputRequiredResult

from fastmcp_extensions import (
    ConsentCheckbox,
    ConsentChoice,
    ConsentForm,
    format_approval_message,
    format_not_approved_message,
    request_approval,
    request_approval_async,
    request_consent,
    request_consent_async,
)

MESSAGE = "Permanently delete 'delete-me'?"
ACTIONS = ["Permanently delete source 'delete-me'"]
NOTES = "This cannot be undone."

CHECKBOX = ConsentCheckbox(label="Yes, permanently delete it")
CHOICE = ConsentChoice(approve_label="Delete permanently", reject_label="Keep it")


def _build_consent_app(*, use_async: bool, **consent_kwargs: Any) -> FastMCP:
    app = FastMCP("consent-test")

    if use_async:

        @app.tool
        async def delete_thing(ctx: Context) -> str | InputRequiredResult:
            consent = await request_consent_async(ctx, MESSAGE, **consent_kwargs)
            if isinstance(consent, InputRequiredResult):
                return consent
            return "deleted" if consent else "not confirmed"

    else:

        @app.tool
        def delete_thing(ctx: Context) -> str | InputRequiredResult:
            consent = request_consent(ctx, MESSAGE, **consent_kwargs)
            if isinstance(consent, InputRequiredResult):
                return consent
            return "deleted" if consent else "not confirmed"

    return app


def _build_approval_app(*, use_async: bool) -> FastMCP:
    app = FastMCP("approval-test")

    if use_async:

        @app.tool
        async def delete_thing(ctx: Context) -> str | InputRequiredResult:
            approval = await request_approval_async(ctx, ACTIONS, NOTES)
            if isinstance(approval, InputRequiredResult):
                return approval
            return "deleted" if approval else format_not_approved_message(ACTIONS)

    else:

        @app.tool
        def delete_thing(ctx: Context) -> str | InputRequiredResult:
            approval = request_approval(ctx, ACTIONS, NOTES)
            if isinstance(approval, InputRequiredResult):
                return approval
            return "deleted" if approval else format_not_approved_message(ACTIONS)

    return app


async def _call(app: FastMCP, mode: str, **client_kwargs: Any) -> str:
    async with Client(app, mode=mode, **client_kwargs) as client:
        result = await client.call_tool("delete_thing", {})
    return result.content[0].text


def _handler(answer: dict[str, Any] | ElicitResult, prompts: list[tuple[str, Any]]):
    async def handler(
        message: str, _response_type: Any, params: Any, *_: Any
    ) -> dict[str, Any] | ElicitResult:
        prompts.append((message, params.requested_schema))
        return answer

    return handler


FORM_ANSWERS = [
    pytest.param(None, {"approve": True}, {"approve": False}, id="default-checkbox"),
    pytest.param(CHECKBOX, {"approve": True}, {"approve": False}, id="checkbox"),
    pytest.param(
        CHOICE,
        {"decision": "Delete permanently"},
        {"decision": "Keep it"},
        id="choice",
    ),
]

ANSWER_KINDS = [
    pytest.param("approve", True, id="approve"),
    pytest.param("reject", False, id="reject"),
    pytest.param("decline", False, id="decline"),
    pytest.param("cancel", False, id="cancel"),
]


def _answers(
    approve: dict[str, Any], reject: dict[str, Any]
) -> dict[str, dict[str, Any] | ElicitResult]:
    return {
        "approve": approve,
        "reject": reject,
        "decline": ElicitResult(action="decline"),
        "cancel": ElicitResult(action="cancel"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(("form", "approve", "reject"), FORM_ANSWERS)
@pytest.mark.parametrize(("answer_kind", "approved"), ANSWER_KINDS)
async def test_consent_prompt_respects_user_answer(
    use_async: bool,
    mode: str,
    form: ConsentForm | None,
    approve: dict[str, Any],
    reject: dict[str, Any],
    answer_kind: str,
    approved: bool,
) -> None:
    prompts: list[tuple[str, Any]] = []

    result = await _call(
        _build_consent_app(use_async=use_async, form=form),
        mode,
        elicitation_handler=_handler(_answers(approve, reject)[answer_kind], prompts),
    )

    assert [message for message, _ in prompts] == [MESSAGE]
    assert result == ("deleted" if approved else "not confirmed")


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(("answer_kind", "approved"), ANSWER_KINDS)
async def test_approval_prompt_respects_user_answer(
    use_async: bool, mode: str, answer_kind: str, approved: bool
) -> None:
    prompts: list[tuple[str, Any]] = []
    answers = _answers({"approve": True}, {"approve": False})

    result = await _call(
        _build_approval_app(use_async=use_async),
        mode,
        elicitation_handler=_handler(answers[answer_kind], prompts),
    )

    [(message, requested_schema)] = prompts
    assert message == format_approval_message(ACTIONS, NOTES)
    assert requested_schema["properties"]["approve"]["title"] == "Yes, I approve."
    assert result == ("deleted" if approved else format_not_approved_message(ACTIONS))


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_prompts_fail_open_without_elicitation_support(
    use_async: bool, mode: str
) -> None:
    assert await _call(_build_consent_app(use_async=use_async), mode) == "deleted"
    assert await _call(_build_approval_app(use_async=use_async), mode) == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_consent_checkbox_schema_is_sent_to_the_client(mode: str) -> None:
    prompts: list[tuple[str, Any]] = []
    app = _build_consent_app(use_async=False, form=CHECKBOX, request_key="rm")

    await _call(app, mode, elicitation_handler=_handler({"approve": True}, prompts))

    [(_, requested_schema)] = prompts
    assert requested_schema["properties"] == {
        "approve": {
            "type": "boolean",
            "title": "Yes, permanently delete it",
            "description": "Yes, permanently delete it",
            "default": False,
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_consent_choice_schema_is_sent_to_the_client(mode: str) -> None:
    prompts: list[tuple[str, Any]] = []

    await _call(
        _build_consent_app(use_async=False, form=CHOICE),
        mode,
        elicitation_handler=_handler({"decision": "Delete permanently"}, prompts),
    )

    [(_, requested_schema)] = prompts
    assert requested_schema["properties"] == {
        "decision": {
            "type": "string",
            "title": "Decision",
            "oneOf": [
                {"const": "Keep it", "title": "Keep it"},
                {"const": "Delete permanently", "title": "Delete permanently"},
            ],
            "default": "Keep it",
        }
    }


def test_consent_choice_uses_enum_names_before_titled_one_of() -> None:
    field = CHOICE.requested_schema("2025-06-18")["properties"]["decision"]

    assert field["enum"] == ["Keep it", "Delete permanently"]
    assert field["enumNames"] == ["Keep it", "Delete permanently"]
    assert "oneOf" not in field


def test_consent_choice_rejects_identical_labels() -> None:
    with pytest.raises(ValueError, match="must differ"):
        ConsentChoice(approve_label="OK", reject_label="OK")


@pytest.mark.asyncio
async def test_prompts_fail_open_without_request_context() -> None:
    ctx = Context(FastMCP("consent-test"))

    assert request_consent(ctx, MESSAGE) is True
    assert await request_consent_async(ctx, MESSAGE) is True
    assert request_approval(ctx, ACTIONS) is True
    assert await request_approval_async(ctx, ACTIONS) is True


def test_format_approval_message() -> None:
    message = format_approval_message(
        ["Delete source 'a'", "Delete connection 'b'"], "This cannot be undone."
    )

    assert message == (
        "Your agent is attempting to perform the following actions:\n\n"
        "- Delete source 'a'\n"
        "- Delete connection 'b'\n\n"
        "This cannot be undone.\n\n"
        'Do you approve these actions? To approve, check "Yes, I approve." and submit. '
        "To decline, submit without checking it."
    )


def test_format_approval_message_without_notes() -> None:
    assert format_approval_message(["Delete source 'a'"]) == (
        "Your agent is attempting to perform the following actions:\n\n"
        "- Delete source 'a'\n\n"
        'Do you approve these actions? To approve, check "Yes, I approve." and submit. '
        "To decline, submit without checking it."
    )


@pytest.mark.parametrize("actions", [[], "Delete source 'a'"], ids=["empty", "str"])
def test_format_approval_message_rejects_invalid_actions(actions: Any) -> None:
    with pytest.raises(ValueError, match="actions_to_take"):
        format_approval_message(actions)


def test_format_not_approved_message() -> None:
    assert format_not_approved_message(["Delete source 'a'"]) == (
        "The user did not approve these actions, so nothing was done:\n"
        "- Delete source 'a'\n\n"
        "Do not retry them. Ask the user how they would like to proceed instead."
    )
