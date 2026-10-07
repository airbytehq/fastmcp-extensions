# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Public configuration types for OpenTelemetry tool-call tracing.

This module imports only the standard library at runtime, so the rest of the
package can import it without loading the tracing middleware.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter


# A plain `Enum`, not a `str` subclass: FastMCP publishes a lone string in
# `Annotated[T, "..."]` as the parameter's description.
class TraceArg(Enum):
    """How one tool argument is recorded on the tool span.

    Declare it inside `Annotated[...]` on the tool's parameter.

    - `OMIT`: nothing is recorded.
    - `PRESENCE`: only that the argument was passed.
    - `HASH`: a keyed hash, which shows whether two calls passed the same
      value.
    - `FINGERPRINT`: the hash plus a keyed fingerprint, which shows how
      similar two values are without exposing either.
    - `VALUE`: the raw value, when it is in the hint's closed set or a bounded
      scalar of the hinted type; anything else is recorded as `HASH`.

    The hashed modes need `ToolTracingConfig.arg_key` and a verified caller, and
    record presence without them.
    """

    OMIT = "omit"
    PRESENCE = "presence"
    HASH = "hash"
    FINGERPRINT = "fingerprint"
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class ToolTracingConfig:
    """Configuration for OpenTelemetry tool-call tracing.

    Every attribute the layer writes is namespaced under `attribute_prefix`.
    Values supplied through `attributes`, `shared_properties`, a per-tool
    callable, or `add_trace_attributes()` are bounded: strings are stripped,
    must be printable, and are cut to 256 characters; `bool`, `int`, and
    finite `float` pass; anything else is dropped.

    When an SDK `TracerProvider` is already installed, the layer attaches to
    it. That provider's sampler then decides which calls are traced (the SDK
    default drops a call whose client sends an unsampled `traceparent`), and
    its own exporters receive FastMCP's unfiltered spans: the allowlist
    applies only to the exporter configured here.

    Attributes:
        exporter: `"otlp"` exports over OTLP/HTTP only when
            `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`
            is set, and otherwise stays dormant. `"console"` writes to stderr.
            A `SpanExporter` instance is used as given and receives spans
            after the privacy boundary.
        attribute_prefix: Namespace for every attribute the layer writes. Its
            first segment cannot be a namespace FastMCP or OpenTelemetry
            writes, such as `mcp`, `fastmcp`, `gen_ai`, or `http`.
        attributes: Extra per-call attributes for the span only: a mapping, or
            a callable (sync or async) resolved after the tool returns or
            raises, when the event's `extra_properties` are resolved too. It
            runs before the response is sent, so keep it fast; an async hook
            is abandoned after five seconds.
        shared_properties: Names of `TelemetryConfig.extra_properties` keys to
            write to the span as well, so a property is set once. Only the
            named keys are copied: event properties can hold values that must
            not reach a trace backend.
        capture_intent: Adds an optional `intent` string argument to every
            tool schema, appends one sentence to the server instructions,
            records the argument, and strips it before the tool runs. A tool
            that declares its own `intent` parameter keeps it, and it is not
            recorded unless `record_declared_intent` is set.
        record_declared_intent: Records the `intent` argument of a tool that
            declares its own `intent` parameter, bounded like a captured
            intent. The tool still receives the argument unchanged.
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
        session_id: A host hook returning a SHA-256 hex digest of the host's
            session key. The digest is used as-is and not hashed again; a
            missing, non-digest, or raising result falls back to the transport
            session identifier.
        require_own_provider: Export only through a `TracerProvider` created by
            this package. A provider this package created earlier is also owned.
    """

    exporter: Literal["otlp", "console"] | SpanExporter = "otlp"
    attribute_prefix: str = "fastmcp_extensions"
    attributes: (
        Mapping[str, object]
        | Callable[[], Mapping[str, object] | Awaitable[Mapping[str, object]]]
        | None
    ) = None
    shared_properties: Sequence[str] = ()
    capture_intent: bool = False
    record_declared_intent: bool = False
    error_classifier: Callable[[BaseException], str | None] | None = None
    other_spans: Callable[[ReadableSpan], Mapping[str, object] | None] | None = None
    arg_key: bytes | Callable[[], bytes | None] | None = field(default=None, repr=False)
    arg_default: TraceArg = TraceArg.HASH
    session_id: Callable[[], str | None] | None = None
    require_own_provider: bool = False
