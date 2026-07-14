# FastMCP Extensions

Unofficial extension library for FastMCP 2.0 with patterns, practices, and utilities for building MCP servers.

## Features

- MCP Server Factory: `mcp_server()` helper that creates FastMCP instances with built-in server info resources, MCP asset discovery (optional), and credential resolution.
- MCP Annotation Constants: Standard annotation hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) following the FastMCP 2.2.7+ specification
- Deferred Registration Decorators: `@mcp_tool`, `@mcp_prompt`, `@mcp_resource` decorators for organizing tools by domain with automatic domain detection.
- Registration Utilities: Functions to register tools, prompts, and resources with a FastMCP app, filtered by domain.
- Tool Testing Utilities: Helpers for testing MCP tools directly with JSON arguments (stdio and HTTP transports).
- Tool List Measurement: Utilities for measuring tool list size to track context truncation issues.
- Prompt Helpers: Generic `get_prompt_text` helper for agents that cannot access prompt assets directly.
- Auth Factory: `resolve_mcp_auth()` / `build_mcp_auth()` assemble a FastMCP `AuthProvider` from environment variables (interactive OIDC for humans, headless JWT bearer for machines, opaque-token introspection, and static tokens), plus `fetch_client_credentials_token()` for clients that need to mint a bearer token.

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
from fastmcp_extensions import mcp_tool, mcp_resource, register_mcp_tools, register_mcp_resources

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
from fastmcp_extensions.measurement import measure_tool_list_detailed

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
from fastmcp_extensions.testing import call_mcp_tool, run_tool_test
import asyncio

# Call a tool programmatically
result = asyncio.run(call_mcp_tool(app, "list_items", {}))

# Or use the CLI helper
run_tool_test(app, "list_items", '{}')
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

The recommended entry point is `resolve_mcp_auth()`, which reads a standard set
of environment variables and returns an `AuthProvider | None` (return `None` =
run unauthenticated, e.g. local stdio):

```python
from fastmcp_extensions import resolve_mcp_auth

app = mcp_server(name="my-mcp-server", package_name="my-package")
app.auth = resolve_mcp_auth()  # env-driven; None when nothing is configured
```

`resolve_mcp_auth()` understands three transport-auth modes, and combines any
that are configured via FastMCP's `MultiAuth`:

| Mode | Who it's for | Enabling env vars |
| ---- | ------------ | ----------------- |
| Interactive OIDC (`OIDCProxy`) | humans (browser Auth Code + PKCE) | `OIDC_CONFIG_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` (all three; optional `OIDC_AUDIENCE`) |
| Headless JWT (`JWTVerifier`) | machines / agents | `MCP_AUTH_JWKS_URI` **or** `MCP_AUTH_JWT_PUBLIC_KEY` (optional `MCP_AUTH_ISSUER`, `MCP_AUTH_AUDIENCE`, `MCP_AUTH_ALGORITHM`) |
| Opaque-token introspection (`IntrospectionTokenVerifier`) | machines with opaque tokens | `MCP_AUTH_INTROSPECTION_URL` + `MCP_AUTH_INTROSPECTION_CLIENT_ID`/`_SECRET` (falls back to `OIDC_CLIENT_ID`/`_SECRET`) |

Shared: `MCP_SERVER_URL` (public base URL) and `MCP_AUTH_REQUIRED_SCOPES`
(comma/space separated). Interactive OIDC is all-or-nothing: setting
`OIDC_CONFIG_URL` without both client credentials logs a warning and leaves
interactive auth disabled.

**Batteries-included JWT realm without hard-coding literals.** A server that
wants to ship a default headless realm (issuer / JWKS URI / audience /
algorithm) passes a `JWTAuthConfig` as `jwt_defaults`. Each `MCP_AUTH_*` env var
overrides the matching field, so a deployment can point at its own realm while
inheriting anything it leaves unset:

```python
import os

from fastmcp_extensions import JWTAuthConfig, resolve_mcp_auth

MY_REALM = JWTAuthConfig(
    jwks_uri="https://idp.example/.well-known/jwks.json",
    issuer="https://idp.example/",
    audience="my-api",
    algorithm="RS256",
)

# Gate the default behind your own flag so the literal stays in your code, not here:
defaults = MY_REALM if os.getenv("MY_MCP_USE_DEFAULT_REALM") else None
app.auth = resolve_mcp_auth(jwt_defaults=defaults)
```

**Lower-level API.** When env-driven resolution isn't enough (bespoke combos,
static tokens, programmatic config), call `build_mcp_auth()` directly with any
of `oidc=`, `jwt=`, `introspection=`, `static_tokens=`. It returns a single
verifier when one is configured, or a `MultiAuth` when several are.

**Client side.** A headless client mints its own short-lived bearer token and
sends it as `Authorization: Bearer <token>`; use
`fetch_client_credentials_token(ClientCredentials(...))` for an OAuth 2.0
client-credentials grant. Nothing is stored server-side — no refresh-token
state. If the token the client mints is also a valid credential for a downstream
API (i.e. the verifier points at that API's issuer), the server can reuse the
verified token as the downstream bearer via FastMCP's `get_access_token()` — one
token doing both transport auth and downstream authorization.

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

- `mcp_server` - Create a FastMCP instance with built-in server info resource and auto-registration of decorated tools and assets.
- `MCPServerConfigArg` - Configuration for credential resolution and other server settings.
- `get_mcp_config` - Get a credential from HTTP headers or environment variables.

### Annotations

| Constant | Description | FastMCP Default |
| -------- | ----------- | --------------- |
| `READ_ONLY_HINT` | Tool only reads data | `False` |
| `DESTRUCTIVE_HINT` | Tool modifies/deletes data | `True` |
| `IDEMPOTENT_HINT` | Repeated calls have same effect | `False` |
| `OPEN_WORLD_HINT` | Tool interacts with external systems | `True` |

### Decorators

- `@mcp_tool(domain, read_only, destructive, idempotent, open_world, extra_help_text)` - Tag a tool for deferred registration
- `@mcp_prompt(name, description, domain)` - Tag a prompt for deferred registration
- `@mcp_resource(uri, description, mime_type, domain)` - Tag a resource for deferred registration

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

### Auth Utilities

- `resolve_mcp_auth(env=None, *, jwt_defaults=None)` - Build an `AuthProvider | None` from environment variables; pass `jwt_defaults` (a `JWTAuthConfig`) to supply a default headless realm that `MCP_AUTH_*` vars can override.
- `build_mcp_auth(*, oidc=None, jwt=None, introspection=None, static_tokens=None, base_url=None, required_scopes=None)` - Lower-level factory that assembles one verifier or a `MultiAuth` from explicit configs.
- `OIDCAuthConfig` / `JWTAuthConfig` / `IntrospectionAuthConfig` - Typed configs for the three verifier modes.
- `fetch_client_credentials_token(ClientCredentials(...))` - Client-side OAuth 2.0 client-credentials grant to mint a short-lived bearer token.
- `ClientCredentials` - Parameters for the client-credentials grant (token URL, client id/secret, scope, audience, auth method).

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
