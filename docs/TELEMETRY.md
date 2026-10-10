# Tool-Call Tracing

FastMCP opens an OpenTelemetry span for every request, but nothing exports it
until an SDK is installed. Tracing is part of telemetry:
`TelemetryConfig(tool_tracing=...)` installs the SDK, adds what an operator needs to
FastMCP's own `tools/call` span, and rebuilds every span from an allowlist
before it is exported. Install the `otel` extra and turn it on:

```bash
pip install "fastmcp-extensions[otel]"
```

```python
from fastmcp_extensions import TelemetryConfig, mcp_server

app = mcp_server(
    display_name="orders-mcp", telemetry=TelemetryConfig(tool_tracing=True)
)
```

Tracing is off by default, and `telemetry=False` or
`TelemetryConfig(enabled=False)` turns it off with the rest of telemetry. Once
enabled, spans are exported over OTLP/HTTP when
`OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set;
until then tracing stays dormant. The OpenTelemetry SDK reads its own `OTEL_*`
exporter and resource variables as usual (for sampling, see
[Sampling and existing providers](#sampling-and-existing-providers)).
`DO_NOT_TRACK` switches off everything that leaves the process (Sentry,
Segment, and trace export); the structured log line stays. One log line at
startup says what tracing is doing, for example
`tracing: exporter=otlp provider=created` or
`tracing: dormant (no OTLP endpoint set)`. Tracing never raises into a tool
call and never breaks server startup. Servers built without `mcp_server()` call
`register_tool_call_telemetry(app, TelemetryConfig(tool_tracing=True))`, which
is idempotent.

## One call, one event, one span

Telemetry emits one event per tool call (log line, Sentry breadcrumb, Segment)
and tracing exports one span. Both are derived from the same facts about the
call, so they agree on the tool, the outcome, the error type, and the error
category. This holds for the order `mcp_server()` and
`register_tool_call_telemetry()` set up; a telemetry middleware added by hand
after tracing does not share the span's facts. The two can be joined:

- The event carries `trace_id` and `span_id` of the span that traced the call.
  An untraced call has neither.
- With `anonymization_salt` set, the span carries the event's `caller_hash` and
  `caller_id_type`.
- `extra_properties` is resolved once per call. Name a key in
  `ToolTracingConfig(shared_properties=...)` to write it to the span as well; keys
  that are not named stay off the span.

The two use different names for some of the same facts, because each matches
dashboards that already exist:

| Event field | Span attribute |
| ----------- | -------------- |
| `name` | `gen_ai.tool.name` |
| `success`, `outcome` | `<p>.outcome` (`success`, or one of four failure outcomes) |
| `error_type` | `<p>.error_type`, `error.type` |
| `error_category`, `error_fault` | `<p>.error.category`, `<p>.error.fault` |
| `upstream_status_code` | `<p>.upstream.status_code` |
| `error_cause_types` | `<p>.error.cause_types` |
| `error_reason` | `<p>.error.reason` |
| — | `<p>.error.stack` |
| `tool_group` | `<p>.tool_module` |
| `mutation_class` | `<p>.tool_mutating`, `<p>.tool_destructive` |
| `mcp_client_name`, `mcp_client_version` | `<p>.client_name`, `<p>.client_version` |
| `package_version` | `service.version` (resource) |

## What a span carries

Each tool call exports one SERVER span named `tools/call <tool>`. `<p>` is the
`attribute_prefix`:

| Attribute | Value |
| --------- | ----- |
| `gen_ai.tool.name`, `gen_ai.operation.name` | The registered tool name and `execute_tool` |
| `mcp.method.name`, `fastmcp.server.name` | `tools/call` and the server name |
| `mcp.protocol.version`, `<p>.mcp_protocol_version` | The negotiated protocol version, when it is a date |
| `<p>.outcome` | `success`, `tool_error` (a returned error), `exception`, `cancelled`, or `unknown_tool` |
| `<p>.error_type`, `error.type` | Class name of the real cause, with FastMCP's `ToolError` wrapper removed |
| `<p>.error.category`, `<p>.error.fault` | A closed category such as `invalid_arguments`, `auth`, or `upstream_timeout`, and who is at fault: `caller`, `upstream`, `server`, or `unknown` |
| `<p>.error.cause_types` | Class names of the exceptions chained behind the cause (`raise ... from`), at most four |
| `<p>.error.reason` | With `error_reason`: the slug the hook returned for the failure, such as an upstream API's error code |
| `<p>.error.stack` | Module/function/line frames only; no exception messages. Also attached to the exception event as `exception.stacktrace` |
| `<p>.upstream.status_code` | The HTTP status the failure carries, if any (100 to 599) |
| `<p>.tool_requested_name` | For an unknown tool: the requested name if it is well formed, else `<other>` |
| `<p>.client_name`, `<p>.client_version` | The MCP client |
| `<p>.caller_hash`, `<p>.caller_id_type` | The caller as telemetry's salted hash, and whether it is a `subject` or a `client`; only with `anonymization_salt` set |
| `<p>.session_id`, `mcp.session.id`, `gen_ai.conversation.id` | SHA-256 of the `Mcp-Session-Id` header, never the raw value; on stdio, a random per-process digest |
| `<p>.root` | `False` for a tool called by another tool through `app.call_tool()` |
| `jsonrpc.request.id`, `gen_ai.tool.call.id` | Root spans only: the request ID if it is an integer or a short token, and its SHA-256 |
| `<p>.tool_module`, `<p>.tool_mutating`, `<p>.tool_destructive` | From the tool's registration |
| `<p>.tool.fingerprint` | Hash of the tool's name, description, schemas, and annotations |
| `<p>.args.supplied`, `<p>.args.unknown`, `<p>.args.invalid` | Argument names the caller sent, names it invented, and `<name>:<pydantic error type>` for validation failures |
| `<p>.arg.<name>` | One record per argument; see [Per-argument declarations](#per-argument-declarations) |
| `<p>.arg_hash_status`, `<p>.arg_key_scope`, `<p>.arg_scope_id`, `<p>.arg_trace_dropped` | How the argument records were keyed, and how many were dropped at export |
| `<p>.intent`, `<p>.intent_present` | With `capture_intent`: the agent's stated reason, cut to 4096 characters. Not recorded for a tool that declares its own `intent` parameter |
| `<p>.result.*` | Result shape: content count and types, text and structured sizes, item count |
| `<p>.eval.run_id`, `<p>.eval.case_id` | From the `X-MCP-Eval-Run` and `X-MCP-Eval-Case` request headers |
| `<p>.process.uptime_s` | Seconds since tracing was installed |
| other `<p>.*` | Attributes from the hooks, a per-tool callable, or `add_trace_attributes()` |

`<p>.error.category` is one of fifteen values, grouped here by
`<p>.error.fault`. `caller`: `invalid_arguments`, `unknown_tool`, `user_error`,
`auth`, `not_found`, `tool_unavailable`. `upstream`: `rate_limited`,
`upstream_error`, `upstream_unreachable`, `upstream_timeout`. `unknown`:
`timeout`, `cancelled`, `tool_error`, `unclassified`. `server`: `internal`. A
tool-filter rejection is `tool_unavailable`. A failure no rule recognises is
`unclassified` with fault `unknown`; `internal` (fault `server`) appears only
when `error_classifier` returns it.

`tools/list` requests export a span with `<p>.tools.count`,
`<p>.tools.schema_chars`, and `<p>.tools.set_fingerprint`. The character count
excludes the `intent` argument that `capture_intent` adds to each tool.

Everything else is dropped: results, exception messages, stack traces, raw
session IDs, `enduser.*`, and argument values other than those recorded as
`VALUE`. Spans the layer did not stamp, such as HTTP client spans, are dropped
unless `other_spans` keeps them.

A client's trace context is not trusted: a root span is exported without a
parent and without the client's `tracestate`. The trace ID a client sends in
`_meta.traceparent` is kept, so a client chooses which trace its spans join.

## Sampling and existing providers

When no OpenTelemetry SDK `TracerProvider` is installed, the layer creates one
that samples every call. `OTEL_TRACES_SAMPLER` is ignored, so a client cannot
switch tracing off with an unsampled `traceparent`.

To sample, install your own SDK `TracerProvider` before calling `mcp_server()`.
The layer attaches to it (the startup line then says `provider=existing`), and
two things change:

- That provider's sampler decides which calls are traced. The SDK default,
  `ParentBased`, drops a call whose client sends an unsampled `traceparent`;
  build it with `remote_parent_not_sampled=ALWAYS_ON` to keep those calls.
- That provider's own exporters receive FastMCP's unfiltered spans. The
  allowlist applies only to the exporter configured here.

## Options

Pass a `ToolTracingConfig` instead of `True`:

```python
import os

from fastmcp_extensions import TelemetryConfig, ToolTracingConfig, mcp_server

app = mcp_server(
    display_name="orders-mcp",
    telemetry=TelemetryConfig(
        tool_tracing=ToolTracingConfig(
            attribute_prefix="acme.mcp",
            attributes={"deployment": "prod"},
            capture_intent=True,
            arg_key=lambda: bytes.fromhex(os.environ["ORDERS_MCP_ARG_KEY"]),
        ),
    ),
)
```

| Field | Default | Meaning |
| ----- | ------- | ------- |
| `exporter` | `"otlp"` | `"otlp"`, `"console"` (writes to stderr), or a `SpanExporter` instance, which receives spans after the privacy boundary. |
| `attribute_prefix` | `"fastmcp_extensions"` | Namespace for every attribute the layer writes. Its first segment cannot be a namespace FastMCP or OpenTelemetry writes (`mcp`, `fastmcp`, `gen_ai`, `enduser`, `error`, `exception`, `jsonrpc`, `rpc`, `http`, `url`), so `acme.mcp` is fine and `mcp.acme` disables tracing with a warning. |
| `attributes` | `None` | Extra per-call attributes for the span only: a mapping, or a zero-argument callable, sync or async, resolved after the tool returns or raises. It runs before the response is sent, so keep it fast; an async hook is abandoned after five seconds. |
| `shared_properties` | `()` | Names of `TelemetryConfig.extra_properties` keys to write to the span as well. Only named keys are copied. |
| `capture_intent` | `False` | Adds an optional `intent` string argument to every tool schema and one sentence to the server instructions, records the argument, and strips it before the tool runs. A tool that declares its own `intent` parameter keeps it, and it is not recorded. |
| `error_classifier` | `None` | `(exception) -> category` override for the span and the event; ignored unless it returns a known category. |
| `error_reason` | `None` | `(exception) -> slug` naming why the call failed, for the span and the event. Exported only when it is lowercase letters, digits, and `:._-`, at most 100 characters. An ID fits that pattern, so return values from a fixed vocabulary only. |
| `other_spans` | `None` | `(span) -> attributes` for spans the layer did not stamp. Returns the complete attribute set to keep, or `None` to drop the span. A kept span's name, kind, timing, and parent are exported unchanged, so return `None` for spans whose name may carry data, such as a SQL statement or a client-chosen prompt name. The hook sees every unstamped span in the process, so set it on only one app per process. |
| `arg_key` | `None` | 32-byte secret for argument hashes, or a callable returning it. |
| `arg_default` | `TraceArg.HASH` | How `str`, `int`, `float`, `UUID`, and `list[str]` arguments without a marker are recorded. `VALUE` is treated as `HASH`. |

Attributes from `attributes`, `shared_properties`, a per-tool callable, or
`add_trace_attributes()` are written under the prefix and bounded: strings are
stripped, must be printable, and are cut to 256 characters; `bool`, `int`, and
finite `float` pass; anything else is dropped. Keys must match
`[a-z0-9_]+(\.[a-z0-9_]+)*`. Keys the layer owns are dropped silently, and so
is any key whose first segment is `arg`, `args`, `error`, `eval`, `process`,
`result`, `tool`, `tools`, or `upstream` (`result` and `result.rows` alike).
At export, `<p>.error.*` and `<p>.upstream.*` are a closed group: only the keys
in the table above survive, only with valid values, and only on a failed call.

An `error_classifier` that maps a server's own exception types to categories:

```python
CATEGORIES = {WorkspaceNotSelectedError: "user_error", BillingError: "internal"}

ToolTracingConfig(
    error_classifier=lambda exc: next(
        (category for kind, category in CATEGORIES.items() if isinstance(exc, kind)),
        None,
    )
)
```

## Per-tool declarations

```python
from fastmcp_extensions import add_trace_attributes, mcp_tool


@mcp_tool(read_only=True, tracing=False)  # no span for this tool
def whoami() -> str:
    return "me"


# The callable receives the call's arguments and returns extra attributes.
@mcp_tool(destructive=True, tracing=lambda args: {"dry_run": bool(args.get("dry_run"))})
def delete_order(order_id: str, dry_run: bool = False) -> str:
    add_trace_attributes({"orders_deleted": 0 if dry_run else 1})
    return order_id
```

`add_trace_attributes()` targets the span of the tool call it runs in, also for
nested calls. It never raises and does nothing outside a traced call.

To record why a call failed, set `error_reason` to map the exception to a slug
(for example an upstream API's error code); it is exported as `<p>.error.reason`
and as the event's `error_reason`. For a reason only the tool knows, call
`add_trace_attributes({"failure_reason": "workspace_not_selected"})` before
raising. That value is the server's own text, bounded like every other
attribute, and reaches the span only.

`tracing=` and `TraceArg` markers apply on the app the tool is registered on.
A tool that reaches a traced app through `mount()` or a proxy is traced with
the defaults: a `tracing=False` or `tracing=` callable declared on its own
server is ignored, and each of its arguments is recorded as `PRESENCE`.
`trace_plan(app)` shows what applies. If the mounted server enables tracing
too, it exports its own span for the call under the parent's, with
`<p>.root = False`, like a nested call; count calls by `<p>.root = True`.

## Per-argument declarations

Every argument is recorded as one small JSON record that says something about
the value without containing it, so a trace can tell "the agent retried with
the same input" from "the agent changed its input". Type hints pick the mode;
a `TraceArg` marker inside `Annotated[...]` overrides it:

```python
from typing import Annotated

from fastmcp_extensions import TraceArg, mcp_tool


@mcp_tool(read_only=True)
def query(
    sql: str,
    note: Annotated[str, TraceArg.OMIT] = "",
    limit: Annotated[int, TraceArg.VALUE] = 20,
) -> list[dict]:
    return []
```

| Mode | Record | Default for |
| ---- | ------ | ----------- |
| `VALUE` | `{"value": v}` | `bool`, `Literal`, `Enum`, and lists of them |
| `HASH` | `{"digest": h}`, a keyed hash; lists add `"count"` | `str`, `int`, `float`, `UUID`, `list[str]` (set by `arg_default`) |
| `FINGERPRINT` | `HASH` plus `"similarity"`, a keyed fingerprint that shows how similar two short texts are without exposing either | Opt-in only |
| `PRESENCE` | `{"present": true}` | `dict`, pydantic models, `Any`, unhinted arguments, and names containing `token`, `secret`, `password`, `credential`, `api_key`, `access_key`, `private_key`, `authorization`, or `session_state` (underscores and case are ignored, so `apiKey` matches) |
| `OMIT` | Nothing | pydantic `SecretStr` / `SecretBytes` |

- `VALUE` exports the raw value, so mark only arguments that are safe to read
  in a trace. Only a member of the closed set, or a bounded value of the hinted
  type, is exported; anything else falls back to `HASH`.
- Hashes need `arg_key` and a caller identified by verified token claims. They
  are scoped to that caller and session, so they compare only within one
  scope. Without either, hashed modes record `{"present": true}`;
  `<p>.arg_hash_status` says which applied (`ok`, `no_key`, `no_scope`, `error`).
- Import `TraceArg` at runtime, not under `TYPE_CHECKING`. A marker that cannot
  be resolved turns every argument of that tool into `PRESENCE`, with a
  warning.
- Records are checked again at export: one that does not match its argument's
  mode is dropped and counted in `<p>.arg_trace_dropped`.

## Testing

`capture_tool_spans()` yields spans as they would be exported, after the
privacy boundary; it works with an in-memory `Client(app)` and needs no
endpoint. The list also holds `tools/list` spans (a FastMCP `Client` lists
tools on its own) and any span `other_spans` keeps, so select by `span.name`.
`trace_plan(app)` returns what is recorded for each tool, so a
snapshot of it makes every change to what leaves the process show up in review.
For a tool registered in several versions, it covers only the newest:

```python
import pytest
from fastmcp import Client

from fastmcp_extensions import capture_tool_spans, trace_plan


@pytest.mark.asyncio
async def test_query_span():
    with capture_tool_spans() as spans:
        async with Client(app) as client:
            await client.call_tool("query", {"sql": "select 1", "limit": 5})
    span = next(span for span in spans if span.name == "tools/call query")
    assert span.attributes["acme.mcp.outcome"] == "success"
    assert span.attributes["acme.mcp.arg.limit"] == '{"value":5}'


@pytest.mark.asyncio
async def test_trace_plan():
    plan = await trace_plan(app)
    assert plan["query"]["args"] == {
        "limit": "value int",
        "note": "omit",
        "sql": "hash",
    }
```
