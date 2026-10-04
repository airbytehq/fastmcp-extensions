# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""End-to-end tests for OpenTelemetry tool-call tracing.

Span assertions read `capture_tool_spans()`, so they see spans as they would
be exported, after the privacy boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import httpx
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.tools import ToolResult
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracerProvider, ProxyTracerProvider, StatusCode

from fastmcp_extensions import (
    TelemetryConfig,
    TraceArg,
    TracingConfig,
    _tracing,
    add_trace_attributes,
    capture_tool_spans,
    mcp_server,
    mcp_tool,
    register_mcp_tools,
    trace_plan,
)
from fastmcp_extensions._arg_trace import is_arg_key
from fastmcp_extensions._middleware import ToolFilterMiddleware
from fastmcp_extensions._telemetry import resolve_version
from fastmcp_extensions._telemetry_middleware import ToolCallTelemetryMiddleware
from fastmcp_extensions._tracing import (
    INTENT_SENTENCE,
    ToolCallTracingMiddleware,
    bound_value,
    register_tool_call_tracing,
)
from fastmcp_extensions.decorators import _REGISTERED_TOOLS
from fastmcp_extensions.tool_traits import ToolTraits, set_tool_traits

P = "fastmcp_extensions"
LOGGER = "fastmcp_extensions._tracing"
CANARY = "canary-9f3e"
ADD = {"a": 1, "b": 2}
CONSOLE = {"exporter": "console"}

# Exported only for a call that passes arguments and returns a result.
RESULT_KEYS = {
    *(f"{P}.result.{key}" for key in ("content_count", "content_types")),
    *(f"{P}.result.{key}" for key in ("structured_chars", "text_chars")),
    *(f"{P}.{key}" for key in ("args.supplied", "arg.a", "arg.b")),
}
# Exported only for a failed call.
ERROR_KEYS = {
    *(f"{P}.{key}" for key in ("error_type", "error.category", "error.fault")),
    "error.type",
}
# The exact attribute keys exported for a successful call. A new upstream
# attribute must not leak, so every addition here is deliberate.
CORE_KEYS = {
    *RESULT_KEYS,
    f"{P}.tool.fingerprint",
    *(f"{P}.{key}" for key in ("client_name", "client_version", "outcome", "root")),
    *(f"{P}.{key}" for key in ("mcp_protocol_version", "process.uptime_s")),
    *(f"{P}.{key}" for key in ("tool_destructive", "tool_mutating")),
    *(f"{P}.{key}" for key in ("arg_hash_status", "arg_key_scope")),
    *("fastmcp.server.name", "mcp.method.name", "mcp.protocol.version"),
    *("gen_ai.operation.name", "gen_ai.tool.call.id", "gen_ai.tool.name"),
    "jsonrpc.request.id",
}


@pytest.fixture(autouse=True)
def _no_export_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)


def _app(**config: Any) -> FastMCP:
    app = mcp_server(
        "t",
        telemetry=TelemetryConfig(tool_tracing=TracingConfig(**config)),
        tool_filters=[lambda tool, _app: tool.name != "hidden"],
    )

    @app.tool(annotations={"readOnlyHint": True})
    def add(a: int, b: int) -> int:
        # Written past the package API; none of it may be exported.
        rogue = {"leaky": CANARY, "gen_ai.tool.name": CANARY, f"{P}.x {CANARY}": 1}
        trace.get_current_span().set_attributes(rogue)
        return a + b

    @app.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    def boom(note: str = "") -> int:
        trace.get_current_span().set_attribute(f"{P}.intent", CANARY)
        raise ValueError(CANARY)

    @app.tool
    def soft_fail() -> ToolResult:
        trace.get_current_span().set_attribute("gen_ai.tool.call.id", CANARY)
        return ToolResult(content=CANARY, is_error=True)

    @app.tool
    async def slow() -> int:
        await asyncio.sleep(5)
        return 1

    @app.tool
    def hidden() -> int:
        return 1

    @app.tool
    async def outer() -> int:
        await app.call_tool("add", ADD)
        with contextlib.suppress(Exception):
            await app.call_tool("boom", {})
        return 0

    @app.tool
    async def top(ctx: Context) -> int:
        # FastMCP re-stamps the request span for an in-process call, and marks
        # it failed here; the exported span must still describe `top`.
        with contextlib.suppress(Exception):
            await ctx.read_resource("resource://missing")
        await app.call_tool("private", {})
        return 0

    @app.tool
    async def private() -> int:
        await app.call_tool("add", ADD)
        return 0

    set_tool_traits(app, "private", ToolTraits(tracing=False))
    return app


async def _spans(
    app: FastMCP, name: str, arguments: dict[str, Any] | None = None, how: str = ""
) -> list[ReadableSpan]:
    """Call one tool and return the exported `tools/call` spans, in export order."""
    with capture_tool_spans() as spans:
        if how == "direct":
            await app.call_tool(name, arguments or {})
        else:
            async with Client(app) as client:
                call = client.call_tool(name, arguments or {}, raise_on_error=False)
                # Unknown tools, filter rejections, and the cancel timeout
                # raise client-side; the span is what is under test.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(call, 0.2 if how == "cancel" else None)
    return [span for span in spans if span.name.startswith("tools/call")]


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


# id -> (tool, arguments, "span outcome error-type category" per exported span).
# The last span is the requested call; the ones before it are nested calls.
OUTCOME_CASES: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "success": ("add", ADD, ["add success"]),
    "raise": ("boom", {}, ["boom exception ValueError internal"]),
    "returned": ("soft_fail", {}, ["soft_fail tool_error ToolError tool_error"]),
    "filtered": ("hidden", {}, ["hidden exception ValueError internal"]),
    "unknown": ("nope", {}, ["? unknown_tool NotFoundError unknown_tool"]),
    "invalid": (
        "add",
        {"a": "x"},
        ["add exception ValidationError invalid_arguments"],
    ),
    "cancel": ("slow", {}, ["slow cancelled CancelledError cancelled"]),
    "nested": (
        "outer",
        {},
        ["add success", "boom exception ValueError internal", "outer success"],
    ),
    "direct": ("add", ADD, ["add success"]),
    # `top` calls the opted-out `private`, which calls `add`.
    "through-opted-out": ("top", {}, ["add success", "top success"]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", OUTCOME_CASES)
async def test_outcome_matrix(case: str) -> None:
    tool, arguments, expected = OUTCOME_CASES[case]
    spans = await _spans(_app(), tool, arguments, how=case)
    actual = []
    for span in spans:
        attrs = _attrs(span)
        outcome, error_type = attrs[f"{P}.outcome"], attrs.get(f"{P}.error_type")
        name = span.name.removeprefix("tools/call").strip() or "?"
        fields = [name, outcome, error_type, attrs.get(f"{P}.error.category")]
        actual.append(" ".join(filter(None, fields)))
        assert attrs.get("error.type") == error_type
        assert attrs["mcp.method.name"] == "tools/call"
        assert (span.status.status_code is StatusCode.ERROR) == (outcome != "success")
        assert attrs[f"{P}.root"] is (span is spans[-1])
        assert (span.parent is None) == attrs[f"{P}.root"]
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected"),
    [("add", CORE_KEYS), ("boom", CORE_KEYS - RESULT_KEYS | ERROR_KEYS)],
)
async def test_exported_key_set_is_exact(tool: str, expected: set[str]) -> None:
    (span,) = await _spans(_app(), tool, ADD if tool == "add" else {})
    assert set(_attrs(span)) == expected


@pytest.mark.asyncio
async def test_tools_list_span_is_exported() -> None:
    app = _app()

    @app.resource("data://tools")
    async def tool_count() -> str:
        # FastMCP renames this request's span to `tools/list`; it is not one.
        return str(len(await app.list_tools()))

    with capture_tool_spans() as spans:
        async with Client(app) as client:
            await client.list_tools()
            await client.read_resource("data://tools")
    (span,) = [span for span in spans if span.name == "tools/list"]
    attrs = _attrs(span)
    assert set(attrs) == {
        *(f"{P}.{key}" for key in ("client_name", "client_version")),
        *(f"{P}.{key}" for key in ("mcp_protocol_version", "process.uptime_s")),
        *(f"{P}.tools.{key}" for key in ("count", "schema_chars", "set_fingerprint")),
        *("fastmcp.server.name", "mcp.method.name", "mcp.protocol.version"),
    }
    assert attrs[f"{P}.tools.count"] == 7  # `hidden` is filtered out
    assert span.parent is None


@pytest.mark.asyncio
async def test_canary_is_never_exported() -> None:
    """Arguments, results, exception messages, and rogue attributes stay inside."""
    app = _app()
    spans = [
        *await _spans(app, "add", ADD),  # rogue span attribute
        *await _spans(app, "boom", {"note": CANARY}),  # argument, exception message
        *await _spans(app, "soft_fail"),  # returned error text
        *await _spans(app, f"Nope {CANARY}"),  # unknown tool name
    ]
    assert len(spans) == 4
    exported = [
        (span.name, _attrs(span), span.events, span.status.description)
        for span in spans
    ]
    assert CANARY not in repr(exported)
    assert _attrs(spans[3])[f"{P}.tool_requested_name"] == "<other>"
    (event,) = spans[1].events
    assert (event.name, event.attributes) == (
        "exception",
        {"exception.type": "ValueError"},
    )


@pytest.mark.asyncio
async def test_http_session_digest_and_untrusted_trace_context() -> None:
    http_app = _app().http_app(stateless_http=True)
    headers = {
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
        "mcp-session-id": "raw-session",
        "x-mcp-eval-run": "run-1",
        "x-mcp-eval-case": "not a valid case id",
    }
    # Not sampled: a client must not be able to switch tracing off.
    meta = {
        "traceparent": f"00-{'ab' * 16}-{'cd' * 8}-00",
        "tracestate": f"vendor={CANARY}",
    }
    params = {"name": "outer", "arguments": {}, "_meta": meta}
    body = {"jsonrpc": "2.0", "id": "req-9", "method": "tools/call", "params": params}
    transport = httpx.ASGITransport(app=http_app)
    with capture_tool_spans() as spans:
        async with http_app.router.lifespan_context(http_app), httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200
    *nested, span = spans
    attrs = _attrs(span)
    assert span.parent is None
    assert len(span.context.trace_state) == 0
    assert [len(child.parent.trace_state) for child in nested if child.parent] == [0, 0]
    assert attrs["jsonrpc.request.id"] == "req-9"
    assert attrs[f"{P}.eval.run_id"] == "run-1"
    assert f"{P}.eval.case_id" not in attrs
    for key in (f"{P}.session_id", "mcp.session.id", "gen_ai.conversation.id"):
        assert attrs[key] == hashlib.sha256(b"raw-session").hexdigest()


TOKEN = SimpleNamespace(claims={"iss": "https://issuer.example", "sub": "u1"})


@pytest.mark.asyncio
async def test_argument_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records are hashed for a verified principal, and re-checked at export."""
    monkeypatch.setattr(_tracing, "get_access_token", lambda: TOKEN)
    app = _app(arg_key=bytes(range(32)))
    assert "arg_key" not in repr(TracingConfig(arg_key=bytes(range(32))))

    @app.tool
    def search(
        query: str, mode: Literal["fast", "full"], note: Annotated[str, TraceArg.OMIT]
    ) -> None:
        # A record written past the engine is dropped at export and counted.
        forged = json.dumps({"value": CANARY})
        trace.get_current_span().set_attribute(f"{P}.arg.mode", forged)

    arguments = {"query": CANARY, "mode": "full", "note": CANARY}
    (span,) = await _spans(app, "search", arguments)
    attrs = _attrs(span)
    family = {k[len(P) + 1 :]: v for k, v in attrs.items() if is_arg_key(P, k)}
    assert list(json.loads(family.pop("arg.query"))) == ["eq"]
    assert family.pop("arg_scope_id")
    assert family == {
        "arg_hash_status": "ok",
        "arg_key_scope": "approximate",
        "arg_trace_dropped": 1,
    }
    assert CANARY not in repr(attrs)


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"iss": "https://i", "sub": "u"}, "https://i\x00u"),
        ({"sub": "u"}, "\x00\x00u"),
        ({"sub": "u", "client_id": "c"}, "\x00c\x00u"),
        ({"azp": "c"}, "\x00c"),
        # `token.client_id` alone can be a placeholder shared by every caller.
        ({"iss": "https://i"}, None),
        ({"iss": "https://i", "sub": "u\x00v"}, None),
        (None, None),
    ],
    ids=["iss-sub", "sub", "client-sub", "client", "no-identity", "nul", "no-token"],
)
def test_verified_principal(
    claims: dict[str, str] | None, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = claims and SimpleNamespace(claims=claims, client_id="unknown")
    monkeypatch.setattr(_tracing, "get_access_token", lambda: token)
    assert _tracing._verified_principal() == expected


# id -> (config, the one log line, without its `tracing: ` prefix)
SETUP_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "dormant": ({}, "dormant (no OTLP endpoint set)"),
    "opted-out": (CONSOLE, "disabled (DO_NOT_TRACK is set)"),
    "no-sdk": (
        CONSOLE,
        "disabled (OpenTelemetry SDK not installed; install `fastmcp-extensions[otel]`)",
    ),
    "foreign": (CONSOLE, "disabled (a non-SDK global TracerProvider is installed)"),
    "bad-exporter": (
        {"exporter": "zipkin"},
        "disabled (exporter must be 'otlp', 'console', or a SpanExporter)",
    ),
    "bad-prefix": (
        {"attribute_prefix": "Bad Prefix"},
        "disabled (invalid attribute_prefix 'Bad Prefix')",
    ),
    "missing-prefix": (
        {"attribute_prefix": None},
        "disabled (invalid attribute_prefix None)",
    ),
    # Would export FastMCP's own `mcp.*` attributes, raw session id included.
    "reserved-prefix": (
        {"attribute_prefix": "mcp.orders"},
        "disabled (invalid attribute_prefix 'mcp.orders')",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", SETUP_CASES)
async def test_setup_logs_one_line_and_never_raises(
    case: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config, message = SETUP_CASES[case]
    if case == "opted-out":
        monkeypatch.setenv("DO_NOT_TRACK", "1")
    elif case == "no-sdk":
        monkeypatch.setitem(sys.modules, "fastmcp_extensions._tracing_sdk", None)
    elif case == "foreign":
        monkeypatch.setattr(trace, "get_tracer_provider", NoOpTracerProvider)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        app = _app(**config)
    logged = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert logged == [f"tracing: {message}"]
    async with Client(app) as client:
        assert (await client.call_tool("add", ADD)).data == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("opted_out", [False, True])
async def test_existing_sdk_provider(
    opted_out: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The layer attaches to a host's provider, and leaves it alone when disabled."""
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    if opted_out:
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        provider.add_span_processor(SimpleSpanProcessor(exporter))  # the host's own
    hooked: list[int] = []
    with caplog.at_level(logging.INFO, logger=LOGGER):
        app = _app(exporter=exporter, attributes=lambda: hooked.append(1) or {})
    assert opted_out or "tracing: exporter=custom provider=existing" in caplog.messages
    async with Client(app) as client:
        await client.call_tool("add", ADD)
    provider.shutdown()  # flushes the batch processor
    spans = exporter.get_finished_spans()
    assert bool(hooked) != opted_out
    if opted_out:
        stamped = {key for span in spans for key in _attrs(span)}
        assert not stamped & {_tracing.MARK, f"{P}.root", f"{P}.outcome"}
    else:
        # The client also lists tools, and that span is exported too.
        exported = {span.name: _attrs(span) for span in spans}
        assert set(exported) <= {"tools/call add", "tools/list"}
        assert set(exported["tools/call add"]) == CORE_KEYS


def test_default_otlp_export_and_resource(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    providers: list[Any] = [ProxyTracerProvider()]
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: providers[-1])
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        mcp_server(
            "orders-mcp",
            package_name="fastmcp",
            telemetry=TelemetryConfig(tool_tracing=True),
        )
    assert "tracing: exporter=otlp provider=created" in caplog.messages
    resource = providers[-1].resource.attributes
    providers[-1].shutdown()
    assert resource["service.name"] == "orders-mcp"
    assert resource["service.version"] == resolve_version("fastmcp")
    assert resource["service.instance.id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", [(_tracing, "_client_info"), (ToolCallTracingMiddleware, "_before")]
)
async def test_failing_attribute_source_keeps_the_span(
    target: tuple[object, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted session token can make the client lookup raise, for example."""
    monkeypatch.setattr(*target, lambda *_: 1 / 0)
    (span,) = await _spans(_app(), "add", ADD)
    attrs = _attrs(span)
    assert attrs[f"{P}.root"] is True and attrs[f"{P}.outcome"] == "success"


async def _late() -> dict[str, object]:
    return {"region": "us", "outcome": "forged"}


def _raises() -> dict[str, object]:
    raise RuntimeError(CANARY)


async def _leaks_cancellation() -> dict[str, object]:
    raise asyncio.CancelledError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        pytest.param(
            {"plan": "pro", "root": False, "note": "x" * 300},
            {"plan": "pro", "note": "x" * 256},
            id="mapping",
        ),
        pytest.param(
            lambda: {"plan": "pro", "tool.x": 1, "Bad Key": 1, "skip": None},
            {"plan": "pro"},
            id="callable",
        ),
        pytest.param(_late, {"region": "us"}, id="async"),
        pytest.param(_raises, {}, id="raising"),
        # The tool call itself was not cancelled, so it still succeeds.
        pytest.param(_leaks_cancellation, {}, id="leaks-cancel"),
    ],
)
async def test_hook_attributes_are_bounded_and_cannot_touch_owned_keys(
    attributes: Any, expected: dict[str, object]
) -> None:
    app = _app(attribute_prefix="acme.mcp", attributes=attributes)
    (span,) = await _spans(app, "add", ADD)
    attrs = _attrs(span)
    core = {key.replace(P, "acme.mcp") for key in CORE_KEYS}
    hooked = {k.removeprefix("acme.mcp."): v for k, v in attrs.items() if k not in core}
    assert hooked == expected
    assert (attrs["acme.mcp.root"], attrs["acme.mcp.outcome"]) == (True, "success")


@pytest.mark.asyncio
async def test_event_and_span_describe_the_same_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    token = SimpleNamespace(claims={"sub": "alice"}, client_id=None)
    monkeypatch.setattr(
        "fastmcp_extensions._attribution.get_access_token", lambda: token
    )
    app = mcp_server(
        "t",
        telemetry=TelemetryConfig(
            anonymization_salt="salt",
            extra_properties=lambda: {"workspace_id": "w1", "user_id": CANARY},
            tool_tracing=TracingConfig(shared_properties=("workspace_id",)),
        ),
    )

    @app.tool
    def add(a: int, b: int) -> int:
        return a + b

    @app.tool
    async def slow() -> None:
        await asyncio.sleep(5)

    with caplog.at_level(logging.INFO, logger="fastmcp_extensions._telemetry"):
        (ok,) = await _spans(app, "add", ADD)
        (cancelled,) = await _spans(app, "slow", how="cancel")
    events = [r.telemetry for r in caplog.records if hasattr(r, "telemetry")]
    for span, event in zip((ok, cancelled), events, strict=True):
        attrs = _attrs(span)
        # The event carries its span's identifiers, caller, and outcome.
        assert event["trace_id"] == format(span.context.trace_id, "032x")
        assert event["span_id"] == format(span.context.span_id, "016x")
        assert event["caller_hash"] == attrs[f"{P}.caller_hash"]
        assert attrs[f"{P}.caller_id_type"] == "subject"
        assert event["success"] == (attrs[f"{P}.outcome"] == "success")
        assert event["error_type"] == attrs.get("error.type")
        # A property is set once and reaches the span only when it is named.
        assert (event["workspace_id"], attrs[f"{P}.workspace_id"]) == ("w1", "w1")
        assert event["user_id"] == CANARY
        assert f"{P}.user_id" not in attrs
    assert [event["success"] for event in events] == [True, False]


@pytest.mark.asyncio
async def test_failure_wiring_from_config_to_span() -> None:
    config = TracingConfig(
        error_classifier=lambda exc: (
            "rate_limited" if isinstance(exc, KeyError) else None
        ),
        attributes=lambda: {"late": "yes"},
    )
    app = mcp_server(
        "t",
        user_facing_errors=[ValueError],
        telemetry=TelemetryConfig(tool_tracing=config),
    )

    @app.tool
    def fail(error: Literal["ValueError", "KeyError"]) -> None:
        raise {"ValueError": ValueError, "KeyError": KeyError}[error]

    for error, category in (("ValueError", "user_error"), ("KeyError", "rate_limited")):
        (span,) = await _spans(app, "fail", {"error": error})
        attrs = _attrs(span)
        assert attrs[f"{P}.error.category"] == category
        assert (attrs["error.type"], attrs[f"{P}.late"]) == (error, "yes")


@pytest.mark.asyncio
async def test_span_survives_the_sdk_attribute_limit() -> None:
    app = _app(attributes=lambda: {f"late_{n}": n for n in range(200)})

    @app.tool
    def chatty() -> None:
        add_trace_attributes({f"row_{n}": n for n in range(200)})

    (span,) = await _spans(app, "chatty")
    assert (span.name, span.parent) == ("tools/call chatty", None)
    assert _attrs(span)[f"{P}.outcome"] == "success"


@pytest.mark.asyncio
async def test_add_trace_attributes_targets_the_innermost_call() -> None:
    app = _app()

    @app.tool
    def inner() -> int:
        add_trace_attributes({"where": "inner", "outcome": "forged"})
        return 1

    @app.tool
    async def wrapper() -> int:
        await app.call_tool("inner", {})
        add_trace_attributes({"where": "wrapper"})
        return 1

    add_trace_attributes({"where": "nowhere"})  # a no-op outside a traced call
    spans = await _spans(app, "wrapper")
    assert [
        (span.name, _attrs(span)[f"{P}.where"], _attrs(span)[f"{P}.outcome"])
        for span in spans
    ] == [
        ("tools/call inner", "inner", "success"),
        ("tools/call wrapper", "wrapper", "success"),
    ]


@pytest.mark.parametrize(
    ("value", "allow_list", "expected"),
    [
        (2**63, False, None),
        (float("nan"), False, None),
        ("a\nb", False, None),
        (["a"], False, None),
        (["a", 1, " b ", "c\nd"], True, ("a", "b")),
        ([str(n) for n in range(20)], True, tuple(str(n) for n in range(16))),
    ],
)
def test_bound_value(value: object, allow_list: bool, expected: object) -> None:
    assert bound_value(value, allow_list=allow_list) == expected


@pytest.fixture
def _isolated_tools() -> Iterator[None]:
    registered = list(_REGISTERED_TOOLS)
    _REGISTERED_TOOLS.clear()
    yield
    _REGISTERED_TOOLS[:] = registered


@pytest.mark.asyncio
@pytest.mark.usefixtures("_isolated_tools")
async def test_per_tool_tracing_option() -> None:
    app = _app()

    @mcp_tool(read_only=True, tracing=False)
    async def untraced(secret: str) -> str:
        await app.call_tool("add", ADD)
        await app.call_tool("add", ADD)
        return secret

    @mcp_tool(destructive=True, tracing=lambda args: {"kind": args["kind"], "root": 0})
    def derived(kind: str) -> str:
        return kind

    register_mcp_tools(app, mcp_module=__name__)

    # The opted-out tool exports nothing, and its nested calls neither adopt
    # the request span nor dangle from it.
    spans = await _spans(app, "untraced", {"secret": CANARY})
    assert [(span.name, _attrs(span)[f"{P}.root"], span.parent) for span in spans] == [
        ("tools/call add", True, None)
    ] * 2

    (span,) = await _spans(app, "derived", {"kind": "source"})
    attrs = _attrs(span)
    assert set(attrs) == CORE_KEYS - {f"{P}.arg.a", f"{P}.arg.b"} | {
        *(f"{P}.{key}" for key in ("kind", "tool_module", "arg.kind"))
    }
    assert (attrs[f"{P}.kind"], attrs[f"{P}.root"]) == ("source", True)
    assert attrs[f"{P}.tool_module"] == "test_tracing"
    assert (attrs[f"{P}.tool_mutating"], attrs[f"{P}.tool_destructive"]) == (True, True)

    with pytest.raises(TypeError, match="tracing must be a bool or a callable"):
        mcp_tool(tracing="yes")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_isolated_tools")
async def test_trace_plan() -> None:
    config = TracingConfig(arg_default=TraceArg.FINGERPRINT, capture_intent=True)
    app = mcp_server("plan", telemetry=TelemetryConfig(tool_tracing=config))

    @app.tool
    def search(
        ctx: Context,
        query: str,
        note: Annotated[str, TraceArg.OMIT],
        mode: Literal["a", "b"] = "a",
        intent: str = "",
    ) -> None: ...

    @mcp_tool(read_only=True)
    def fetch(
        url: str, limit: Annotated[int, TraceArg.VALUE] = 5, api_key: str = ""
    ) -> None: ...

    @mcp_tool(tracing=False)
    def untraced(secret: str) -> None: ...

    register_mcp_tools(app, mcp_module=__name__, exclude_args=["api_key"])
    plan = await trace_plan(app)
    assert list(plan) == ["fetch", "search", "untraced"]
    assert {name: entry["args"] for name, entry in plan.items()} == {
        "fetch": {"limit": "value int", "url": "fingerprint"},
        "search": {
            "mode": 'value allowed=["a","b"]',
            "note": "omit",
            "query": "fingerprint",
        },
        "untraced": {},
    }
    assert set(plan["fetch"]) == {"args", "fingerprint", "schema_chars"}
    # A marker is not a description, so it must not reach the published schema.
    schema = (await app.get_tool("search")).parameters["properties"]["note"]
    assert schema == {"type": "string"}
    # An app without tracing gets a plan with the default configuration.
    plain = FastMCP("plain")
    plain.tool(fetch)
    assert (await trace_plan(plain))["fetch"]["args"]["url"] == "hash"


@pytest.mark.asyncio
async def test_intent_capture() -> None:
    app = _app(capture_intent=True)
    register_tool_call_tracing(app, TracingConfig(capture_intent=True))
    assert app.instructions == INTENT_SENTENCE

    @app.tool
    def wants_intent(intent: str = "") -> str:
        return intent

    async with Client(app) as client:
        schema = {tool.name: tool.input_schema for tool in await client.list_tools()}
        assert schema["add"]["properties"]["intent"]["type"] == "string"
        assert schema["add"]["required"] == ["a", "b"]
        assert schema["wants_intent"]["properties"]["intent"]["default"] == ""
        declared = await client.call_tool("wants_intent", {"intent": "kept"})
        assert declared.data == "kept"
        # Export is dormant here, and `intent` is still advertised and stripped.
        assert (await client.call_tool("add", {**ADD, "intent": "why"})).data == 3

    # `add` does not declare `intent`, so the call succeeds only if it is stripped.
    (span,) = await _spans(app, "add", {**ADD, "intent": "  " + "x" * 5000})
    attrs = _attrs(span)
    assert (attrs[f"{P}.outcome"], attrs[f"{P}.intent_present"]) == ("success", True)
    assert len(attrs[f"{P}.intent"]) == 4096
    assert attrs[f"{P}.intent"].endswith("...[truncated]")

    (span,) = await _spans(app, "add", ADD)
    assert _attrs(span)[f"{P}.intent_present"] is False
    assert f"{P}.intent" not in _attrs(span)

    # A tool's own `intent` parameter is the tool's data, not the agent's reason.
    (span,) = await _spans(app, "wants_intent", {"intent": CANARY})
    assert CANARY not in repr(_attrs(span))


def test_registration_position_and_idempotence() -> None:
    kinds = (
        ToolCallTelemetryMiddleware,
        ToolCallTracingMiddleware,
        ToolFilterMiddleware,
    )

    def order(app: FastMCP) -> list[type]:
        return [type(m) for m in app.middleware if isinstance(m, kinds)]

    # Registered late, tracing still lands inside telemetry and outside filters.
    late = mcp_server("late", tool_filters=[lambda _tool, _app: True])
    for _ in range(2):
        register_tool_call_tracing(late, TracingConfig())
    assert order(_app()) == order(late) == list(kinds)

    off = mcp_server("off")  # tracing is off unless the telemetry config sets it
    assert order(off) == [ToolCallTelemetryMiddleware]

    # `enabled=False` is the master switch: no telemetry and no tracing.
    disabled = mcp_server(
        "x", telemetry=TelemetryConfig(enabled=False, tool_tracing=True)
    )
    assert order(disabled) == []

    plain = FastMCP("plain")
    register_tool_call_tracing(plain, TracingConfig())
    assert order(plain) == [ToolCallTracingMiddleware]


def test_other_spans_keeps_only_what_the_hook_returns() -> None:
    def keep_get(span: ReadableSpan) -> dict[str, object] | None:
        return {"http.method": "GET"} if span.name == "GET" else None

    app = _app(other_spans=keep_get)  # spans reach a hook only while its app is alive
    with capture_tool_spans() as spans:
        for name in ("GET", "POST"):
            with trace.get_tracer("test").start_as_current_span(name) as span:
                span.set_attribute("http.url", CANARY)
                span.add_event("leaky", {"detail": CANARY})
    assert app is not None
    assert [(span.name, _attrs(span), span.events) for span in spans] == [
        ("GET", {"http.method": "GET"}, ())
    ]
