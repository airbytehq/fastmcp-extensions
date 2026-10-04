# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""OpenTelemetry SDK half of tool-call tracing.

Holds everything that constructs or subclasses SDK types: the provider
policy, the privacy boundary, and the span capture behind
`capture_tool_spans()`.

This module imports the SDK at the top, and the SDK is an optional
dependency (the `[otel]` extra). `_tracing` therefore imports it only inside
functions, and nothing else may import it.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
import weakref
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from opentelemetry import trace
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import SpanContext, Status, StatusCode

from fastmcp_extensions._arg_trace import is_arg_key
from fastmcp_extensions._tracing import (
    _INSTALLS,
    _KEY,
    _REQUEST_ID,
    CAPTURES,
    INTENT_ARG,
    MARK,
    OUTCOMES,
    bound_value,
    clean_intent,
)

if TYPE_CHECKING:
    from opentelemetry.util.types import AttributeValue

    from fastmcp_extensions._tracing import ToolCallTracingMiddleware

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CALLER_HASH = re.compile(r"[0-9a-f]{16}")
_INSTANCE_ID = str(uuid.uuid4())
_OWN_PROVIDERS: weakref.WeakSet[TracerProvider] = weakref.WeakSet()
"""The providers this package created, as opposed to ones a host installed."""


def global_provider(
    resource_attributes: Mapping[str, str] | None = None,
) -> tuple[TracerProvider, str] | None:
    """Return the global SDK provider and whether it was `created` or `existing`.

    Creates and sets one when none is set yet. Returns `None` when the global
    provider is not an SDK provider, since nothing can be attached to it.
    """
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current, "existing"
    if not isinstance(current, trace.ProxyTracerProvider):
        return None
    # A client's `traceparent` must not be able to switch tracing off.
    mine = TracerProvider(
        sampler=ParentBased(ALWAYS_ON, remote_parent_not_sampled=ALWAYS_ON),
        resource=_resource(resource_attributes),
    )
    trace.set_tracer_provider(mine)
    # Another thread may have set a provider first; attach to whoever won.
    current = trace.get_tracer_provider()
    if not isinstance(current, TracerProvider):
        return None
    if current is not mine:
        return current, "existing"
    _OWN_PROVIDERS.add(mine)
    return current, "created"


def _resource(attributes: Mapping[str, str] | None) -> Resource:
    """Return a resource carrying `attributes` as defaults.

    Merging the detector last lets `OTEL_SERVICE_NAME` and
    `OTEL_RESOURCE_ATTRIBUTES` win over the defaults passed in.
    """
    return Resource.create(
        {"service.instance.id": _INSTANCE_ID, **(attributes or {})}
    ).merge(OTELResourceDetector().detect())


def attach(
    install: ToolCallTracingMiddleware,
    *,
    service_name: str,
    service_version: str | None,
) -> str | None:
    """Export `install`'s spans through the privacy boundary.

    Returns `created` or `existing` for the provider used, or `None` when a
    non-SDK global provider is installed.
    """
    exporter = install.config.exporter
    if exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter()
    elif exporter == "console":
        # Never stdout, which would corrupt the stdio transport.
        exporter = ConsoleSpanExporter(out=sys.stderr)
    resource_attributes = {"service.name": service_name}
    if service_version:
        resource_attributes["service.version"] = service_version
    found = global_provider(resource_attributes)
    if found is None:
        return None
    provider, how = found
    # A provider's resource is the first app's. On a provider this package
    # created, every app exports its own identity; a host's provider keeps
    # the identity the host gave it.
    if provider in _OWN_PROVIDERS:
        install.resource = _resource(resource_attributes)
    provider.add_span_processor(BatchSpanProcessor(BoundaryExporter(exporter, install)))
    return how


def clean(
    span: ReadableSpan, install: ToolCallTracingMiddleware
) -> ReadableSpan | None:
    """Rebuild `span` from the allowlist for `install`, or return `None` to drop it.

    This is the privacy boundary. It never raises: a span that fails to
    rebuild is dropped.
    """
    try:
        return _clean(span, install)
    except Exception:
        logger.debug("span dropped at the privacy boundary", exc_info=True)
        return None


def _clean(
    span: ReadableSpan, install: ToolCallTracingMiddleware
) -> ReadableSpan | None:
    p, a = install.prefix + ".", span.attributes or {}
    status = span.status.status_code
    mark = a.get(MARK)
    if mark is None:
        other_spans = install.config.other_spans
        kept = other_spans(span) if other_spans is not None else None
        if kept is None:
            return None
        return _rebuild(install, span, span.name, dict(kept), (), span.parent, status)
    if mark != install.mark:
        # Another app's span; its own boundary exports it.
        return None

    out: dict[str, object] = {}
    for key, value in a.items():
        # Argument records have their own bounds; `revalidate` below re-adds them.
        if key == MARK or not key.startswith(p) or is_arg_key(install.prefix, key):
            continue
        # A tool can write past the package API, so the key rule is re-applied.
        if not _KEY.fullmatch(key[len(p) :]):
            continue
        if key == p + INTENT_ARG:
            if not install.config.capture_intent:
                continue
            bounded = clean_intent(value) or None
        else:
            bounded = bound_value(value, allow_list=True)
        if bounded is not None:
            out[key] = bounded
    if (server := bound_value(a.get("fastmcp.server.name"))) is not None:
        out["fastmcp.server.name"] = server
    protocol = a.get("mcp.protocol.version")
    if isinstance(protocol, str) and _PROTOCOL_VERSION.fullmatch(protocol):
        out["mcp.protocol.version"] = out[p + "mcp_protocol_version"] = protocol
    # FastMCP's raw `mcp.session.id` never passes; only our digest does.
    session = out.pop(p + "session_id", None)
    if isinstance(session, str) and _SHA256.fullmatch(session):
        out[p + "session_id"] = session
        out["mcp.session.id"] = out["gen_ai.conversation.id"] = session

    # The caller is exported only as telemetry's salted hash and its kind.
    caller, kind = out.pop(p + "caller_hash", None), out.pop(p + "caller_id_type", None)
    if (
        isinstance(caller, str)
        and _CALLER_HASH.fullmatch(caller)
        and kind in ("subject", "client")
    ):
        out[p + "caller_hash"], out[p + "caller_id_type"] = caller, kind

    outcome = out.get(p + "outcome")
    if outcome not in OUTCOMES:
        if p + "tools.count" not in out:
            return None
        out["mcp.method.name"] = "tools/list"
        return _rebuild(install, span, "tools/list", out, (), None, status)

    # FastMCP rewrites the span's name, tool name, method, and status when the
    # tool makes an in-process FastMCP call, so all four come from our keys.
    out["gen_ai.operation.name"] = "execute_tool"
    out["mcp.method.name"] = "tools/call"
    tool = out.pop(p + "tool_name", None)
    tool = tool if isinstance(tool, str) else None
    if tool is not None:
        out["gen_ai.tool.name"] = tool
    out.update(install.args.revalidate(tool, a))
    # A client's trace context is not trusted: root spans export no parent.
    root = out.get(p + "root") is True
    for key, pattern in (
        ("jsonrpc.request.id", _REQUEST_ID),
        ("gen_ai.tool.call.id", _SHA256),
    ):
        value = a.get(key)
        if root and isinstance(value, str) and pattern.fullmatch(value):
            out[key] = value
    error_type = out.pop(p + "error_type", None)
    events: tuple[Event, ...] = ()
    if (
        outcome != "success"
        and isinstance(error_type, str)
        and error_type.isidentifier()
    ):
        out[p + "error_type"] = out["error.type"] = error_type
        stamps = [event.timestamp for event in span.events if event.name == "exception"]
        if stamps:
            events = (Event("exception", {"exception.type": error_type}, stamps[0]),)
    name = f"tools/call {tool}" if tool else "tools/call"
    status = StatusCode.UNSET if outcome == "success" else StatusCode.ERROR
    return _rebuild(
        install, span, name, out, events, None if root else span.parent, status
    )


def _rebuild(
    install: ToolCallTracingMiddleware,
    span: ReadableSpan,
    name: str,
    attributes: Mapping[str, object],
    events: Sequence[Event],
    parent: SpanContext | None,
    status: StatusCode,
) -> ReadableSpan:
    """Return a copy of `span` carrying only what the boundary kept."""

    def bare(context: SpanContext) -> SpanContext:
        # Rebuilt without the trace state, which the client supplies.
        return SpanContext(
            context.trace_id, context.span_id, context.is_remote, context.trace_flags
        )

    return ReadableSpan(
        name=name,
        context=bare(span.context),
        parent=parent and bare(parent),
        resource=install.resource or span.resource,
        attributes=cast("Mapping[str, AttributeValue]", attributes),
        events=events,
        links=(),
        kind=span.kind,
        status=Status(status),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class BoundaryExporter(SpanExporter):
    """Exporter wrapper that passes only cleaned spans to the real exporter."""

    def __init__(self, inner: SpanExporter, install: ToolCallTracingMiddleware) -> None:
        """Wrap `inner` for the spans of `install`."""
        self._inner = inner
        # Held weakly: the provider keeps this exporter for the life of the
        # process, and it must not keep a discarded app's config and hooks.
        self._install = weakref.ref(install)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export the spans that survive the privacy boundary."""
        install = self._install()
        if install is None:
            return SpanExportResult.SUCCESS
        kept = [
            cleaned
            for cleaned in (clean(span, install) for span in spans)
            if cleaned is not None
        ]
        if not kept:
            return SpanExportResult.SUCCESS
        try:
            return self._inner.export(kept)
        except Exception:
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush the wrapped exporter."""
        return self._inner.force_flush(timeout_millis)

    def shutdown(self) -> None:
        """Shut down the wrapped exporter."""
        self._inner.shutdown()


_capture_provider: TracerProvider | None = None


class _CaptureExporter(SpanExporter):
    """Copies every span an install would export into the active captures."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if CAPTURES:
            for span in spans:
                for install in list(_INSTALLS):
                    cleaned = clean(span, install)
                    if cleaned is not None:
                        for sink in CAPTURES:
                            sink.append(cleaned)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


@contextmanager
def capture() -> Iterator[list[ReadableSpan]]:
    """Yield a list that fills with spans as they would be exported."""
    global _capture_provider
    found = global_provider()
    if found is None:
        raise RuntimeError(
            "capture_tool_spans() needs an OpenTelemetry SDK TracerProvider, "
            "but a different global TracerProvider is installed"
        )
    provider = found[0]
    if provider is not _capture_provider:
        provider.add_span_processor(SimpleSpanProcessor(_CaptureExporter()))
        _capture_provider = provider
    spans: list[ReadableSpan] = []
    CAPTURES.append(spans)
    try:
        yield spans
    finally:
        # By identity: `list.remove` compares by equality and would remove
        # another capture's equal (for example empty) list.
        CAPTURES[:] = [sink for sink in CAPTURES if sink is not spans]
