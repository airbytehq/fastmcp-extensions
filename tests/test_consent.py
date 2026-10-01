# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for best-effort, fail-open consent prompts."""

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
    format_consent_message,
    format_not_approved_message,
    request_consent,
    request_consent_async,
)

ACTIONS = ["Permanently delete source 'delete-me'"]
NOTES = "This cannot be undone."


def _build_app(*, use_async: bool, **consent_kwargs: Any) -> FastMCP:
    app = FastMCP("consent-test")

    if use_async:

        @app.tool
        async def delete_thing(ctx: Context) -> str | InputRequiredResult:
            consent = await request_consent_async(ctx, ACTIONS, NOTES, **consent_kwargs)
            if isinstance(consent, InputRequiredResult):
                return consent
            return "deleted" if consent else "not confirmed"

    else:

        @app.tool
        def delete_thing(ctx: Context) -> str | InputRequiredResult:
            consent = request_consent(ctx, ACTIONS, NOTES, **consent_kwargs)
            if isinstance(consent, InputRequiredResult):
                return consent
            return "deleted" if consent else "not confirmed"

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


CHECKBOX = ConsentCheckbox(label="Yes, permanently delete it")
CHOICE = ConsentChoice(approve_label="Delete permanently", reject_label="Keep it")

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


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(("form", "approve", "reject"), FORM_ANSWERS)
@pytest.mark.parametrize(
    ("answer_kind", "expected"),
    [
        pytest.param("approve", "deleted", id="approve"),
        pytest.param("reject", "not confirmed", id="reject"),
        pytest.param("decline", "not confirmed", id="decline"),
        pytest.param("cancel", "not confirmed", id="cancel"),
    ],
)
async def test_consent_prompt_respects_user_answer(
    use_async: bool,
    mode: str,
    form: ConsentForm | None,
    approve: dict[str, Any],
    reject: dict[str, Any],
    answer_kind: str,
    expected: str,
) -> None:
    answers: dict[str, dict[str, Any] | ElicitResult] = {
        "approve": approve,
        "reject": reject,
        "decline": ElicitResult(action="decline"),
        "cancel": ElicitResult(action="cancel"),
    }
    prompts: list[tuple[str, Any]] = []

    result = await _call(
        _build_app(use_async=use_async, form=form),
        mode,
        elicitation_handler=_handler(answers[answer_kind], prompts),
    )

    expected_message = format_consent_message(ACTIONS, NOTES, form=form)
    assert [message for message, _ in prompts] == [expected_message]
    assert result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_consent_fails_open_without_elicitation_support(
    use_async: bool, mode: str
) -> None:
    assert await _call(_build_app(use_async=use_async), mode) == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_consent_checkbox_schema_is_sent_to_the_client(mode: str) -> None:
    prompts: list[tuple[str, Any]] = []
    app = _build_app(use_async=False, form=CHECKBOX, request_key="rm")

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
        _build_app(use_async=False, form=CHOICE),
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


@pytest.mark.asyncio
async def test_consent_fails_open_without_request_context() -> None:
    ctx = Context(FastMCP("consent-test"))

    assert request_consent(ctx, ACTIONS) is True
    assert await request_consent_async(ctx, ACTIONS) is True


def test_format_consent_message_default_checkbox() -> None:
    message = format_consent_message(
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


def test_format_consent_message_choice_without_notes() -> None:
    message = format_consent_message(["Delete source 'a'"], form=CHOICE)

    assert message == (
        "Your agent is attempting to perform the following actions:\n\n"
        "- Delete source 'a'\n\n"
        'Do you approve these actions? To approve, select "Delete permanently" and '
        'submit. To decline, submit "Keep it".'
    )


@pytest.mark.parametrize("actions", [[], "Delete source 'a'"], ids=["empty", "str"])
def test_format_consent_message_rejects_invalid_actions(actions: Any) -> None:
    with pytest.raises(ValueError, match="actions_to_take"):
        format_consent_message(actions)


def test_consent_choice_rejects_identical_labels() -> None:
    with pytest.raises(ValueError, match="must differ"):
        ConsentChoice(approve_label="OK", reject_label="OK")


def test_format_not_approved_message() -> None:
    assert format_not_approved_message(["Delete source 'a'"]) == (
        "The user did not approve these actions, so nothing was done:\n"
        "- Delete source 'a'\n\n"
        "Do not retry them. Ask the user how they would like to proceed instead."
    )
