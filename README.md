# <p align="center">🚀 FastMCP Extensions 🚀</p>

🧩 _The paved road on top of FastMCP. Wire the hard parts once, reuse them on every server you ship._

## What It Adds Over Baseline FastMCP

Baseline [FastMCP](https://github.com/jlowin/fastmcp) is the protocol engine: it gives you the machinery to register tools, prompts, and resources and to speak MCP over stdio or HTTP. This library encodes _how you actually ship_ an MCP server, so each new one inherits the hardening instead of reinventing it:

1. 🔐 **Auth wired once, reused everywhere** - `build_mcp_auth()` is a pure, typed factory that assembles the right verifier — or a `MultiAuth` when several apply — from explicit configs: interactive OIDC for humans (browser Auth Code + PKCE), headless JWT for machines and agents, and opaque-token introspection. Harden it in one place and every server benefits. See [Authenticating an MCP Server](#authenticating-an-mcp-server).
2. 🧯 **Secure, predictable defaults** - The auth factory reads **no environment variables**: each server owns its own env-var names and can validate a complete configuration before building the provider. Refresh-token storage is injectable, so a server can use a durable, shared backend across restarts and replicas without the library owning your database.
3. 🕵️ **Credential hygiene when you wire it in** - An installable redaction filter scrubs bearer tokens and other credential values from controlled log records, while one-way key normalization makes arbitrary client IDs and other store keys legal for durable backends.
4. 🎚️ **Tool filtering from MCP annotations** - Read-only mode, no-destructive mode, and module/tool exclusion use MCP tool annotations (`readOnlyHint`, `destructiveHint`, …) and request/server configuration. Filters compose with logical AND, so layering can only narrow the surface, never widen it. See [Tool Filtering](#tool-filtering).
5. 🧩 **MCP Apps UI support without per-server wiring** - Link a tool to a UI resource with `app=AppConfig(...)`, opt into the standard filters, and the library hides it from clients that cannot render MCP Apps UI (detected via the standard `_meta.ui` marker). `run_mcp_http_server()` carries the client's extension declaration through stateless HTTP automatically. See [MCP Apps UI support](#mcp-apps-ui-support).
6. 🛡️ **Capability gating, safe in local _and_ hosted deploys** - The standard `capability_filter` hides tools whose built-in or deployment-defined `required_capabilities` are unavailable. Custom capability resolvers fail closed and cannot override built-in capabilities; the filesystem gate is forced off under HTTP regardless of configuration. Call `assert_http_trusted_execution_disabled()` at HTTP startup to fail loudly on an unsafe configuration.
7. 🧵 **Deferred registration, solved** - `@mcp_tool` / `@mcp_prompt` / `@mcp_resource` tag tools, prompts, and resources into a registry (auto-detecting the domain from the file stem), and the domain-filtered `register_*` functions register them in one call — organize by domain without fighting import order.
8. 🏭 **A server factory with fewer moving parts** - `mcp_server()` hands you a FastMCP instance that already has a server-info resource, optional asset discovery, and credential resolution from HTTP headers or env vars via `get_mcp_config` — typed pieces instead of hand-wired boilerplate.
9. 🖥️ **One codebase, two front-ends** - `cli_app()` is the CLI counterpart of `mcp_server()`: shared tool functions and the same telemetry sinks can power both surfaces. Write a tool once; call it from the command line and expose it over MCP.
10. 📖 **Auto-generated docs for every tool** - A Markdown docs generator (Docusaurus- and pdoc-compatible) renders your tool surface from the source of truth, giving every tool its own URL anchor to share with stakeholders. Documenting and announcing changes stops being a manual step.
11. 📈 **Telemetry that's free until you want it** - Sentry, Segment, and structured-log sinks record timing, success, and error type across both MCP and CLI paths. Sentry and Segment are no-ops unless you supply their keys, so the telemetry wiring can ship in the base template.
12. 🌐 **Browser-friendly landing page** - A registrable landing page so a browser `GET` on your MCP HTTP endpoint returns something human-readable instead of an error.
13. 🧪 **Test and debug tooling** - `call_mcp_tool` / `run_tool_test` / `run_http_tool_test` exercise tools with JSON args over stdio and HTTP, and tool-list measurement catches context-window truncation before it bites an agent.
14. 🔭 **Tracing behind an allowlist** - `TelemetryConfig(tool_tracing=True)` exports one OpenTelemetry span per tool call with its outcome, error category, client, and argument and result shape. Every span is rebuilt from an allowlist before export, so results and exception messages stay in the process. A value leaves only for an argument recorded as `VALUE` and for the opt-in `intent` text. See [Tool-Call Tracing](#tool-call-tracing).
15. 🧱 **A buffer against major-version churn** - Servers build against this library's API, not FastMCP's internals, so a FastMCP major bump lands here first. Through the 2.x→3.x transition this library supported both lines during the overlap and the servers on top needed little or no rework; it now targets FastMCP 4.x, having absorbed the 3.x→4.x move the same way.

## Upgrading to 0.x (FastMCP 4)

This release requires **FastMCP 4.x** (`fastmcp>=4.0.9`), which itself requires
`mcp` 2.x — the dependency floor moved, so upgrade both together. For servers
built on this library, the changes are mostly internal:

- **Custom annotation keys are gone from the wire.** `mcp` 2.x
  `ToolAnnotations` drops unknown keys, and we no longer emit custom keys
  anywhere: `annotations=` accepts only the standard spec hints (anything
  else raises `ValueError` — use `meta=` for deliberate custom wire
  metadata). `mcp_module` and `requires_client_filesystem` are now internal
  registration-time traits, readable in-process via
  `get_tool_traits(app, tool_name)` (`ToolTraits.mcp_module` /
  `ToolTraits.required_capabilities`). All custom keys are declared in
  `fastmcp_extensions.annotations`. UI tools are detected via the standard
  `_meta.ui` marker written by FastMCP's `AppConfig`, and `mcp_tool` accepts
  `meta=` / `app=` / `required_capabilities=` directly.
- **`exclude_args` still works.** `register_mcp_tools(..., exclude_args=[...])`
  hides parameters from the tool schema exactly as before (FastMCP 4 removed
  the underlying kwarg; the library emulates it via dependency injection).
- **MCP 2026-07-28 is served natively.** FastMCP 4 / `mcp` 2.x answer
  `server/discover` and still serve legacy `initialize`-handshake clients
  (2024-11-05 through 2025-11-25) on the same endpoint — no middleware needed
  for modern or legacy clients.

See the [FastMCP 3→4 upgrade guide](https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3)
for changes that may touch your own FastMCP API usage outside this library.

### Philosophy

**Opinionated on purpose.**

1. A CLI and an MCP server are two front-ends over one shared body of code, not two implementations that drift.
2. Auth, filtering, telemetry, docs, and testing scaffolding are wired once and inherited.
3. We want a more capable MCP server implementation as baseline - with fewer footguns and less repeated code.

## Installation

```bash
pip install fastmcp-extensions
```

Or with uv:

```bash
uv add fastmcp-extensions
```

## Quick Start

### Using the MCP Server Factory

The `mcp_server` function creates a FastMCP instance with built-in server info resources and optional credential resolution. `package_name` is the installed distribution used for version reporting; when omitted, it is derived from the calling module's installed distribution.

```python
from fastmcp_extensions import mcp_server, MCPServerConfigArg

app = mcp_server(
    display_name="my-mcp-server",
    package_name="my-package",
    advertised_properties={
        "docs_url": "https://github.com/org/repo",
        "release_history_url": "https://github.com/org/repo/releases",
    },
    server_config_args=[
        MCPServerConfigArg(
            name="api_key",
            http_header_key="X-API-Key",
            env_var="MY_API_KEY",
            required=True,
            sensitive=True,
        ),
    ],
)

# Server info resource is automatically registered at {name}://server/info
# Get credentials from HTTP headers or environment variables
from fastmcp_extensions import get_mcp_config

api_key = get_mcp_config(app, "api_key")
```

### Using Annotation Constants

```python
from fastmcp_extensions import (
    READ_ONLY_HINT,
    DESTRUCTIVE_HINT,
    IDEMPOTENT_HINT,
    OPEN_WORLD_HINT,
)

# Use in tool annotations
annotations = {
    READ_ONLY_HINT: True,
    IDEMPOTENT_HINT: True,
}
```

### Using Deferred Registration

```python
from fastmcp import FastMCP
from fastmcp_extensions import (
    mcp_tool,
    mcp_resource,
    register_mcp_tools,
    register_mcp_resources,
)


# Define tools with the decorator (domain auto-detected from filename)
@mcp_tool(read_only=True, idempotent=True)
def list_items() -> list[str]:
    """List all available items."""
    return ["item1", "item2"]


@mcp_resource("myserver://version", "Server version", "application/json")
def get_version() -> dict:
    """Get server version info."""
    return {"version": "1.0.0"}


# Register with FastMCP app
app = FastMCP("my-server")
register_mcp_tools(app)
register_mcp_resources(app)
```

### Measuring Tool List Size

```python
import asyncio
from fastmcp_extensions.utils.describe_server import measure_tool_list_detailed


async def check_tool_size():
    measurement = await measure_tool_list_detailed(app, server_name="my-server")
    print(measurement)
    # Output:
    # MCP Server: my-server
    # Tool count: 10
    # Total characters: 5,432
    # Average chars per tool: 543


asyncio.run(check_tool_size())
```

### Testing Tools

```python
from fastmcp_extensions.utils.test_tool import call_mcp_tool, run_tool_test
import asyncio

# Call a tool programmatically
result = asyncio.run(call_mcp_tool(app, "list_items", {}))

# Or use the CLI helper
run_tool_test(app, "list_items", "{}")
```

### Getting Prompt Text

```python
from fastmcp_extensions.prompts import get_prompt_text
import asyncio

# Get prompt text for agents that can't access prompts directly
text = asyncio.run(get_prompt_text(app, "my_prompt", {"arg": "value"}))
```

### Authenticating an MCP Server

MCP servers built on this library should not talk to an identity provider or
manage token lifecycles themselves. They only declare **which verifier(s) they
trust**; FastMCP verifies the `Authorization: Bearer <token>` on every request.
Minting tokens is the client's job. This library owns the assembly.

The entry point is `build_mcp_auth()`: a **pure, typed** factory that assembles
an `AuthProvider | None` from explicit config objects (return `None` = run
unauthenticated, e.g. local stdio). It reads **no environment variables** — the
server owns its own env-var names (whatever branding it prefers) and maps them
into the configs, so this library never imposes a naming scheme or a backend:

```python
import os

from fastmcp_extensions import (
    JWTAuthConfig,
    OIDCAuthConfig,
    build_mcp_auth,
    mcp_server,
)

app = mcp_server(display_name="my-mcp-server", package_name="my-package")

# The server decides its env-var names and maps them into typed configs. Read
# every field with os.getenv and only build the config once all are present, so
# a partially-configured deployment never raises a KeyError.
config_url = os.getenv("MY_OIDC_CONFIG_URL")
client_id = os.getenv("MY_OIDC_CLIENT_ID")
client_secret = os.getenv("MY_OIDC_CLIENT_SECRET")
base_url = os.getenv("MY_MCP_SERVER_URL")

oidc = None
if config_url and client_id and client_secret and base_url:
    oidc = OIDCAuthConfig(
        config_url=config_url,
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
    )

app.auth = build_mcp_auth(
    oidc=oidc,  # interactive humans (browser Auth Code + PKCE), optional
    jwt=JWTAuthConfig(  # headless machines / agents, optional
        jwks_uri="https://idp.example/.well-known/jwks.json",
        issuer="https://idp.example/",
        audience="my-api",
    ),
)
```

`build_mcp_auth()` understands three transport-auth modes and combines any that
are configured via FastMCP's `MultiAuth`:

| Mode | Who it's for | Config object |
| ---- | ------------ | ------------- |
| Interactive OIDC (`OIDCProxy`) | humans (browser Auth Code + PKCE) | `OIDCAuthConfig(config_url, client_id, client_secret, base_url, ...)` |
| Headless JWT (`JWTVerifier`) | machines / agents | `JWTAuthConfig(...)` with either `jwks_uri=...` or `public_key=...`, plus `issuer` / `audience` / `algorithm` |
| Opaque-token introspection (`IntrospectionTokenVerifier`) | machines with opaque tokens | `IntrospectionAuthConfig(introspection_url, client_id, client_secret)` |

`static_tokens=`, `base_url=`, and `required_scopes=` round out the parameters.
It returns a single verifier when one is configured, or a `MultiAuth` when
several are.

#### Trusting several realms / user tokens

`jwt=` also accepts a sequence of `JWTAuthConfig`s — one verifier per entry,
combined via `MultiAuth` — so a server can trust several issuers or realms at
once. A typical pairing is an application-token realm (client-credentials
tokens) plus a user-token realm pinned with `allowed_client_ids`:

```python
jwt = [
    # Application tokens minted via the client credentials grant:
    JWTAuthConfig(
        jwks_uri="https://app-realm.example/jwks", issuer="https://app-realm.example/"
    ),
    # Interactive user/session tokens, pinned by `azp` (aud varies per client):
    JWTAuthConfig(
        jwks_uri="https://user-realm.example/jwks",
        issuer="https://user-realm.example/",
        allowed_client_ids=frozenset({"my-webapp-client"}),
    ),
]
```

The second config builds a `ClientAllowlistJWTVerifier`: signature and issuer
are checked as usual, and the token is then rejected unless its `azp` claim
names an allowlisted client. Use this for user/session tokens from
interactive clients — client-credentials tokens don't carry `azp`, so leave
`allowed_client_ids` unset for them.

For a durable, shared interactive-OIDC store (so refresh tokens
survive restarts and span replicas), the server constructs its own backend and
injects it via `OIDCAuthConfig(client_storage=...)` — keeping all
backend-specific config (project, database, encryption) in the deployment, not
in this library.

**Consent screen.** `OIDCProxy` shows its own consent page before redirecting to
the IdP, rendered from the server's `name`, `icons[0]`, and `website_url` and
otherwise not themeable. When the IdP already collects consent on a branded
login page, that is two prompts in a row — pass
`OIDCAuthConfig(require_authorization_consent="external",
extra_authorize_params={"prompt": "consent"})` to skip the built-in page and let
the IdP prompt instead of silently reusing an existing session.

**Custom proxy class.** When a deployment has to change how the proxy *behaves*
— its `authorize` redirect, extra routes, per-request upstream endpoints, or
token verifier — rather than a constructor setting, subclass `OIDCProxy` and
pass `OIDCAuthConfig(proxy_factory=functools.partial(MyProxy, extra=...))`.
`build_mcp_auth` still does the kwarg mapping, `client_storage` handling, and
`MultiAuth` assembly; the factory only replaces the final `OIDCProxy(...)` call
and receives the same kwargs.

**Client side.** A headless client mints its own short-lived bearer token and
sends it as `Authorization: Bearer <token>`; use
`fetch_client_credentials_token(ClientCredentials(...))` for an OAuth 2.0
client-credentials grant. Nothing is stored server-side — no refresh-token
state. If the token the client mints is also a valid credential for a downstream
API (i.e. the verifier points at that API's issuer), the server can reuse the
verified token as the downstream bearer via FastMCP's `get_access_token()` — one
token doing both transport auth and downstream authorization.

## MCP Apps UI support

MCP Apps UI support is a tool-visibility gate for servers that expose
interactive renderings. Link a tool to a UI resource with `app=` and enable
the standard filters:

```python
from fastmcp.apps import AppConfig

from fastmcp_extensions import mcp_server, mcp_tool, register_mcp_tools

app = mcp_server(
    display_name="my-server",
    include_standard_tool_filters=True,
)


@mcp_tool(
    interactive_ui=True,
    app=AppConfig(resource_uri="ui://my-server/dashboard.html"),
)
def show_dashboard() -> str:
    """Return data for an interactive dashboard."""
    return "dashboard data"


register_mcp_tools(app)
```

The standard `interactive_ui_filter` leaves ordinary tools visible and hides
tools carrying the `_meta.ui` marker from clients that did not declare the
`io.modelcontextprotocol/ui` extension. This is a rendering-capability check,
not a privilege boundary: extension declarations are client-controlled and
must never be used to grant authority.

## Stateless HTTP capability carry-through

**The problem.** A client declares its extensions once, during `initialize`. A
stateless HTTP server builds a fresh session per request and discards it, so by
the time `tools/list` arrives that declaration is gone. The UI gate would
therefore hide interactive tools from every client, including the ones that can
render them.

**The protocol rule we use.** The streamable HTTP transport (revision
2025-03-26) says a server MAY return an `Mcp-Session-Id` header on its response
to `initialize`, and that a client receiving one MUST include it on every
subsequent request. That MUST is the only client-side behavior this relies on.

**What we put in it.** The spec makes the session ID server-assigned and opaque
to the client, and says nothing about its contents — so we make it carry the
data instead of pointing at it. `run_mcp_http_server()` encodes the declared
extensions into the ID it returns, and decodes them from the header on each
later request. The client stores the state; the server keeps none, which means
no session table, no sticky routing, and nothing lost across restarts or
replicas.

The value is `<uuid4>.<base64url payload>`: a random component for the
uniqueness the spec asks of session IDs, then the encoded extension IDs. The
encoding is not decoration — the spec restricts the value to visible ASCII
(0x21–0x7E), so arbitrary payloads have to be encoded to be legal.

The ID is minted once, on the `initialize` response, and never reissued: the
middleware sets the header only for that one exchange. So this carries state
fixed at session start, not state that changes call to call — a server wanting
the latter cannot get it by handing back a new ID, because clients capture the
session ID at initialize and are under no obligation to notice a later one.

**Clients that do not echo it.** Set the `X-MCP-Extensions` header on each
request, listing extension IDs separated by commas or whitespace. This is our
own header rather than a protocol feature — an escape hatch for clients that do
not implement session IDs.

Both paths are unauthenticated client statements, so gate rendering with them,
never authority.

Goose Desktop is verified against this end to end: it declares the UI extension
at `initialize`, echoes the session ID, and renders interactive tools over
stateless HTTP with no custom configuration.

This mechanism has a known end date. MCP revision 2026-07-28 removes protocol
sessions and carries client capabilities in per-request `_meta`, which will
replace both paths above once the stack supports it.

The capability declaration is settled at `initialize`; encoded session state
is the complementary mechanism for mutable tool state that changes on every
call.

## Encoded session state

Stateful tools can carry mutable state across stateless MCP calls without a
server-side store. Define a `ToolStateBase` with the fields your tool needs,
then annotate the tool with `with_state`:

```python
from fastmcp_extensions import ToolStateBase, mcp_tool


class BasketState(ToolStateBase):
    count: int = 0


@mcp_tool(with_state=BasketState)
def add_item(item: str, *, state_handle: BasketState) -> str:
    state_handle.count += 1
    return f"{item} #{state_handle.count}"
```

The advertised input is `encoded_session_state: str | None`, and the named
result includes both `result` and a fresh `encoded_session_state`. Pass the
returned handle to the next call; omitting it creates a fresh state. The
injected `state_handle` is mutated in place and re-encoded after every response;
reassigning the parameter inside the tool body silently discards that change.

Each handle is bound to the qualified type of the state that was used to mint
it. Multiple tools can freely share one state type, while passing a handle from
one state type to a tool that declares another is rejected with an actionable
error. Every mint is independently valid until its own expiry, so an agent can
retain earlier handles as checkpoints and pass an older handle back to roll the
state back. This restores state only; it does not undo side effects the tool
performed outside the handle.

Handles use MessagePack serialization, base64url encoding, expiry, and
optional HMAC signing. Configure signing and optional authenticated-principal
binding at the server level with `EncodedSessionStateConfig`:

```python
from fastmcp_extensions import EncodedSessionStateConfig, mcp_server

app = mcp_server(
    display_name="my-server",
    encoded_session_state=EncodedSessionStateConfig(
        signing="required",
        secret="a-secret-from-your-deployment",
        principal_binding=True,
    ),
)
```

Signing is explicit: use `signing="required"` for authenticated deployments or
`signing="disabled"` for bearer-style deployments. The default state TTL is 30
days and can be overridden with `class BasketState(ToolStateBase,
state_ttl=timedelta(days=7))`. Every state field must have a default because
omitting the handle constructs the state with no arguments. Principal binding
requires `signing="required"`: an unsigned envelope lets the caller rewrite the
principal, so the binding would claim a boundary it cannot hold. Rotating keys
can retain a previous key through `previous_secrets`.

Servers with at least one stateful tool also register a `get_decoded_state` tool
automatically. It verifies a handle and returns its state fields, state type,
expiry, key ID, signing status, and remaining lifetime, or the specific reason
validation failed. Set `enable_state_inspection_tool=False` in
`EncodedSessionStateConfig` to suppress this tool.

## HTTP server runner

`run_mcp_http_server()` builds and serves a FastMCP HTTP application. When
stateless HTTP is in effect — `stateless_http=True`, or FastMCP's own
`stateless_http` setting — it adds the capability carry-through layers by
default:

```python
from fastmcp_extensions import run_mcp_http_server

run_mcp_http_server(
    app,
    path="/mcp",
    transport="streamable-http",
    stateless_http=True,
)
```

When stateless HTTP is in effect, the composed layers are the caller's
`wrapper=` innermost, then `CapabilityTokenMiddleware`, then the
path-scoped `RejectEventStreamGetMiddleware` outermost. The latter returns
`405` with `Allow: POST, DELETE` for an SSE-style `GET` to the MCP endpoint
while allowing the browser landing page and unrelated routes through. Pass
`enable_stateless_capability_middleware=False` to opt out. Stateful HTTP and
SSE transport do not receive these stateless-only layers.

## Tool Filtering

`mcp_server()` can add the standard filters with
`include_standard_tool_filters=True`:

```python
app = mcp_server(
    display_name="my-server",
    include_standard_tool_filters=True,
)
```

The standard filters support read-only mode, no-destructive mode, module
include/exclude, tool exclusion, and the trusted-execution gate. Read-only and
no-destructive modes use the tool's MCP annotations; annotate tools at
registration time:

```python
@mcp_tool(read_only=True, destructive=False)
def list_items() -> list[str]:
    return ["item1", "item2"]
```

Filters compose with logical **AND**, so each filter can only narrow the visible
tool set. Tools requiring `Capability.CLIENT_FILESYSTEM` remain hidden unless
trusted execution is enabled for a local stdio server. The gate is always forced
off for HTTP requests; call `assert_http_trusted_execution_disabled(app)` from
an HTTP entrypoint to fail fast if its configuration is enabled.

### Custom capabilities

Use `capability_resolvers` for deployment-defined capabilities. A resolver
receives only the server `app`; read deployment configuration through
`get_mcp_config`. These config args are environment-only because they omit
`http_header_key`, so callers cannot widen the tool surface by supplying a
request header:

```python
from fastmcp import FastMCP
from fastmcp_extensions import MCPServerConfigArg, get_mcp_config, mcp_server, mcp_tool

DOCS_SEARCH = "io.example/docs-search"
DOCS_API_KEY = MCPServerConfigArg(
    name="docs_api_key",
    env_var="DOCS_API_KEY",
    default="",
    sensitive=True,
)
DOCS_API_URL = MCPServerConfigArg(
    name="docs_api_url",
    env_var="DOCS_API_URL",
    default="",
)


def docs_search_available(app: FastMCP) -> bool:
    return bool(
        get_mcp_config(app, "docs_api_key").strip()
        and get_mcp_config(app, "docs_api_url").strip()
    )


app = mcp_server(
    display_name="docs-server",
    server_config_args=[DOCS_API_KEY, DOCS_API_URL],
    capability_resolvers={DOCS_SEARCH: docs_search_available},
)


@mcp_tool(required_capabilities=[DOCS_SEARCH])
def search_docs(query: str) -> str:
    """Search the docs."""
    return query
```

Header/env precedence is declared independently on each `MCPServerConfigArg`:
when both are provided, `get_mcp_config` checks that arg's HTTP header before
its environment variable. Omitting `http_header_key` makes that arg
environment-only. Missing resolvers and resolver exceptions leave a custom
capability unavailable; exceptions are logged by type without their message.

## Tool-Call Tracing

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

### One call, one event, one span

Telemetry emits one event per tool call (log line, Sentry breadcrumb, Segment)
and tracing exports one span. Both are derived from the same facts about the
call, so they agree on the tool, the outcome, and the error type, and they can
be joined:

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
| `success` | `<p>.outcome` (`success`, or one of four failure outcomes) |
| `error_type` | `<p>.error_type`, `error.type` |
| `tool_group` | `<p>.tool_module` |
| `mutation_class` | `<p>.tool_mutating`, `<p>.tool_destructive` |
| `mcp_client_name`, `mcp_client_version` | `<p>.client_name`, `<p>.client_version` |
| `package_version` | `service.version` (resource) |

### What a span carries

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
| `<p>.upstream.status_code` | The HTTP status the failure carries, if any |
| `<p>.tool_requested_name` | For an unknown tool: the requested name if it is well formed, else `<other>` |
| `<p>.client_name`, `<p>.client_version` | The MCP client |
| `<p>.caller_hash`, `<p>.caller_id_type` | The caller as telemetry's salted hash, and whether it is a `subject` or a `client`; only with `anonymization_salt` set |
| `<p>.session_id`, `mcp.session.id`, `gen_ai.conversation.id` | The validated SHA-256 hex digest from `session_id`, if configured and valid; otherwise SHA-256 of `Mcp-Session-Id` or the per-process stdio digest. The raw header value is never exported. |
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

### Sampling and existing providers

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

### Options

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
| `error_classifier` | `None` | `(exception) -> category` override; ignored unless it returns a known category. |
| `other_spans` | `None` | `(span) -> attributes` for spans the layer did not stamp. Returns the complete attribute set to keep, or `None` to drop the span. A kept span's name, kind, timing, and parent are exported unchanged, so return `None` for spans whose name may carry data, such as a SQL statement or a client-chosen prompt name. The hook sees every unstamped span in the process, so set it on only one app per process. |
| `arg_key` | `None` | 32-byte secret for argument hashes, or a callable returning it. |
| `arg_default` | `TraceArg.HASH` | How `str`, `int`, `float`, `UUID`, and `list[str]` arguments without a marker are recorded. `VALUE` is treated as `HASH`. |
| `session_id` | `None` | Zero-argument hook returning a SHA-256 hex digest of the host's session key. It is used as-is and not hashed again; `None`, non-digest, or failing results fall back to the transport identifier. |
| `require_own_provider` | `False` | When export starts, raise if the installed `TracerProvider` was not created by this package; dormant and `DO_NOT_TRACK` paths remain non-raising. |

Attributes from `attributes`, `shared_properties`, a per-tool callable, or
`add_trace_attributes()` are written under the prefix and bounded: strings are
stripped, must be printable, and are cut to 256 characters; `bool`, `int`, and
finite `float` pass; anything else is dropped. Keys must match
`[a-z0-9_]+(\.[a-z0-9_]+)*`. Keys the layer owns are dropped silently, and so
is any key whose first segment is `arg`, `args`, `error`, `eval`, `process`,
`result`, `tool`, `tools`, or `upstream` (`result` and `result.rows` alike).

### Per-tool declarations

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

`tracing=` and `TraceArg` markers apply on the app the tool is registered on.
A tool that reaches a traced app through `mount()` or a proxy is traced with
the defaults: a `tracing=False` or `tracing=` callable declared on its own
server is ignored, and each of its arguments is recorded as `PRESENCE`.
`trace_plan(app)` shows what applies. If the mounted server enables tracing
too, it exports its own span for the call under the parent's, with
`<p>.root = False`, like a nested call; count calls by `<p>.root = True`.

### Per-argument declarations

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
| `HASH` | `{"eq": h}`, a keyed hash; lists add `"count"` | `str`, `int`, `float`, `UUID`, `list[str]` (set by `arg_default`) |
| `FINGERPRINT` | `HASH` plus `"fp"`, a keyed fingerprint that shows how similar two short texts are without exposing either | Opt-in only |
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

### Testing

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

## User-Facing Errors

Convert expected exceptions into concise MCP client errors without tracebacks by
passing their types to `mcp_server()`. A formatter can customize the message:

```python
from fastmcp_extensions import mcp_server

app = mcp_server(
    display_name="my-server",
    user_facing_errors=[ValueError],
    user_facing_error_formatter=lambda error: f"Invalid request: {error}",
)
```

## Poe Tasks for MCP Servers

This library provides template scripts for common MCP development tasks. Copy these to your project and customize:

- `bin/test_mcp_tool.py` - Test tools with JSON arguments via stdio
- `bin/test_mcp_tool_http.py` - Test tools over HTTP transport
- `bin/measure_mcp_tool_list.py` - Measure tool list size

Add to your `poe_tasks.toml`:

```toml
[tool.poe.tasks.mcp-tool-test]
help = "Test MCP tools directly with JSON arguments"
cmd = "python bin/test_mcp_tool.py"

[tool.poe.tasks.mcp-tool-test-http]
help = "Test MCP tools over HTTP transport"
cmd = "python bin/test_mcp_tool_http.py"

[tool.poe.tasks.mcp-measure-tools]
help = "Measure the size of the MCP tool list output"
cmd = "python bin/measure_mcp_tool_list.py"
```

## API Reference

### Server Factory

- `mcp_server` - Create a FastMCP instance with a built-in server info resource, optional asset discovery, credential resolution, capability resolvers, and tool filtering.
- `MCPServerConfigArg` - Configuration for credential resolution and other server settings.
- `get_mcp_config` - Get a credential from HTTP headers or environment variables.

### CLI

- `cli_app` - Create a Cyclopts CLI app with shared structured-log, Sentry, and Segment telemetry.

### Tool Filtering

- Standard filters - Read-only, no-destructive, module/tool exclusion, and trusted-execution filters based on MCP annotations and server configuration; enable them with `include_standard_tool_filters=True`.
- `capability_filter` - Hide tools whose built-in or custom `required_capabilities` aren't available; custom IDs require a resolver and resolver errors fail closed. `interactive_ui_filter` / `trusted_execution_filter` are thin wrappers kept for compatibility.
- `extension_tool_filter` - Build a rendering-capability filter for any extension ID and annotation key.
- `assert_http_trusted_execution_disabled` - Fail fast when trusted execution is enabled for an HTTP entrypoint.

### HTTP Helpers

- `run_mcp_http_server` - Build and serve a FastMCP HTTP application with stateless capability carry-through defaults.
- `DEFAULT_UVICORN_CONFIG` - Default Uvicorn settings used by `run_mcp_http_server`.
- `ToolStateBase` / `EncodedSessionStateConfig` - Define state fields and configure encoded state handles for stateless tools.
- `encode_session_state` / `decode_session_state` - Serialize, sign, validate, and restore state handles.
- `DecodedSessionState` / `get_decoded_state` - Typed decoded-handle results and the automatically registered inspection tool.
- `EncodedSessionStateError` - Actionable validation error for expired, invalid, or mismatched handles.
- `fastmcp_extensions.utils.docs.generate_markdown_docs` - Generate Docusaurus- and pdoc-compatible Markdown docs from a FastMCP server inspection.
- `register_landing_page` / `render_default_landing_html` - Add a browser-friendly `GET` landing page to an MCP HTTP endpoint.
- `AuthorizationRedactionFilter` / `install_authorization_redaction` - Scrub credential values from controlled log records.
- `HashKeyNormalizer` / `NormalizedKeysWrapper` - Normalize arbitrary storage keys for durable key-value backends.

### MCP Apps and capability carry-through

- `CapabilityTokenMiddleware` / `RejectEventStreamGetMiddleware` - Carry extension declarations through stateless HTTP and reject SSE-style `GET` requests at the MCP path.
- `encode_capability_token` / `decode_capability_token` - Encode and fail-closed decode self-describing capability tokens.
- `SessionToken` / `encode_session_token` / `decode_session_token` / `session_token_from_headers` - Encode and decode v2 session tokens carrying declared extensions, `clientInfo` name and version, and protocol version. `CapabilityTokenMiddleware` mints one on every `initialize` response, so stateless tool-call telemetry reports `mcp_client_name` and `mcp_client_version` for clients that echo `Mcp-Session-Id`. `minted_session_token(scope)` returns the token being minted for the current `initialize` from the ASGI scope state, so in-app code can correlate the handshake with the `Mcp-Session-Id` the client will echo. Tokens are unsigned client self-declarations, never authorization.
- `client_supports_extension` / `client_declared_extensions_from_headers` - Resolve client extension declarations from FastMCP session capabilities, the session token, and the fallback header.
- `DEFAULT_EXTENSIONS_HEADER` - Default fallback header name, `X-MCP-Extensions`.

### Annotations

| Constant | Description | FastMCP Default |
| -------- | ----------- | --------------- |
| `READ_ONLY_HINT` | Tool only reads data | `False` |
| `DESTRUCTIVE_HINT` | Tool modifies/deletes data | `True` |
| `IDEMPOTENT_HINT` | Repeated calls have same effect | `False` |
| `OPEN_WORLD_HINT` | Tool interacts with external systems | `True` |

### Decorators

- `@mcp_tool(read_only, destructive, idempotent, open_world, requires_client_filesystem, interactive_ui, with_state, meta, app, annotations, required_capabilities, extra_help_text, tracing)` - Tag a tool for deferred registration; the domain comes from the defining module's file stem
- `@mcp_prompt(name, description)` - Tag a prompt for deferred registration
- `@mcp_resource(uri, description, mime_type)` - Tag a resource for deferred registration
- `@mcp_provider(interactive_ui, annotations, required_capabilities)` - Tag a provider factory for deferred tool registration (`interactive_ui` is accepted but has no effect; provider tools are gated via their own `_meta.ui` marker and inherit `required_capabilities`).

### Registration Functions

- `register_mcp_tools(app, domain, exclude_args)` - Register tools with FastMCP app
- `register_mcp_prompts(app, domain)` - Register prompts with FastMCP app
- `register_mcp_resources(app, domain)` - Register resources with FastMCP app

### Testing Utilities

- `call_mcp_tool(app, tool_name, args)` - Call a tool asynchronously
- `list_mcp_tools(app)` - List all available tools
- `run_tool_test(app, tool_name, json_args)` - Run a tool test with JSON args
- `run_http_tool_test(http_server_command, port, tool_name, args, env)` - Test over HTTP

### Measurement Utilities

- `measure_tool_list(app)` - Get (tool_count, total_chars) tuple
- `measure_tool_list_detailed(app, server_name)` - Get detailed measurement
- `get_tool_details(app)` - Get per-tool size breakdown

### Prompt Utilities

- `get_prompt_text(app, prompt_name, arguments)` - Get prompt text content
- `list_prompts(app)` - List all available prompts

### Telemetry

- `TelemetryConfig` / `register_tool_call_telemetry` - Configure tool-call telemetry, including `tool_tracing`, and register it on a plain FastMCP app; `mcp_server(telemetry=...)` does both.
- `ToolCallTelemetryMiddleware` - Record MCP tool-call timing, success, and error type.
- `TelemetrySinks` / `TelemetryRecord` / `ToolCallTelemetryRecord` - Configure telemetry destinations and represent emitted records.

### Tracing

- `ToolTracingConfig` - Options for OpenTelemetry tool-call tracing, passed as `TelemetryConfig(tool_tracing=...)`.
- `TraceArg` - Per-argument marker for `Annotated[...]`: `OMIT`, `PRESENCE`, `HASH`, `FINGERPRINT`, or `VALUE`.
- `add_trace_attributes` - Add bounded attributes to the current tool call's span from inside a tool.
- `capture_tool_spans` / `trace_plan` - Test helpers: collect spans as they would be exported, and list what is recorded for each tool.

### Auth Utilities

- `build_mcp_auth(*, oidc=None, jwt=None, introspection=None, static_tokens=None, base_url=None, required_scopes=None)` - Pure, typed factory that assembles one verifier or a `MultiAuth` from explicit configs. Reads no environment variables — the calling server maps its own env into the configs.
- `OIDCAuthConfig` / `JWTAuthConfig` / `IntrospectionAuthConfig` - Typed configs for the three verifier modes.
- `fetch_client_credentials_token(ClientCredentials(...))` - Client-side OAuth 2.0 client-credentials grant to mint a short-lived bearer token.
- `ClientCredentials` - Parameters for the client-credentials grant (token URL, client id/secret, scope, audience, auth method).
- `ClientCredentialsExchangeMiddleware` / `wrap_client_credentials` - Exchange presented client credentials for a bearer token before FastMCP authentication.
- `build_client_credentials_post_kwargs` - Build token-request form fields for the configured client-credentials auth method.

## Development

```bash
# Install dependencies
uv sync --extra dev

# Run tests
uv run poe test

# Format and lint
uv run poe fix

# Run all checks
uv run poe check
```

## License

MIT License - see [LICENSE](LICENSE) for details.
