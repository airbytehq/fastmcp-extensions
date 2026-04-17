# Copyright (c) 2026 Airbyte, Inc., all rights reserved.
"""Unit tests for `fastmcp_extensions.utils.docs`.

These tests exercise the pure rendering / bucketing helpers against synthetic
`fastmcp inspect` report fixtures so they run without invoking the real
`fastmcp` CLI or importing any user server module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fastmcp_extensions.utils.docs import (
    DEFAULT_OUTPUT,
    MISC_MODULE,
    _bucket_by_module,
    _fmt_default,
    _fmt_type,
    _get_module,
    _prepare_output_dir,
    _render_hint_badges,
    _render_index,
    _render_module_page,
    _render_parameters_table,
    _render_prompt,
    _render_resource,
    _render_tool,
    _spec_to_module_name,
)

# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param("my_pkg.mcp.server:app", "my_pkg.mcp.server", id="dotted"),
        pytest.param("path/to/server.py:app", "path.to.server", id="path"),
        pytest.param("path/to/server:app", "path.to.server", id="path-no-py"),
        pytest.param("solo_module:app", "solo_module", id="no-separator"),
        pytest.param(
            "src/my_pkg/mcp/server.py:app",
            "my_pkg.mcp.server",
            id="src-layout-stripped",
        ),
    ],
)
@pytest.mark.unit
def test_spec_to_module_name(spec: str, expected: str) -> None:
    """Normalization accepts both file-path and dotted-module specs."""
    assert _spec_to_module_name(spec) == expected


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        pytest.param({"type": "string"}, "string", id="primitive"),
        pytest.param(
            {"type": "array", "items": {"type": "integer"}},
            "array<integer>",
            id="array",
        ),
        pytest.param({"enum": ["a", "b"]}, 'enum("a", "b")', id="enum"),
        pytest.param(
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "string | null",
            id="union-anyOf",
        ),
        pytest.param({"type": ["string", "null"]}, "string | null", id="multi-type"),
        pytest.param({}, "any", id="empty"),
    ],
)
@pytest.mark.unit
def test_fmt_type(schema: dict, expected: str) -> None:
    """`_fmt_type` renders expected type strings for common JSON-schema shapes."""
    assert _fmt_type(schema) == expected


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        pytest.param({"default": 3}, "`3`", id="int"),
        pytest.param({"default": None}, "`null`", id="null"),
        pytest.param({"default": "foo"}, '`"foo"`', id="string"),
        pytest.param({}, "—", id="missing"),
    ],
)
@pytest.mark.unit
def test_fmt_default(schema: dict, expected: str) -> None:
    """`_fmt_default` renders defaults as Markdown code spans (or em-dash)."""
    assert _fmt_default(schema) == expected


# -----------------------------------------------------------------------------
# Per-item renderers
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_render_hint_badges_emits_true_hints_only() -> None:
    """Only hints set to explicit `True` get rendered; falsy/missing are omitted."""
    out = _render_hint_badges(
        {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "title": "Do a thing",
        }
    )
    assert "`read-only`" in out
    assert "`idempotent`" in out
    assert "destructive" not in out
    assert "**Title:** Do a thing" in out


@pytest.mark.unit
def test_render_parameters_table_handles_empty() -> None:
    """A schema with no properties renders the `_No parameters._` sentinel."""
    assert _render_parameters_table({"type": "object", "properties": {}}) == (
        "_No parameters._\n\n"
    )


@pytest.mark.unit
def test_render_parameters_table_escapes_pipes_in_union_types() -> None:
    """Union types (which contain `|`) get escaped so GFM tables still render."""
    out = _render_parameters_table(
        {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "A value",
                },
            },
        }
    )
    # The literal `|` in `string | null` must be backslash-escaped inside
    # the table cell.
    assert "`string \\| null`" in out
    assert "yes" in out


@pytest.mark.unit
def test_render_tool_includes_anchor_heading_and_parameters() -> None:
    """A rendered tool has an HTML anchor, H3 heading, and a parameters block."""
    out = _render_tool(
        {
            "name": "do_stuff",
            "description": "Does stuff.",
            "input_schema": {
                "type": "object",
                "required": ["flag"],
                "properties": {"flag": {"type": "boolean", "description": "Flag."}},
            },
            "annotations": {"readOnlyHint": True},
            "tags": ["beta"],
        }
    )
    assert '<a id="do_stuff"></a>' in out
    assert "### do_stuff" in out
    assert "`read-only`" in out
    assert "**Tags:** `beta`" in out
    # "Parameters" renders as bold text, not an H4 heading, so pdoc's TOC
    # extractor doesn't surface it as a redundant sibling of the tool's
    # nav entry in the left sidebar.
    assert "**Parameters:**" in out
    assert "#### Parameters" not in out
    assert "`flag`" in out
    # The collapsible input JSON schema block should be included.
    assert "<details>" in out
    assert "Show input JSON schema" in out


@pytest.mark.unit
def test_render_prompt_arguments_block_uses_bold_label() -> None:
    """A prompt with arguments renders `**Arguments:**` (not an H4 heading)."""
    out = _render_prompt(
        {
            "name": "greet",
            "description": "Say hello.",
            "arguments": [
                {"name": "who", "description": "Name.", "required": True},
            ],
        }
    )
    assert "### greet" in out
    # Matches the same bold-not-heading treatment used for `_render_tool`'s
    # "Parameters" label, for the same pdoc sidebar-noise reason.
    assert "**Arguments:**" in out
    assert "#### Arguments" not in out
    assert "`who`" in out


@pytest.mark.unit
def test_render_prompt_without_arguments() -> None:
    """A prompt with no arguments renders the `_No arguments._` sentinel."""
    out = _render_prompt({"name": "hello", "description": "Hi."})
    assert "### hello" in out
    assert "_No arguments._" in out


@pytest.mark.unit
def test_render_resource_emits_uri_and_mime() -> None:
    """A resource renders its URI, MIME type, and tags as a bullet list."""
    out = _render_resource(
        {
            "name": "config",
            "description": "Server config.",
            "uri": "config://server",
            "mime_type": "application/json",
            "tags": ["read"],
        }
    )
    assert "### config" in out
    assert "`config://server`" in out
    assert "application/json" in out
    assert "- **Tags:** `read`" in out


# -----------------------------------------------------------------------------
# Bucketing + page rendering
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_get_module_prefers_annotations_then_meta_then_fallback() -> None:
    """`_get_module` respects the annotation > meta > fallback > misc precedence."""
    assert _get_module({"annotations": {"mcp_module": "cloud"}}, {}) == "cloud"
    assert _get_module({"meta": {"mcp_module": "local"}}, {}) == "local"
    assert _get_module({"name": "t1"}, {"t1": "reg"}) == "reg"
    assert _get_module({"name": "unknown"}, {}) == MISC_MODULE


_FIXTURE_REPORT = {
    "server": {
        "name": "demo-server",
        "version": "1.2.3",
        "instructions": "Hello.\nMore info below.",
    },
    "tools": [
        {
            "name": "beta_tool",
            "description": "Beta.",
            "annotations": {"mcp_module": "cloud"},
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "alpha_tool",
            "description": "Alpha.",
            "annotations": {"mcp_module": "cloud"},
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "orphan_tool",
            "description": "No module.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ],
    "prompts": [
        {"name": "prompt_a", "annotations": {"mcp_module": "local"}},
    ],
    "resources": [
        {"name": "res_a", "uri": "r://1", "annotations": {"mcp_module": "local"}},
    ],
}


@pytest.mark.unit
def test_bucket_by_module_groups_and_alpha_sorts() -> None:
    """Items bucket by mcp_module; items within a bucket alpha-sort; misc last."""
    buckets = _bucket_by_module(_FIXTURE_REPORT, fallback_map={})
    module_order = list(buckets.keys())
    # Alpha-sorted modules with `misc` pinned to the end.
    assert module_order == ["cloud", "local", "misc"]
    # Within the cloud bucket, tools are alpha-sorted.
    assert [t["name"] for t in buckets["cloud"].tools] == ["alpha_tool", "beta_tool"]
    # Tools without mcp_module land in the misc bucket.
    assert [t["name"] for t in buckets["misc"].tools] == ["orphan_tool"]


@pytest.mark.unit
def test_render_module_page_has_expected_sections() -> None:
    """A module page renders an H1, a summary line, and per-primitive H2 sections."""
    buckets = _bucket_by_module(_FIXTURE_REPORT, fallback_map={})
    page = _render_module_page(buckets["cloud"], server_name="demo-server")
    assert page.startswith("# cloud module")
    assert "## Tools (2)" in page
    # Per-tool H3 headings.
    assert "### alpha_tool" in page
    assert "### beta_tool" in page
    # No prompts or resources in the `cloud` bucket, so those sections
    # are absent.
    assert "## Prompts" not in page
    assert "## Resources" not in page


@pytest.mark.unit
def test_render_index_has_frontmatter_and_module_table() -> None:
    """The index page begins with YAML front-matter and includes a Modules table."""
    buckets = _bucket_by_module(_FIXTURE_REPORT, fallback_map={})
    page = _render_index(_FIXTURE_REPORT, buckets)
    assert page.startswith("---\n")
    # Front-matter scalars are emitted as double-quoted YAML strings so
    # values containing YAML-significant characters (e.g. the em-dash in
    # this title is fine, but real server names often contain colons)
    # don't break downstream parsers.
    assert 'title: "demo-server — MCP server"' in page
    assert 'sidebar_label: "Overview"' in page
    assert "| Module | Tools | Prompts | Resources |" in page
    # Totals line reflects the fixture data.
    assert "**Tools:** 3" in page
    assert "**Prompts:** 1" in page
    assert "**Resources:** 1" in page


# -----------------------------------------------------------------------------
# Output-dir safety
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_prepare_output_dir_creates_and_wipes(tmp_path: Path) -> None:
    """`_prepare_output_dir` rmtrees and re-creates the requested directory."""
    target = tmp_path / "out"
    target.mkdir()
    (target / "stale.md").write_text("stale", encoding="utf-8")
    resolved = _prepare_output_dir(target)
    assert resolved.exists()
    assert not (resolved / "stale.md").exists()


@pytest.mark.unit
def test_prepare_output_dir_refuses_unsafe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_prepare_output_dir` refuses to wipe root, cwd, or home."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="Refusing to rmtree"):
        _prepare_output_dir(Path.cwd())


@pytest.mark.unit
def test_default_output_is_repo_relative() -> None:
    """`DEFAULT_OUTPUT` is a sensible relative default."""
    assert not DEFAULT_OUTPUT.is_absolute()
    assert DEFAULT_OUTPUT.parts[0] == "docs"
