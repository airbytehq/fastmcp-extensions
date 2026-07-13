# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the landing page helpers."""

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from fastmcp_extensions import (
    LandingPageContent,
    register_landing_page,
    render_default_landing_html,
)


@pytest.mark.unit
def test_render_includes_title_and_endpoint() -> None:
    """The rendered page shows the title and endpoint URL."""
    html = render_default_landing_html(
        LandingPageContent(
            title="My Server",
            endpoint_url="https://example.com/mcp",
        )
    )
    assert "<title>My Server</title>" in html
    assert "My Server" in html
    assert "https://example.com/mcp" in html


@pytest.mark.unit
def test_render_includes_docs_button_when_docs_url_set() -> None:
    """The setup-instructions button renders only when docs_url is provided."""
    with_docs = render_default_landing_html(
        LandingPageContent(
            title="S",
            endpoint_url="https://e/mcp",
            docs_url="https://docs.example.com",
        )
    )
    assert 'href="https://docs.example.com"' in with_docs
    assert "Setup instructions" in with_docs

    without_docs = render_default_landing_html(
        LandingPageContent(title="S", endpoint_url="https://e/mcp")
    )
    assert "Setup instructions" not in without_docs


@pytest.mark.unit
def test_render_escapes_endpoint_and_docs_urls() -> None:
    """User-supplied URLs are HTML-escaped to prevent markup injection."""
    html = render_default_landing_html(
        LandingPageContent(
            title="S",
            endpoint_url='https://e/mcp?x="><script>',
            docs_url='https://d?y="><b>',
        )
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.unit
def test_register_adds_get_route() -> None:
    """register_landing_page registers a GET route at the given path."""
    app = FastMCP("t")
    register_landing_page(
        app,
        path="/mcp",
        title="S",
        endpoint_url="https://e/mcp",
    )
    routes = app._additional_http_routes
    landing = [r for r in routes if r.name == "mcp_landing_page"]
    assert len(landing) == 1
    assert landing[0].path == "/mcp"
    assert "GET" in landing[0].methods


@pytest.mark.unit
def test_landing_route_serves_html() -> None:
    """A browser GET to the landing path returns the HTML page."""
    app = FastMCP("t")
    register_landing_page(
        app,
        path="/mcp",
        title="My Server",
        endpoint_url="https://example.com/mcp",
        docs_url="https://docs.example.com",
    )
    with TestClient(app.http_app(path="/mcp", stateless_http=True)) as client:
        response = client.get("/mcp")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "My Server" in response.text
    assert "https://example.com/mcp" in response.text


@pytest.mark.unit
def test_custom_render_overrides_default() -> None:
    """A custom render callable replaces the built-in template."""
    app = FastMCP("t")
    register_landing_page(
        app,
        path="/mcp",
        title="Custom",
        endpoint_url="https://e/mcp",
        render=lambda content: f"<h1>{content.title}</h1>",
    )
    with TestClient(app.http_app(path="/mcp", stateless_http=True)) as client:
        response = client.get("/mcp")
    assert response.status_code == 200
    assert response.text == "<h1>Custom</h1>"
