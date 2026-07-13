# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Browser-friendly landing pages for MCP HTTP endpoints.

MCP servers running over streamable HTTP only speak `POST`/`DELETE` on their
endpoint path, so a human who opens that URL in a browser gets a bare
`405 Method Not Allowed`. This module lets a server register a `GET` route at
the same path that returns human-readable content explaining what the URL is
and how to use it, without interfering with MCP traffic.

## Basic Usage

Register the built-in branded landing page:

```py
from fastmcp_extensions import register_landing_page

register_landing_page(
    app,
    path=mcp_path,
    title="Airbyte Ops MCP Server",
    endpoint_url="https://mcp.internal.airbyte.ai/ops-mcp",
    docs_url="https://github.com/airbytehq/airbyte-ops-mcp#readme",
)
```

## Custom Content

Pass a `render` callable to fully control the HTML while reusing the route
wiring:

```py
def render(content: LandingPageContent) -> str:
    return f"<h1>{content.title}</h1>"


register_landing_page(
    app,
    path=mcp_path,
    title="My Server",
    endpoint_url="https://example.com/mcp",
    render=render,
)
```
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.responses import HTMLResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP
    from starlette.requests import Request

DEFAULT_DESCRIPTION = (
    "This URL is an <strong>MCP endpoint</strong>, not a web page. Add it to an "
    "MCP client such as Claude, Cursor, VS Code, or Goose &mdash; it isn't meant "
    "to be opened directly in a browser."
)
DEFAULT_POWERED_BY_URL = "https://airbyte.com"
DEFAULT_ROUTE_NAME = "mcp_landing_page"


@dataclass
class LandingPageContent:
    """Content shown on the browser landing page for an MCP endpoint.

    - `title`: Page heading and `<title>`.
    - `endpoint_url`: The streamable-HTTP endpoint users configure in their client.
    - `docs_url`: Optional link to setup instructions; the call-to-action button
      is omitted when this is `None`.
    - `description`: Optional HTML snippet explaining the endpoint. Falls back to a
      generic MCP explanation. This value is treated as trusted HTML and is not
      escaped, so callers must not pass unsanitized user input here.
    - `powered_by_url`: Footer attribution link.
    """

    title: str
    endpoint_url: str
    docs_url: str | None = None
    description: str | None = None
    powered_by_url: str = DEFAULT_POWERED_BY_URL


def render_default_landing_html(content: LandingPageContent) -> str:
    """Render the built-in branded landing page for an MCP endpoint.

    The `endpoint_url`, `docs_url`, and `powered_by_url` values are HTML-escaped.
    The `description` is emitted as-is (see `LandingPageContent`).
    """
    safe_title = html.escape(content.title)
    safe_url = html.escape(content.endpoint_url)
    safe_powered_by = html.escape(content.powered_by_url)
    description = content.description or DEFAULT_DESCRIPTION

    docs_button = ""
    if content.docs_url:
        safe_docs = html.escape(content.docs_url)
        docs_button = f'<a class="btn" href="{safe_docs}">Setup instructions &rarr;</a>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  :root {{ --accent:#615eff; --ink:#0b0b23; --muted:#5b5b7a; --bg:#f7f7fb; --line:#e6e6f0; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:640px; margin:0 auto; padding:64px 24px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:16px; padding:40px; }}
  .badge {{ display:inline-block; font-size:12px; font-weight:600; letter-spacing:.05em;
    text-transform:uppercase; color:var(--accent); margin-bottom:16px; }}
  h1 {{ margin:0 0 16px; font-size:24px; }}
  p {{ color:var(--muted); line-height:1.6; margin:0 0 16px; }}
  .url {{ display:block; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    background:var(--bg); border:1px solid var(--line); border-radius:8px;
    padding:12px 14px; color:var(--ink); word-break:break-all; margin:0 0 24px; }}
  a.btn {{ display:inline-block; background:var(--accent); color:#fff; text-decoration:none;
    font-weight:600; padding:12px 20px; border-radius:8px; }}
  .foot {{ margin-top:24px; font-size:13px; color:var(--muted); }}
  .foot a {{ color:var(--accent); }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="badge">Model Context Protocol</div>
      <h1>{safe_title}</h1>
      <p>{description}</p>
      <p>Configure your MCP client with this streamable-HTTP endpoint:</p>
      <span class="url">{safe_url}</span>
      {docs_button}
      <p class="foot">Powered by <a href="{safe_powered_by}">Airbyte</a>.</p>
    </div>
  </div>
</body>
</html>
"""


def register_landing_page(
    app: FastMCP,
    *,
    path: str = "/",
    title: str,
    endpoint_url: str,
    docs_url: str | None = None,
    description: str | None = None,
    powered_by_url: str = DEFAULT_POWERED_BY_URL,
    render: Callable[[LandingPageContent], str] | None = None,
    route_name: str = DEFAULT_ROUTE_NAME,
) -> None:
    """Register a browser `GET` landing page at an MCP endpoint path.

    In stateless streamable-HTTP mode FastMCP binds only `POST`/`DELETE` to the
    MCP path, so registering a `GET` custom route at the same `path` serves
    browsers without touching MCP traffic. Call this before `app.run(...)`.

    - `path`: The endpoint path to serve the page on. Use the same value passed
      as `path=` to `app.run(...)` (typically `/` behind a path-stripping load
      balancer, or `/mcp` locally).
    - `title`: Page title/heading.
    - `endpoint_url`: The public streamable-HTTP URL users configure.
    - `docs_url`: Optional link to setup instructions.
    - `description`: Optional trusted-HTML description (see `LandingPageContent`).
    - `render`: Optional renderer overriding the built-in template. Receives a
      `LandingPageContent` and returns an HTML string.
    - `route_name`: Starlette route name.
    """
    content = LandingPageContent(
        title=title,
        endpoint_url=endpoint_url,
        docs_url=docs_url,
        description=description,
        powered_by_url=powered_by_url,
    )
    render_fn = render or render_default_landing_html
    html_body = render_fn(content)

    async def _landing_page(request: Request) -> HTMLResponse:
        return HTMLResponse(html_body)

    app.custom_route(path, methods=["GET"], name=route_name)(_landing_page)
