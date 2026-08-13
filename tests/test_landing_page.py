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
@pytest.mark.parametrize("field", ["docs_url", "powered_by_url"])
def test_render_rejects_unsafe_href_scheme(field: str) -> None:
    """`javascript:`/`data:` URLs in href fields raise instead of rendering."""
    kwargs = {
        "title": "S",
        "endpoint_url": "https://e/mcp",
        field: "javascript:alert(1)",
    }
    with pytest.raises(ValueError, match="Unsafe URL scheme"):
        render_default_landing_html(LandingPageContent(**kwargs))


@pytest.mark.unit
def test_render_allows_relative_docs_url() -> None:
    """Relative (scheme-less) href URLs are permitted."""
    html = render_default_landing_html(
        LandingPageContent(title="S", endpoint_url="https://e/mcp", docs_url="/docs")
    )
    assert 'href="/docs"' in html


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
    with TestClient(app.http_app(path="/mcp", stateless_http=True)) as client:
        response = client.get("/mcp")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>S</title>" in response.text


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


@pytest.mark.unit
def test_render_includes_version_footer_when_set() -> None:
    """The muted version footer renders only when a version is provided."""
    with_version = render_default_landing_html(
        LandingPageContent(
            title="S",
            endpoint_url="https://e/mcp",
            version_str="v1.2.3",
        )
    )
    assert '<p class="version">v1.2.3</p>' in with_version

    without_version = render_default_landing_html(
        LandingPageContent(title="S", endpoint_url="https://e/mcp")
    )
    assert 'class="version"' not in without_version


@pytest.mark.unit
def test_render_escapes_version() -> None:
    """A version string is HTML-escaped rather than injected as markup."""
    html = render_default_landing_html(
        LandingPageContent(
            title="S",
            endpoint_url="https://e/mcp",
            version_str="<script>x</script>",
        )
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.unit
def test_register_landing_page_serves_version() -> None:
    """A version passed to `register_landing_page` reaches the served page."""
    app = FastMCP("t")
    register_landing_page(
        app,
        path="/mcp",
        title="S",
        endpoint_url="https://e/mcp",
        version_str="v9.9.9",
    )
    with TestClient(app.http_app(path="/mcp", stateless_http=True)) as client:
        response = client.get("/mcp")
    assert response.status_code == 200
    assert "v9.9.9" in response.text


@pytest.mark.unit
def test_render_links_version_when_version_url_set() -> None:
    """A version_url wraps the version footer in a link."""
    html = render_default_landing_html(
        LandingPageContent(
            title="S",
            endpoint_url="https://e/mcp",
            version_str="v1.2.3",
            version_url="https://example.com/releases/v1.2.3",
        )
    )
    assert '<a href="https://example.com/releases/v1.2.3">v1.2.3</a>' in html


@pytest.mark.unit
def test_render_rejects_unsafe_version_url() -> None:
    """An unsafe version_url scheme raises rather than rendering a link."""
    with pytest.raises(ValueError, match="Unsafe URL scheme"):
        render_default_landing_html(
            LandingPageContent(
                title="S",
                endpoint_url="https://e/mcp",
                version_str="v1.2.3",
                version_url="javascript:alert(1)",
            )
        )


@pytest.mark.unit
def test_render_ignores_version_url_without_version() -> None:
    """A version_url alone renders no footer."""
    html = render_default_landing_html(
        LandingPageContent(
            title="S",
            endpoint_url="https://e/mcp",
            version_url="https://example.com/releases",
        )
    )
    assert 'class="version"' not in html
