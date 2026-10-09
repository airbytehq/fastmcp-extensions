# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""End-to-end tests for OpenTelemetry tool-call tracing.

Span assertions read `capture_tool_spans()`, so they see spans as they would
be exported, after the privacy boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import json
import logging
import sys
import weakref
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import httpx
import pytest
from fastmcp import Client, Context, FastMCP, FastMCPApp
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.addressing import hashed_backend_name
from fastmcp.tools import ToolResult
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    NoOpTracerProvider,
    ProxyTracerProvider,
    SpanKind,
    StatusCode,
)

from fastmcp_extensions import (
    TelemetryConfig,
    ToolTracingConfig,
    TraceArg,
    UserFacingErrorMiddleware,
    add_trace_attributes,
    capture_tool_spans,
    mcp_server,
    mcp_tool,
    register_mcp_tools,
    trace_plan,
)
from fastmcp_extensions._middleware import ToolFilterMiddleware
from fastmcp_extensions._telemetry import resolve_version
from fastmcp_extensions._telemetry_middleware import ToolCallTelemetryMiddleware
from fastmcp_extensions.decorators import _REGISTERED_TOOLS
from fastmcp_extensions.otel import middleware as _tracing
from fastmcp_extensions.otel._arg_digests import is_arg_key
from fastmcp_extensions.otel._sdk import BoundaryExporter
from fastmcp_extensions.otel.middleware import (
    INTENT_SENTENCE,
    ToolCallOtelMiddleware,
    bound_value,
    register_tool_call_tracing,
)
from fastmcp_extensions.tool_traits import ToolTraits, set_tool_traits

P = "fastmcp_extensions"
LOGGER = "fastmcp_extensions.otel.middleware"
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
        telemetry=TelemetryConfig(tool_tracing=ToolTracingConfig(**config)),
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
    "raise": ("boom", {}, ["boom exception ValueError unclassified"]),
    "returned": ("soft_fail", {}, ["soft_fail tool_error ToolError tool_error"]),
    "filtered": (
        "hidden",
        {},
        ["hidden exception ToolUnavailableError tool_unavailable"],
    ),
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
        ["add success", "boom exception ValueError unclassified", "outer success"],
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
        # FastMCP renames this request's span to `tools/list`; it is not one,
        # however many times the handler lists tools.
        await app.list_tools()
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
    assert "arg_key" not in repr(ToolTracingConfig(arg_key=bytes(range(32))))

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
    assert list(json.loads(family.pop("arg.query"))) == ["digest"]
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
        monkeypatch.setitem(sys.modules, "fastmcp_extensions.otel._sdk", None)
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
    assert resource["service.name"] == "orders-mcp"
    assert resource["service.version"] == resolve_version("fastmcp")
    assert resource["service.instance.id"]

    # A second app shares the provider but exports its own service identity.
    exporter = InMemorySpanExporter()
    billing = mcp_server(
        "billing-mcp",
        telemetry=TelemetryConfig(tool_tracing=ToolTracingConfig(exporter=exporter)),
    )
    billing.tool(lambda: "pong", name="ping")
    asyncio.run(billing.call_tool("ping", {}))
    providers[-1].force_flush()
    providers[-1].shutdown()
    (span,) = exporter.get_finished_spans()
    assert span.resource.attributes["service.name"] == "billing-mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", [(_tracing, "_client_info"), (ToolCallOtelMiddleware, "_before")]
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


def _sync_leaks_cancellation() -> dict[str, object]:
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
        pytest.param(_sync_leaks_cancellation, {}, id="sync-leaks-cancel"),
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
async def test_a_slow_async_hook_does_not_hold_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hangs() -> dict[str, object]:
        await asyncio.sleep(60)
        return {"region": "us"}

    monkeypatch.setattr(_tracing, "_HOOK_TIMEOUT_S", 0.05)
    (span,) = await asyncio.wait_for(_spans(_app(attributes=hangs), "add", ADD), 5)
    attrs = _attrs(span)
    assert (attrs[f"{P}.outcome"], f"{P}.region" in attrs) == ("success", False)


@pytest.mark.asyncio
async def test_hashed_name_tool_keeps_its_own_intent() -> None:
    app = _app(capture_intent=True)
    backend = FastMCPApp("dash")

    @backend.tool()
    def save(intent: str = "none") -> str:
        return intent

    app.add_provider(backend)
    name = hashed_backend_name("dash", "save")
    with capture_tool_spans() as spans:
        async with Client(app) as client:
            result = await client.call_tool(name, {"intent": CANARY})
    (span,) = [span for span in spans if span.name.startswith("tools/call")]
    # The tool declares `intent`, so it is the tool's data: kept, not exported.
    assert result.content[0].text == CANARY
    assert span.name == f"tools/call {name}"
    assert CANARY not in json.dumps(_attrs(span), default=str)

    # The hashed name carries the opt-out of the tool it resolves to.
    set_tool_traits(app, "save", ToolTraits(tracing=False))
    assert await _spans(app, name, {"intent": "x"}) == []


class _StatusError(Exception):
    status_code = 503


# event field -> span attribute suffix
ERROR_FACTS = {
    "outcome": "outcome",
    "error_category": "error.category",
    "error_fault": "error.fault",
    "upstream_status_code": "upstream.status_code",
    "error_cause_types": "error.cause_types",
}


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
            # A server `trace_id` must not replace the span's on the event.
            extra_properties=lambda: {
                "workspace_id": "w1",
                "id": CANARY,
                "trace_id": "stale",
            },
            # A bare string is one name, not a set of substrings to match.
            tool_tracing=ToolTracingConfig(shared_properties="workspace_id"),
        ),
    )

    @app.tool
    def add(a: int, b: int) -> int:
        return a + b

    @app.tool
    async def slow() -> None:
        await asyncio.sleep(5)

    @app.tool
    def upstream() -> None:
        raise RuntimeError from _StatusError()

    with caplog.at_level(logging.INFO, logger="fastmcp_extensions._telemetry"):
        (ok,) = await _spans(app, "add", ADD)
        (cancelled,) = await _spans(app, "slow", how="cancel")
        (failed,) = await _spans(app, "upstream")
    events = [r.telemetry for r in caplog.records if hasattr(r, "telemetry")]
    for span, event in zip((ok, cancelled, failed), events, strict=True):
        attrs = _attrs(span)
        # One source: the error facts on the event are the span's.
        for key, attribute in ERROR_FACTS.items():
            assert event.get(key) == attrs.get(f"{P}.{attribute}")
        # The event carries its span's identifiers, caller, and outcome.
        assert event["trace_id"] == format(span.context.trace_id, "032x")
        assert event["span_id"] == format(span.context.span_id, "016x")
        assert event["caller_hash"] == attrs[f"{P}.caller_hash"]
        assert attrs[f"{P}.caller_id_type"] == "subject"
        assert event["success"] == (attrs[f"{P}.outcome"] == "success")
        assert event["error_type"] == attrs.get("error.type")
        # A property is set once and reaches the span only when it is named.
        assert (event["workspace_id"], attrs[f"{P}.workspace_id"]) == ("w1", "w1")
        assert event["id"] == CANARY
        assert f"{P}.id" not in attrs
    assert [event["success"] for event in events] == [True, False, False]
    assert [events[2][key] for key in ERROR_FACTS] == [
        "exception",
        "upstream_error",
        "upstream",
        503,
        ("_StatusError",),
    ]


@pytest.mark.asyncio
async def test_failure_wiring_from_config_to_span(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = ToolTracingConfig(
        error_classifier=lambda exc: (
            "rate_limited" if isinstance(exc, KeyError) else None
        ),
        error_reason=lambda exc: "quota" if isinstance(exc, KeyError) else None,
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
        with caplog.at_level(logging.INFO, logger="fastmcp_extensions._telemetry"):
            (span,) = await _spans(app, "fail", {"error": error})
        attrs = _attrs(span)
        assert attrs[f"{P}.error.category"] == category
        assert (attrs["error.type"], attrs[f"{P}.late"]) == (error, "yes")
        assert attrs.get(f"{P}.error.reason") == (
            "quota" if error == "KeyError" else None
        )
    # The classifier and the reason reach the event too.
    event = caplog.records[-1].telemetry
    assert (event["error_category"], event["error_reason"]) == ("rate_limited", "quota")


@pytest.mark.asyncio
async def test_classifier_reaches_the_event_of_an_untraced_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app(error_classifier=lambda _: "rate_limited")

    @app.tool
    def quiet() -> None:
        raise KeyError

    set_tool_traits(app, "quiet", ToolTraits(tracing=False))
    with caplog.at_level(logging.INFO, logger="fastmcp_extensions._telemetry"):
        assert await _spans(app, "quiet") == []
    assert caplog.records[-1].telemetry["error_category"] == "rate_limited"


class _UserError(Exception):
    pass


def _traced_inside_telemetry() -> FastMCP:
    return _app()


def _traced_only() -> FastMCP:
    app = FastMCP("plain")
    register_tool_call_tracing(app, ToolTracingConfig())
    return app


def _traced_with_intent() -> FastMCP:
    return _app(capture_intent=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "arguments"),
    [
        (_traced_inside_telemetry, {}),
        (_traced_only, {}),
        (_traced_with_intent, {"intent": "why"}),
    ],
    ids=["telemetry-and-tracing", "tracing-only", "intent"],
)
async def test_user_facing_middleware_inside_tracing_keeps_the_cause(
    build: Any, arguments: dict[str, Any]
) -> None:
    app = build()
    app.add_middleware(UserFacingErrorMiddleware((_UserError,)))

    @app.tool
    def fail() -> None:
        raise _UserError

    (span,) = await _spans(app, "fail", arguments)
    attrs = _attrs(span)
    assert attrs[f"{P}.error_type"] == "_UserError"
    assert attrs[f"{P}.error.category"] == "user_error"


@pytest.mark.asyncio
async def test_error_group_is_closed_at_the_boundary() -> None:
    app = _app()
    group = (f"{P}.error.", f"{P}.upstream.")

    @app.tool
    def forges_on_success() -> None:
        trace.get_current_span().set_attributes(
            {
                f"{P}.error.category": "auth",
                f"{P}.error.fault": "caller",
                f"{P}.upstream.status_code": 418,
                f"{P}.error.reason": "free text",
            }
        )

    @app.tool
    def forges_on_failure() -> None:
        trace.get_current_span().set_attributes(
            {
                f"{P}.error.reason": "forged",
                f"{P}.error.detail": "free text",
                f"{P}.upstream.status_code": 418,
                f"{P}.error.fault": "nobody",
                f"{P}.error.cause_types": ("Forged",),
            }
        )
        raise RuntimeError

    # Nothing in the group leaves on a success.
    (span,) = await _spans(app, "forges_on_success")
    assert [key for key in _attrs(span) if key.startswith(group)] == []

    # On a failure, only what the layer itself wrote is kept.
    (span,) = await _spans(app, "forges_on_failure")
    assert {k: v for k, v in _attrs(span).items() if k.startswith(group)} == {
        f"{P}.error.category": "unclassified",
        f"{P}.error.fault": "unknown",
    }


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

    class Cancels(dict[str, object]):
        def items(self) -> Any:
            raise asyncio.CancelledError

    @app.tool
    def inner() -> int:
        add_trace_attributes(Cancels())  # must not cancel the call
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
    assert attrs[f"{P}.tool_module"] == "test_otel_middleware"
    assert (attrs[f"{P}.tool_mutating"], attrs[f"{P}.tool_destructive"]) == (True, True)

    with pytest.raises(TypeError, match="tracing must be a bool or a callable"):
        mcp_tool(tracing="yes")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_isolated_tools")
async def test_trace_plan() -> None:
    config = ToolTracingConfig(arg_default=TraceArg.FINGERPRINT, capture_intent=True)
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
    register_tool_call_tracing(app, ToolTracingConfig(capture_intent=True))
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
        ToolCallOtelMiddleware,
        ToolFilterMiddleware,
    )

    def order(app: FastMCP) -> list[type]:
        return [type(m) for m in app.middleware if isinstance(m, kinds)]

    # Registered late, tracing still lands inside telemetry and outside filters.
    late = mcp_server("late", tool_filters=[lambda _tool, _app: True])
    for _ in range(2):
        register_tool_call_tracing(late, ToolTracingConfig())
    assert order(_app()) == order(late) == list(kinds)

    off = mcp_server("off")  # tracing is off unless the telemetry config sets it
    assert order(off) == [ToolCallTelemetryMiddleware]

    # `enabled=False` is the master switch: no telemetry and no tracing.
    disabled = mcp_server(
        "x", telemetry=TelemetryConfig(enabled=False, tool_tracing=True)
    )
    assert order(disabled) == []

    plain = FastMCP("plain")
    register_tool_call_tracing(plain, ToolTracingConfig())
    assert order(plain) == [ToolCallOtelMiddleware]


@pytest.mark.asyncio
async def test_tool_code_cannot_replace_intent_or_change_arguments() -> None:
    app = _app(capture_intent=True)
    received: list[object] = []

    def reorder(arguments: dict[str, Any]) -> dict[str, object]:
        arguments["items"].sort()  # must not reach the tool
        return {"first": arguments["items"][0]}

    @app.tool
    def forge(items: list[int]) -> None:
        received.append(items)
        span = trace.get_current_span()
        span.set_attributes({f"{P}.intent": CANARY, f"{P}.intent_present": True})

    set_tool_traits(app, "forge", ToolTraits(tracing=reorder))
    (span,) = await _spans(app, "forge", {"items": [3, 1], "intent": "real"})
    assert (_attrs(span)[f"{P}.intent"], _attrs(span)[f"{P}.first"]) == ("real", 1)
    (span,) = await _spans(app, "forge", {"items": [3, 1]})
    assert f"{P}.intent" not in _attrs(span)
    assert received == [[3, 1], [3, 1]]


def test_intent_is_made_exportable() -> None:
    # A lone surrogate cannot be encoded, and would fail the whole export batch.
    assert _tracing.clean_intent(" why\x00 it\ud800 ran\nnext line ") == (
        "why  it  ran\nnext line"
    )


def test_boundary_survives_a_cancelling_other_spans_hook() -> None:
    def cancels(span: ReadableSpan) -> dict[str, object]:
        raise asyncio.CancelledError

    app = _app(other_spans=cancels)
    tracer = trace.get_tracer("test")
    with capture_tool_spans() as spans, tracer.start_as_current_span("GET"):
        pass
    assert app is not None
    assert spans == []


def test_boundary_releases_a_discarded_app() -> None:
    # No tools: FastMCP caches tool functions, which pins an app they close over.
    config = ToolTracingConfig(other_spans=lambda span: {})
    app = mcp_server("t", telemetry=TelemetryConfig(tool_tracing=config))
    install = next(m for m in app.middleware if isinstance(m, ToolCallOtelMiddleware))
    boundary = BoundaryExporter(InMemorySpanExporter(), install)
    released = weakref.ref(install)
    del app, install, config
    gc.collect()
    # The provider keeps the exporter; it must not keep the app's hooks alive.
    assert released() is None
    assert boundary.export([]) is SpanExportResult.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["", "direct"])
async def test_other_spans_never_sees_an_opted_out_tool(how: str) -> None:
    keep = [True]  # cleared below: the hook outlives this test with its app
    app = _app(other_spans=lambda span: {} if keep else None)
    set_tool_traits(app, "add", ToolTraits(tracing=False))
    spans = await _spans(app, "add", ADD, how)
    keep.clear()
    # The in-process test client's own CLIENT span is not the server's.
    assert [span for span in spans if span.kind is SpanKind.SERVER] == []


@pytest.mark.asyncio
async def test_other_spans_never_sees_a_failed_tools_list() -> None:
    class Fails(Middleware):
        async def on_list_tools(self, context: Any, call_next: Any) -> Any:
            raise RuntimeError

    keep = [True]  # cleared below: the hook outlives this test with its app
    app = _app(other_spans=lambda span: {} if keep else None)
    app.add_middleware(Fails())
    with capture_tool_spans() as spans:
        async with Client(app) as client:
            with contextlib.suppress(Exception):
                await client.list_tools()
    keep.clear()
    assert "tools/list" not in {s.name for s in spans if s.kind is SpanKind.SERVER}


@pytest.mark.asyncio
async def test_cancelled_call_cancels_a_scheduled_hook() -> None:
    tasks: list[asyncio.Future[None]] = []

    def hook() -> asyncio.Future[None]:
        tasks.append(asyncio.ensure_future(asyncio.sleep(5)))
        return tasks[-1]

    await _spans(_app(attributes=hook), "slow", how="cancel")
    await asyncio.sleep(0)
    assert tasks[-1].cancelled()


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
