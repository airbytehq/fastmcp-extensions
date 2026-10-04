# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""OpenTelemetry tracing for MCP tool calls.

FastMCP opens a SERVER span for every request, but nothing exports it until an
OpenTelemetry SDK is installed. This module does three things:

1. **Setup** - installs the SDK and an exporter (from the `[otel]` extra).
2. **Enrich** - `ToolCallTracingMiddleware` adds attributes to FastMCP's own
   `tools/call` span while it is still open. It opens its own span only for
   nested or direct `app.call_tool()` calls.
3. **Privacy boundary** - an exporter wrapper rebuilds every span from an
   allowlist and drops everything else, so raw results and exception messages
   never leave the process. Tool arguments leave only as the records
   `_arg_trace` builds, which are re-validated here.

`mcp_server()` registers the middleware from `TelemetryConfig.tool_tracing`; see
`fastmcp_extensions.server` for the user-facing configuration docs. Plain
`FastMCP` apps use `register_tool_call_tracing()`, which is idempotent.

This module imports only the OpenTelemetry API. Everything that needs the SDK
lives in `_tracing_sdk`, which is imported lazily.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import logging
import math
import os
import re
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from fastmcp.exceptions import DisabledError, NotFoundError
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.telemetry import SEAM_SPAN_MARKER, get_protocol_span_attributes
from fastmcp.telemetry import suppress_fastmcp_telemetry
from fastmcp.tools import Tool, ToolResult
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from fastmcp_extensions._arg_trace import ArgTracer, TraceArg
from fastmcp_extensions._attribution import _client_info
from fastmcp_extensions._telemetry import (
    resolve_extra_properties,
    resolve_version,
    telemetry_opted_out,
)
from fastmcp_extensions._telemetry_middleware import (
    ToolCallTelemetryMiddleware,
    _requested_version,
    unwrap_tool_error,
)
from fastmcp_extensions._tracing_extras import (
    MAX_LIST_ITEMS,
    ContractCache,
    argument_name_attributes,
    declared_parameters,
    error_attributes,
    eval_attributes,
    result_attributes,
    safe_name,
    tool_contract,
    tool_list_attributes,
    user_facing_error_types,
    validation_attributes,
)
from fastmcp_extensions.tool_traits import MutationClass, ToolTraits, get_tool_traits

if TYPE_CHECKING:
    from fastmcp import Context, FastMCP
    from mcp import types as mt
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)

MARK = "fastmcp_extensions.install-id"
"""Span attribute naming the install that stamped a span. Never exported.

The `-` cannot occur in an attribute prefix or a hook key, so nothing collides.
"""

INTENT_ARG = "intent"
INTENT_SENTENCE = (
    "Tools may accept an optional `intent` string; if present, state in one "
    "sentence why you are calling the tool (never credentials, identifiers or "
    "data values)."
)
_INTENT_SCHEMA = {
    "type": "string",
    "description": (
        "Briefly describe the wider task and why you chose this tool, in English. "
        "Omit argument values, personal information, and secrets."
    ),
}
MAX_STRING, MAX_INTENT = 256, 4096
_TRUNCATED = "...[truncated]"
OUTCOMES = frozenset(
    {"success", "tool_error", "exception", "cancelled", "unknown_tool"}
)
_KEY = re.compile(r"[a-z0-9_]+(\.[a-z0-9_]+)*")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
RESERVED_PREFIXES = frozenset(
    {
        "mcp",
        "fastmcp",
        "gen_ai",
        "enduser",
        "error",
        "exception",
        "jsonrpc",
        "rpc",
        "http",
        "url",
    }
)
"""First segments an `attribute_prefix` cannot use.

The privacy boundary keeps everything under the prefix, so a prefix inside a
namespace that FastMCP or an instrumentation writes would export their raw
attributes.
"""
OWNED_KEYS = frozenset(
    {
        "root",
        "outcome",
        "error_type",
        "client_name",
        "client_version",
        "session_id",
        "mcp_protocol_version",
        "tool_name",
        "tool_module",
        "tool_mutating",
        "tool_destructive",
        "tool_requested_name",
        "intent",
        "intent_present",
        "arg_tracing",
        "arg_key_scope",
        "arg_scope_id",
        "arg_trace_dropped",
    }
)
"""Attribute suffixes only the layer writes; hooks and tools cannot set them."""

OWNED_NAMESPACES = frozenset(
    {"arg", "args", "error", "eval", "process", "result", "tool", "tools", "upstream"}
)
"""First dotted segments reserved for the layer's own attribute groups."""

_STDIO_SESSION = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class TracingConfig:
    """Configuration for OpenTelemetry tool-call tracing.

    Every attribute the layer writes is namespaced under `attribute_prefix`.
    Values supplied through `attributes`, `late_attributes`, a per-tool
    callable, or `add_trace_attributes()` are bounded: strings are stripped,
    must be printable, and are cut to 256 characters; `bool`, `int`, and
    finite `float` pass; anything else is dropped.

    When an SDK `TracerProvider` is already installed, the layer attaches to
    it. That provider's sampler then decides which calls are traced (the SDK
    default drops a call whose client sends an unsampled `traceparent`), and
    its own exporters receive FastMCP's unfiltered spans: the allowlist
    applies only to the exporter configured here.

    Attributes:
        enabled: Master switch.
        exporter: `"otlp"` exports over OTLP/HTTP only when
            `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`
            is set, and otherwise stays dormant. `"console"` writes to stderr.
            A `SpanExporter` instance is used as given and receives spans
            after the privacy boundary.
        attribute_prefix: Namespace for every attribute the layer writes. Its
            first segment cannot be a namespace FastMCP or OpenTelemetry
            writes, such as `mcp`, `fastmcp`, `gen_ai`, or `http`.
        attributes: Extra per-call attributes, resolved before the tool runs.
        late_attributes: Extra per-call attributes, resolved after the tool
            returns or raises, while the span is still open. May be async.
        capture_intent: Adds an optional `intent` string argument to every
            tool schema, appends one sentence to the server instructions,
            records the argument, and strips it before the tool runs. A tool
            that declares its own `intent` parameter keeps it, and it is not
            recorded.
        error_classifier: Server override for the error category. Ignored
            unless it returns a known category.
        other_spans: Decides what survives of spans the layer did not stamp,
            such as HTTP client spans. Returns the complete attribute set to
            keep, or `None` to drop the span. By default all are dropped. A
            kept span's name, kind, timing, and parent are exported unchanged,
            so return `None` for spans whose name may carry data. The hook
            sees every unstamped span in the process, so set it on only one
            app per process.
        arg_key: 32-byte secret for argument hashes, or a callable returning
            it. Without it, hashed argument records fall back to presence.
        arg_default: How `str`, `int`, `float`, `UUID`, and `list[str]`
            arguments without a `TraceArg` marker are recorded. `VALUE` is
            treated as `HASH`, so a raw value is never a default.
    """

    enabled: bool = True
    exporter: Literal["otlp", "console"] | SpanExporter = "otlp"
    attribute_prefix: str = "fastmcp_extensions"
    attributes: Mapping[str, object] | Callable[[], Mapping[str, object]] | None = None
    late_attributes: (
        Callable[[], Mapping[str, object] | Awaitable[Mapping[str, object]]] | None
    ) = None
    capture_intent: bool = False
    error_classifier: Callable[[BaseException], str | None] | None = None
    other_spans: Callable[[ReadableSpan], Mapping[str, object] | None] | None = None
    arg_key: bytes | Callable[[], bytes | None] | None = field(default=None, repr=False)
    arg_default: TraceArg = TraceArg.HASH


class _Call(NamedTuple):
    """The tool call the current task is inside."""

    span: trace.Span
    prefix: str
    traced: bool = True
    inside_traced: bool = True
    """Whether this call is a traced call or runs inside one."""


_CURRENT: ContextVar[_Call | None] = ContextVar(
    "fastmcp_extensions_traced_call", default=None
)
_INSTALLS: weakref.WeakSet[ToolCallTracingMiddleware] = weakref.WeakSet()
CAPTURES: list[list[ReadableSpan]] = []
"""The span lists of the open `capture_tool_spans()` blocks."""


def bound_value(
    value: object, *, allow_list: bool = False
) -> str | bool | int | float | tuple[str, ...] | None:
    """Return `value` if it is safe to export as an attribute, else `None`.

    Strings are stripped, must be non-empty and printable, and are cut to 256
    characters. With `allow_list`, a list or tuple becomes a tuple of its
    first 16 items that bound to a string.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(2**63) <= value < 2**63 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        # Called on `str` itself, so a subclass cannot override the checks.
        text = str.strip(value)
        return text[:MAX_STRING] if text and str.isprintable(text) else None
    if allow_list and isinstance(value, (list, tuple)):
        bounded = (bound_value(item) for item in value[:MAX_LIST_ITEMS])
        return tuple(item for item in bounded if isinstance(item, str)) or None
    return None


def user_attributes(prefix: str, attrs: object) -> dict[str, object]:
    """Bound and namespace attributes supplied by a hook or a tool.

    Keys the layer owns, malformed keys, and values that fail `bound_value`
    are dropped.
    """
    out: dict[str, object] = {}
    if not isinstance(attrs, Mapping):
        return out
    for key, value in attrs.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            continue
        if key in OWNED_KEYS or key.split(".", 1)[0] in OWNED_NAMESPACES:
            continue
        bounded = bound_value(value)
        if bounded is not None:
            out[f"{prefix}.{key}"] = bounded
    return out


def _set_attributes(span: trace.Span, attrs: Mapping[str, object]) -> None:
    """Write attributes whose values have already been bounded."""
    span.set_attributes(cast("Mapping[str, AttributeValue]", attrs))


def add_trace_attributes(attributes: Mapping[str, object]) -> None:
    """Add attributes to the span of the tool call this code is running in.

    Keys are namespaced under the server's `attribute_prefix` and values are
    bounded like every other attribute. Never raises, and does nothing outside
    a traced tool call.
    """
    try:
        call = _CURRENT.get()
        if call is not None and call.traced and call.span.is_recording():
            _set_attributes(call.span, user_attributes(call.prefix, attributes))
    except Exception:
        logger.debug("add_trace_attributes skipped", exc_info=True)


def clean_intent(value: object) -> str:
    """Return the stripped intent text, cut to 4096 characters with a marker."""
    text = str.strip(value) if isinstance(value, str) else ""
    if len(text) > MAX_INTENT:
        text = text[: MAX_INTENT - len(_TRUNCATED)] + _TRUNCATED
    return text


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _is_unclaimed_seam(span: trace.Span, method: str = "tools/call") -> bool:
    """Return whether `span` is FastMCP's request span and no outer call owns it."""
    attrs = getattr(span, "attributes", None) or {}
    current = _CURRENT.get()
    return (
        span.is_recording()
        and attrs.get(SEAM_SPAN_MARKER) is True
        and attrs.get("mcp.method.name") == method
        and (current is None or current.span is not span)
    )


@contextmanager
def _claimed_span(name: str, prefix: str) -> Iterator[tuple[trace.Span, bool]]:
    """Yield `(span, own)` for one tool call.

    The span is FastMCP's request span when it is unclaimed. Otherwise (a
    nested or direct `app.call_tool()`) it is our own SERVER span, and
    FastMCP's inner span is suppressed so the call is traced once.
    """
    span = trace.get_current_span()
    own = not _is_unclaimed_seam(span)
    with ExitStack() as stack:
        if own:
            span = stack.enter_context(
                trace.get_tracer("fastmcp_extensions").start_as_current_span(
                    f"tools/call {name}",
                    kind=SpanKind.SERVER,
                    record_exception=False,
                    set_status_on_exception=False,
                )
            )
            stack.enter_context(suppress_fastmcp_telemetry())
        token = _CURRENT.set(_Call(span, prefix))
        try:
            yield span, own
        finally:
            _CURRENT.reset(token)


def _failure(exc: BaseException) -> tuple[str, BaseException]:
    """Return the `(outcome, cause)` of a failed tool call."""
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", exc
    if isinstance(exc, (NotFoundError, DisabledError)):
        return "unknown_tool", exc
    return "exception", unwrap_tool_error(exc)


def _safe(
    source: Callable[[], Mapping[str, object]], prefix: str = ""
) -> dict[str, object]:
    """Return the attributes from `source`, or nothing when it raises.

    Each attribute source runs through this, so one failing source cannot
    lose the attributes of the others.
    """
    try:
        return {prefix + key: value for key, value in source().items()}
    except Exception as exc:
        # Only the type: a per-tool callable's error message can quote the
        # client's argument values.
        logger.debug("trace attribute source skipped: %s", type(exc).__name__)
        return {}


def _client_attributes() -> dict[str, object]:
    """Return the bounded name and version of the MCP client."""
    client = _client_info()
    labels = {
        "client_name": bound_value(client.name),
        "client_version": bound_value(client.version),
    }
    return {key: label for key, label in labels.items() if label is not None}


def _tool_attributes(traits: ToolTraits, tool: Tool) -> dict[str, object]:
    """Return the module and mutation class of `tool`, read at call time."""
    attrs: dict[str, object] = {}
    if traits.mcp_module:
        attrs["tool_module"] = traits.mcp_module
    mutation = traits.mutation_class or MutationClass.from_annotations(tool.annotations)
    if mutation is not MutationClass.UNKNOWN:
        attrs["tool_mutating"] = mutation is not MutationClass.READ
        attrs["tool_destructive"] = mutation is MutationClass.DESTRUCTIVE
    return attrs


class ToolCallTracingMiddleware(Middleware):
    """Middleware that enriches the OpenTelemetry span of every MCP tool call.

    It stays installed when export is dormant or disabled, because intent
    stripping and `capture_tool_spans()` depend on it. It then leaves every
    span alone and runs no hook, even when another provider is recording.
    """

    def __init__(self, config: TracingConfig) -> None:
        """Initialize the middleware for one app."""
        self.config = config
        self.prefix = config.attribute_prefix
        self.mark = uuid.uuid4().hex[:12]
        self.exporting = False
        self._installed_at = time.monotonic()
        self._contracts: ContractCache = {}
        self.args = ArgTracer(
            self.prefix,
            default=config.arg_default,
            key=config.arg_key,
            skip=(INTENT_ARG,) if config.capture_intent else (),
        )
        _INSTALLS.add(self)

    def _active(self) -> bool:
        """Return whether spans stamped now would reach an exporter or a capture."""
        return self.exporting or bool(CAPTURES)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Stamp the `tools/list` span and advertise the `intent` argument."""
        # Checked before the handler runs: a resource or prompt handler that
        # lists tools in-process makes FastMCP rename its own request span.
        span = trace.get_current_span()
        stamp = self._active() and _is_unclaimed_seam(span, "tools/list")
        tools = await call_next(context)
        try:
            if stamp:
                attrs = self._request_attributes(context.fastmcp_context)
                attrs.update(
                    _safe(
                        lambda: tool_list_attributes(tools, self._contracts),
                        self.prefix + ".",
                    )
                )
                _set_attributes(span, attrs)
        except Exception:
            logger.debug("tools/list trace attributes skipped", exc_info=True)
        if not self.config.capture_intent:
            return tools
        try:
            with_intent = []
            for tool in tools:
                params = copy.deepcopy(tool.parameters)
                params.setdefault("properties", {}).setdefault(
                    INTENT_ARG, dict(_INTENT_SCHEMA)
                )
                with_intent.append(tool.model_copy(update={"parameters": params}))
            return with_intent
        except Exception:
            logger.debug("intent schema skipped", exc_info=True)
            return tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Trace one tool call. Tracing never changes the call's outcome."""
        name = context.message.name
        ctx = context.fastmcp_context
        app = ctx.fastmcp if ctx is not None else None
        outer = _CURRENT.get()
        inside = outer is not None and outer.inside_traced
        traits = get_tool_traits(app, name) if app is not None else ToolTraits()
        option = traits.tracing
        if option is False or not self._active():
            # Remember the untraced call so a nested call does not claim this
            # tool's request span and stamp it with the wrong tool.
            token = _CURRENT.set(
                _Call(trace.get_current_span(), self.prefix, False, inside)
            )
            try:
                return await call_next(await self._strip_intent(context, None))
            finally:
                _CURRENT.reset(token)

        with _claimed_span(name, self.prefix) as (span, own):
            recording = span.is_recording()
            tool: Tool | None = None
            # What the boundary needs to export the span. Written first, so a
            # failing attribute source below cannot lose the span, and again
            # with the outcome, so the SDK's attribute limit evicts other
            # keys before these.
            keep: dict[str, object] = {
                MARK: self.mark,
                f"{self.prefix}.root": not inside,
            }
            result: ToolResult | None = None
            error: BaseException | None = None
            try:
                try:
                    if recording:
                        _set_attributes(span, keep)
                    arguments = context.message.arguments or {}
                    if recording or (
                        self.config.capture_intent and INTENT_ARG in arguments
                    ):
                        tool = await self._tool(context)
                    if tool is not None:
                        # The boundary derives `gen_ai.tool.name` and the span
                        # name from this key, because FastMCP rewrites its own
                        # when the tool makes an in-process FastMCP call.
                        keep[f"{self.prefix}.tool_name"] = name
                    stripped = await self._strip_intent(context, tool)
                    # Only the injected `intent` is recorded. A tool's own
                    # `intent` parameter is not stripped and is the tool's data.
                    intent = (
                        arguments.get(INTENT_ARG) if stripped is not context else None
                    )
                    context = stripped
                    if recording:
                        attrs = self._before(
                            context, tool, traits, not inside, own, intent
                        )
                        if callable(option):
                            received = dict(context.message.arguments or {})
                            attrs.update(
                                _safe(
                                    lambda: user_attributes(
                                        self.prefix, option(received)
                                    )
                                )
                            )
                        _set_attributes(span, attrs)
                except Exception:
                    # Tracing must never break a tool call.
                    logger.debug("trace attributes skipped", exc_info=True)
                result = await call_next(context)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                if recording:
                    late: dict[str, object] = {}
                    try:
                        if not isinstance(error, asyncio.CancelledError):
                            late = await self._late()
                    except asyncio.CancelledError as cancelled:
                        result, error = None, cancelled
                        raise
                    finally:
                        self._after(span, result, error, tool, app, late, keep)

    async def _tool(
        self, context: MiddlewareContext[mt.CallToolRequestParams]
    ) -> Tool | None:
        """Return the tool this call resolves to, or `None`."""
        try:
            ctx = context.fastmcp_context
            if ctx is None:
                return None
            return await ctx.fastmcp.get_tool(
                context.message.name,
                version=_requested_version(context.message.meta),
            )
        except Exception:
            return None

    async def _strip_intent(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        tool: Tool | None,
    ) -> MiddlewareContext[mt.CallToolRequestParams]:
        """Remove a captured `intent` argument unless the tool declares it."""
        arguments = context.message.arguments or {}
        if not self.config.capture_intent or INTENT_ARG not in arguments:
            return context
        try:
            tool = tool or await self._tool(context)
            if tool is not None and INTENT_ARG in declared_parameters(tool):
                return context
            stripped = {k: v for k, v in arguments.items() if k != INTENT_ARG}
            return context.copy(
                message=context.message.model_copy(update={"arguments": stripped})
            )
        except Exception:
            logger.debug("intent stripping skipped", exc_info=True)
            return context

    def _request_attributes(self, ctx: Context | None) -> dict[str, object]:
        """Return the attributes shared by tool spans and the `tools/list` span."""
        p = self.prefix
        attrs: dict[str, object] = {
            MARK: self.mark,
            f"{p}.process.uptime_s": round(time.monotonic() - self._installed_at, 1),
        }
        headers = get_http_headers(include={"mcp-session-id"})
        session = headers.get("mcp-session-id")
        if session:
            attrs[f"{p}.session_id"] = _digest(session)
        elif ctx is not None and ctx.transport == "stdio":
            attrs[f"{p}.session_id"] = _STDIO_SESSION
        attrs.update(_safe(_client_attributes, p + "."))
        attrs.update(_safe(lambda: eval_attributes(headers), p + "."))
        return attrs

    def _before(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        tool: Tool | None,
        traits: ToolTraits,
        root: bool,
        own: bool,
        intent: object,
    ) -> dict[str, object]:
        """Return the attributes written before the tool runs.

        Later sources win on a key clash, and hook attributes cannot touch
        keys the layer owns.
        """
        p, name, ctx = self.prefix, context.message.name, context.fastmcp_context
        attrs = self._request_attributes(ctx)
        if tool is not None:
            attrs.update(_safe(lambda: _tool_attributes(traits, tool), p + "."))
        else:
            # The requested name is agent-supplied, so only a well-formed one
            # is recorded.
            attrs[f"{p}.tool_requested_name"] = safe_name(name)
        if own:
            # FastMCP sets these on its request span; our own span needs them.
            attrs.update(_safe(get_protocol_span_attributes))
            if ctx is not None:
                attrs["fastmcp.server.name"] = ctx.fastmcp.name
        if ctx is not None:
            request = ctx.request_context
            if root and request is not None and request.request_id is not None:
                request_id = str(request.request_id)
                attrs["gen_ai.tool.call.id"] = _digest(request_id)
                if _REQUEST_ID.fullmatch(request_id):
                    attrs["jsonrpc.request.id"] = request_id
        if self.config.capture_intent:
            text = clean_intent(intent)
            attrs[f"{p}.intent_present"] = bool(text)
            if text:
                attrs[f"{p}.intent"] = text
        if tool is not None:
            arguments = context.message.arguments
            attrs.update(
                _safe(lambda: argument_name_attributes(arguments, tool), p + ".")
            )
            attrs.update(
                _safe(
                    lambda: {
                        "tool.fingerprint": tool_contract(tool, self._contracts)[0]
                    },
                    p + ".",
                )
            )
            attrs.update(
                _safe(
                    lambda: arg_trace_attributes(
                        self.args,
                        name,
                        tool,
                        arguments,
                        session_digest=attrs.get(f"{p}.session_id"),
                        client_name=attrs.get(f"{p}.client_name"),
                        client_version=attrs.get(f"{p}.client_version"),
                    )
                )
            )
        attrs.update(
            user_attributes(p, resolve_extra_properties(self.config.attributes))
        )
        return attrs

    def _after(
        self,
        span: trace.Span,
        result: ToolResult | None,
        error: BaseException | None,
        tool: Tool | None,
        app: FastMCP | None,
        late: Mapping[str, object],
        keep: Mapping[str, object],
    ) -> None:
        """Write `late`, the outcome, and `keep` again, while the span is open.

        FastMCP writes its own `error.type` and exception event after the
        middleware unwinds; the privacy boundary ignores those and derives
        the exported keys from the ones written here.
        """
        try:
            p = self.prefix
            # First, so the SDK's attribute limit evicts these before the rest.
            attrs: dict[str, object] = dict(late)
            if error is not None:
                outcome, cause = _failure(error)
                attrs[f"{p}.error_type"] = type(cause).__name__
                attrs.update(
                    _safe(
                        lambda: error_attributes(
                            cause,
                            unknown_tool=outcome == "unknown_tool",
                            user_facing_errors=(
                                user_facing_error_types(app) if app is not None else ()
                            ),
                            classifier=self.config.error_classifier,
                        ),
                        p + ".",
                    )
                )
                attrs.update(_safe(lambda: validation_attributes(cause, tool), p + "."))
            elif result is not None and result.is_error:
                outcome = "tool_error"
                attrs[f"{p}.error_type"] = "ToolError"
                attrs.update(_safe(lambda: error_attributes(None), p + "."))
            else:
                outcome = "success"
            if result is not None:
                attrs.update(_safe(lambda: result_attributes(result), p + "."))
            attrs[f"{p}.outcome"] = outcome
            attrs.update(keep)
            _set_attributes(span, attrs)
            if outcome != "success":
                span.set_status(Status(StatusCode.ERROR))
        except Exception:
            logger.debug("trace outcome skipped", exc_info=True)

    async def _late(self) -> dict[str, object]:
        """Return the bounded `late_attributes`, or nothing when the hook fails."""
        hook = self.config.late_attributes
        if hook is None:
            return {}
        try:
            value = hook()
            if inspect.isawaitable(value):
                # Run as its own task: `asyncio.wait` raises `CancelledError`
                # only when this call is cancelled, not when the hook leaks one.
                task = asyncio.ensure_future(value)
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    task.cancel()
                    raise
                value = {} if task.cancelled() else task.result()
            return user_attributes(self.prefix, value)
        except Exception:
            logger.debug("late trace attributes skipped", exc_info=True)
            return {}


def _verified_principal() -> str | None:
    """Return the caller identity from verified token claims, or `None`.

    It only keys the argument hashes; it is never logged or exported.
    """
    token = get_access_token()
    if token is None:
        return None
    claims = token.claims or {}

    def claim(name: str) -> str:
        value = claims.get(name)
        return value if isinstance(value, str) else ""

    issuer, subject = claim("iss"), claim("sub")
    # Only a claimed client id: `token.client_id` can be a shared placeholder.
    client_id = claim("client_id") or claim("azp")
    # The empty issuer keeps the forms below apart from any `iss` + `sub` pair.
    if issuer and subject:
        parts = (issuer, subject)
    elif subject:
        parts = ("", client_id, subject)
    elif client_id:
        parts = ("", client_id)
    else:
        return None
    return None if any("\x00" in part for part in parts) else "\x00".join(parts)


def arg_trace_attributes(
    tracer: ArgTracer,
    name: str,
    tool: Tool,
    arguments: Mapping[str, object] | None,
    *,
    session_digest: object = None,
    client_name: object = None,
    client_version: object = None,
) -> dict[str, str]:
    """Return the argument records and their status attributes for one call."""
    return tracer.record(
        name,
        getattr(tool, "fn", None),
        declared_parameters(tool),
        arguments,
        principal=_verified_principal(),
        session_digest=session_digest if isinstance(session_digest, str) else None,
        client_name=client_name if isinstance(client_name, str) else "",
        client_version=client_version if isinstance(client_version, str) else "",
        now=time.time(),
    )


async def trace_plan(app: FastMCP) -> dict[str, dict[str, Any]]:
    """Return what tracing records for each tool of `app`, for snapshot tests.

    A snapshot of the result in a server's test suite makes every change to
    what leaves the process show up in code review. It needs neither the
    OpenTelemetry SDK nor a running server. For a tool registered in several
    versions, the plan covers only the newest.

    Returns:
        A dict keyed by tool name, sorted. Each value holds the tool's
        contract `fingerprint`, its `schema_chars`, and `args`: how each
        argument is recorded (for example `hash`, `value int`, or
        `omit`). `args` is empty for a tool with `tracing=False`.
    """
    tracer = next(
        (m.args for m in app.middleware if isinstance(m, ToolCallTracingMiddleware)),
        None,
    ) or ArgTracer(TracingConfig().attribute_prefix)
    plan: dict[str, dict[str, Any]] = {}
    for tool in await app.list_tools(run_middleware=False):
        fingerprint, schema_chars = tool_contract(tool)
        classes = (
            tracer.classes(
                tool.name, getattr(tool, "fn", None), declared_parameters(tool)
            )
            if get_tool_traits(app, tool.name).tracing is not False
            else {}
        )
        plan[tool.name] = {
            "fingerprint": fingerprint,
            "schema_chars": schema_chars,
            "args": {name: cls.describe() for name, cls in classes.items()},
        }
    return dict(sorted(plan.items()))


def register_tool_call_tracing(app: FastMCP, config: TracingConfig) -> None:
    """Register tool-call tracing on `app` unless it is already present.

    Never raises: when tracing cannot start, one log line says why and the
    server runs untraced.
    """
    if not isinstance(config, TracingConfig):
        logger.warning("tracing: disabled (config must be a TracingConfig)")
        return
    if not config.enabled or any(
        isinstance(middleware, ToolCallTracingMiddleware)
        for middleware in app.middleware
    ):
        return
    prefix = config.attribute_prefix
    if (
        not isinstance(prefix, str)
        or not _KEY.fullmatch(prefix)
        or prefix.split(".", 1)[0] in RESERVED_PREFIXES
    ):
        logger.warning(
            "tracing: disabled (invalid attribute_prefix %r)", config.attribute_prefix
        )
        return

    install = ToolCallTracingMiddleware(config)
    # Just inside telemetry and outside the tool filters, so filter
    # rejections are traced.
    telemetry_positions = [
        index
        for index, existing in enumerate(app.middleware)
        if isinstance(existing, ToolCallTelemetryMiddleware)
    ]
    if telemetry_positions:
        app.middleware.insert(telemetry_positions[-1] + 1, install)
    else:
        app.add_middleware(install)
    if config.capture_intent and INTENT_SENTENCE not in (app.instructions or ""):
        app.instructions = " ".join(filter(None, [app.instructions, INTENT_SENTENCE]))
    _start_export(app, install)


def _start_export(app: FastMCP, install: ToolCallTracingMiddleware) -> None:
    """Attach the exporter and log one line saying what tracing is doing."""
    exporter = install.config.exporter
    try:
        if telemetry_opted_out():
            logger.info("tracing: disabled (DO_NOT_TRACK is set)")
            return
        if exporter == "otlp" and not (
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        ):
            logger.info("tracing: dormant (no OTLP endpoint set)")
            return
        import fastmcp_extensions._tracing_sdk as _tracing_sdk

        if exporter not in ("otlp", "console") and not isinstance(
            exporter, _tracing_sdk.SpanExporter
        ):
            logger.warning(
                "tracing: disabled (exporter must be 'otlp', 'console', "
                "or a SpanExporter)"
            )
            return
        package = getattr(
            getattr(app, "x_mcp_server_config", None), "package_name", None
        )
        version = resolve_version(package)
        provider = _tracing_sdk.attach(
            install,
            service_name=app.name,
            service_version=None if version == "unknown" else version,
        )
        if provider is None:
            logger.warning(
                "tracing: disabled (a non-SDK global TracerProvider is installed)"
            )
        else:
            install.exporting = True
            logger.info(
                "tracing: exporter=%s provider=%s",
                exporter if isinstance(exporter, str) else "custom",
                provider,
            )
    except ImportError:
        logger.warning(
            "tracing: disabled (OpenTelemetry SDK not installed; "
            "install `fastmcp-extensions[otel]`)"
        )
    except Exception as exc:
        logger.warning("tracing: disabled (%s during setup)", type(exc).__name__)


@contextmanager
def capture_tool_spans() -> Iterator[list[ReadableSpan]]:
    """Collect the spans of tool calls made inside the block, for tests.

    Yields the list of spans as they would be exported, after the privacy
    boundary. Works with in-memory `Client(app)` and when export is dormant.
    The list also holds `tools/list` spans (a FastMCP `Client` lists tools on
    its own) and any span `other_spans` keeps, so select by `span.name`.

    Raises:
        ImportError: If the OpenTelemetry SDK is not installed.
        RuntimeError: If a non-SDK global `TracerProvider` is installed.
    """
    import fastmcp_extensions._tracing_sdk as _tracing_sdk

    with _tracing_sdk.capture() as spans:
        yield spans
