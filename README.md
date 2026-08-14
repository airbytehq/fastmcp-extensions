# <p align="center">🚀 FastMCP Extensions 🚀</p>

🧩 _The paved road on top of FastMCP. Wire the hard parts once, reuse them on every server you ship._

## What It Adds Over Baseline FastMCP

Baseline [FastMCP](https://github.com/jlowin/fastmcp) is the protocol engine: it gives you the machinery to register tools, prompts, and resources and to speak MCP over stdio or HTTP. This library encodes _how you actually ship_ an MCP server, so each new one inherits the hardening instead of reinventing it:

1. 🔐 **Auth wired once, reused everywhere** - `build_mcp_auth()` is a pure, typed factory that assembles the right verifier — or a `MultiAuth` when several apply — from explicit configs: interactive OIDC for humans (browser Auth Code + PKCE), headless JWT for machines and agents, and opaque-token introspection. Harden it in one place and every server benefits. See [Authenticating an MCP Server](#authenticating-an-mcp-server).
2. 🧯 **Secure, predictable defaults** - The auth factory reads **no environment variables**: each server owns its own env-var names and can validate a complete configuration before building the provider. Refresh-token storage is injectable, so a server can use a durable, shared backend across restarts and replicas without the library owning your database.
3. 🕵️ **Credential hygiene when you wire it in** - An installable redaction filter scrubs bearer tokens and other credential values from controlled log records, while one-way key normalization makes arbitrary client IDs and other store keys legal for durable backends.
4. 🎚️ **Tool filtering from MCP annotations** - Read-only mode, no-destructive mode, and module/tool exclusion use MCP tool annotations (`readOnlyHint`, `destructiveHint`, …) and request/server configuration. Filters compose with logical AND, so layering can only narrow the surface, never widen it. See [Tool Filtering](#tool-filtering).
5. 🧩 **MCP Apps UI support without per-server wiring** - Annotate a tool with `interactive-ui=True`, opt into the standard filters, and the library hides it from clients that cannot render MCP Apps UI. `run_mcp_http_server()` carries the client's extension declaration through stateless HTTP automatically. See [MCP Apps UI support](#mcp-apps-ui-support).
6. 🛡️ **Modality gating, safe in local _and_ hosted deploys** - The standard trusted-execution filter hides tools annotated `requiresClientFilesystem=True` by default, and the gate is forced off under HTTP regardless of configuration. Call `assert_http_trusted_execution_disabled()` at HTTP startup to fail loudly on an unsafe configuration.
7. 🧵 **Deferred registration, solved** - `@mcp_tool` / `@mcp_prompt` / `@mcp_resource` tag tools, prompts, and resources into a registry (auto-detecting the domain from the file stem), and the domain-filtered `register_*` functions register them in one call — organize by domain without fighting import order.
8. 🏭 **A server factory with fewer moving parts** - `mcp_server()` hands you a FastMCP instance that already has a server-info resource, optional asset discovery, and credential resolution from HTTP headers or env vars via `get_mcp_config` — typed pieces instead of hand-wired boilerplate.
9. 🖥️ **One codebase, two front-ends** - `cli_app()` is the CLI counterpart of `mcp_server()`: shared tool functions and the same telemetry sinks can power both surfaces. Write a tool once; call it from the command line and expose it over MCP.
10. 📖 **Auto-generated docs for every tool** - A Markdown docs generator (Docusaurus- and pdoc-compatible) renders your tool surface from the source of truth, giving every tool its own URL anchor to share with stakeholders. Documenting and announcing changes stops being a manual step.
11. 📈 **Telemetry that's free until you want it** - Sentry, Segment, and structured-log sinks record timing, success, and error type across both MCP and CLI paths. Sentry and Segment are no-ops unless you supply their keys, so the telemetry wiring can ship in the base template.
12. 🌐 **Browser-friendly landing page** - A registrable landing page so a browser `GET` on your MCP HTTP endpoint returns something human-readable instead of an error.
13. 🧪 **Test and debug tooling** - `call_mcp_tool` / `run_tool_test` / `run_http_tool_test` exercise tools with JSON args over stdio and HTTP, and tool-list measurement catches context-window truncation before it bites an agent.
14. 🧱 **A buffer against major-version churn** - Servers build against this library's API, not FastMCP's internals, so a FastMCP major bump lands here first. Through the 2.x→3.x transition this library supported both lines during the overlap and the servers on top needed little or no rework; it now targets FastMCP 3.x, and we expect to absorb the 4.x move the same way.

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

The `mcp_server` function creates a FastMCP instance with built-in server info resources and optional credential resolution:

```python
from fastmcp_extensions import mcp_server, MCPServerConfigArg

app = mcp_server(
    name="my-mcp-server",
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

app = mcp_server(name="my-mcp-server", package_name="my-package")

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
several are. For a durable, shared interactive-OIDC store (so refresh tokens
survive restarts and span replicas), the server constructs its own backend and
injects it via `OIDCAuthConfig(client_storage=...)` — keeping all
backend-specific config (project, database, encryption) in the deployment, not
in this library.

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
interactive renderings. Annotate a tool with `interactive_ui=True` and enable
the standard filters:

```python
from fastmcp_extensions import mcp_server, mcp_tool, register_mcp_tools

app = mcp_server(
    name="my-server",
    include_standard_tool_filters=True,
)


@mcp_tool(interactive_ui=True)
def show_dashboard() -> str:
    """Return data for an interactive dashboard."""
    return "dashboard data"


register_mcp_tools(app)
```

The standard `interactive_ui_filter` leaves ordinary tools visible and hides
annotated tools from clients that did not declare the
`io.modelcontextprotocol/ui` extension. This is a rendering-capability check,
not a privilege boundary: extension declarations are client-controlled and
must never be used to grant authority.

## Stateless HTTP capability carry-through

Stateless FastMCP recreates the request session and therefore does not retain
the client's extension declaration from `initialize`. Without carry-through,
the UI filter would hide UI tools from every later request. `run_mcp_http_server()`
handles this by encoding declared extensions into a self-describing
`Mcp-Session-Id` capability token. Compliant clients echo that ID without
requiring server-side session state. Clients that do not echo it can use the
`X-MCP-Extensions` fallback header instead. Goose Desktop is a verified
real-world client using the UI extension declaration.

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
    name="my-server",
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
tool set. Tools annotated `requiresClientFilesystem=True` remain hidden unless
trusted execution is enabled for a local stdio server. The gate is always forced
off for HTTP requests; call `assert_http_trusted_execution_disabled(app)` from
an HTTP entrypoint to fail fast if its configuration is enabled.

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

- `mcp_server` - Create a FastMCP instance with a built-in server info resource, optional asset discovery, credential resolution, and tool filtering.
- `MCPServerConfigArg` - Configuration for credential resolution and other server settings.
- `get_mcp_config` - Get a credential from HTTP headers or environment variables.

### CLI

- `cli_app` - Create a Cyclopts CLI app with shared structured-log, Sentry, and Segment telemetry.

### Tool Filtering

- Standard filters - Read-only, no-destructive, module/tool exclusion, and trusted-execution filters based on MCP annotations and server configuration; enable them with `include_standard_tool_filters=True`.
- `ANNOTATION_INTERACTIVE_UI` / `interactive_ui_filter` - Gate tools annotated `interactive-ui` on the client's `io.modelcontextprotocol/ui` rendering capability.
- `extension_tool_filter` - Build a rendering-capability filter for any extension ID and annotation key.
- `assert_http_trusted_execution_disabled` - Fail fast when trusted execution is enabled for an HTTP entrypoint.

### HTTP Helpers

- `run_mcp_http_server` - Build and serve a FastMCP HTTP application with stateless capability carry-through defaults.
- `DEFAULT_UVICORN_CONFIG` - Default Uvicorn settings used by `run_mcp_http_server`.
- `fastmcp_extensions.utils.docs.generate_markdown_docs` - Generate Docusaurus- and pdoc-compatible Markdown docs from a FastMCP server inspection.
- `register_landing_page` / `render_default_landing_html` - Add a browser-friendly `GET` landing page to an MCP HTTP endpoint.
- `AuthorizationRedactionFilter` / `install_authorization_redaction` - Scrub credential values from controlled log records.
- `HashKeyNormalizer` / `NormalizedKeysWrapper` - Normalize arbitrary storage keys for durable key-value backends.

### MCP Apps and capability carry-through

- `CapabilityTokenMiddleware` / `RejectEventStreamGetMiddleware` - Carry extension declarations through stateless HTTP and reject SSE-style `GET` requests at the MCP path.
- `encode_capability_token` / `decode_capability_token` - Encode and fail-closed decode self-describing capability tokens.
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

- `@mcp_tool(read_only, destructive, idempotent, open_world, requires_client_filesystem, interactive_ui, extra_help_text)` - Tag a tool for deferred registration; the domain comes from the defining module's file stem
- `@mcp_prompt(name, description)` - Tag a prompt for deferred registration
- `@mcp_resource(uri, description, mime_type)` - Tag a resource for deferred registration
- `@mcp_provider(interactive_ui, annotations)` - Tag a provider factory for deferred tool registration.

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

- `ToolCallTelemetryMiddleware` - Record MCP tool-call timing, success, and error type.
- `TelemetrySinks` / `TelemetryRecord` / `ToolCallTelemetryRecord` - Configure telemetry destinations and represent emitted records.

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
