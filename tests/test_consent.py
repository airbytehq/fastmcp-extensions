# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for best-effort, fail-open consent prompts."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.client.elicitation import ElicitResult
from mcp.types import InputRequiredResult

from fastmcp_extensions import request_consent, request_consent_async

MESSAGE = "Permanently delete 'delete-me'?"


def _build_app(*, use_async: bool, **consent_kwargs: Any) -> FastMCP:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param({"confirm": True}, "deleted", id="checked"),
        pytest.param({"confirm": False}, "not confirmed", id="unchecked"),
        pytest.param(ElicitResult(action="decline"), "not confirmed", id="decline"),
        pytest.param(ElicitResult(action="cancel"), "not confirmed", id="cancel"),
    ],
)
async def test_consent_prompt_respects_user_answer(
    use_async: bool, mode: str, answer: dict[str, Any] | ElicitResult, expected: str
) -> None:
    prompts: list[tuple[str, Any]] = []

    result = await _call(
        _build_app(use_async=use_async),
        mode,
        elicitation_handler=_handler(answer, prompts),
    )

    assert [message for message, _ in prompts] == [MESSAGE]
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
async def test_consent_field_title_is_shown_to_the_client(mode: str) -> None:
    prompts: list[tuple[str, Any]] = []
    app = _build_app(
        use_async=False, field_title="Confirm permanent deletion", request_key="rm"
    )

    await _call(app, mode, elicitation_handler=_handler({"confirm": True}, prompts))

    [(_, requested_schema)] = prompts
    assert requested_schema["properties"]["confirm"]["title"] == (
        "Confirm permanent deletion"
    )


@pytest.mark.asyncio
async def test_consent_fails_open_without_request_context() -> None:
    ctx = Context(FastMCP("consent-test"))

    assert request_consent(ctx, MESSAGE) is True
    assert await request_consent_async(ctx, MESSAGE) is True
