# Contributing

Thank you for your interest in contributing!

## Quick Start

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Setup
uv sync --all-extras

# Install poe globally (optional)
uv tool install poethepoet
```

## Available Commands

View all available tasks:
```bash
poe --help
```

## Suppression Comments

Use a suppression comment only when a real fix is not reasonable. Put the
directive first, then two spaces, a second `#`, and a plain-English reason.
Use this format for ty ignores and Ruff `# noqa` comments:

```python
value = upstream_value  # type: ignore  # The upstream package does not provide this type.
name = value  # noqa: F841  # This local value is required by the example.
```

Keep the explanation specific to the code and do not use unexplained or
file-wide suppressions. Ty does not honor coded mypy-style
`# type: ignore[code]`; use bare `# type: ignore`, or
`# ty: ignore[rule]` when a specific rule needs targeting.

## 🚀 Releasing

This project uses [`semantic-pr-release-drafter`](https://github.com/aaronsteers/semantic-pr-release-drafter) for automated release management. To release, simply click "`Edit`" on the latest release draft from the [releases page](https://github.com/airbytehq/fastmcp-extensions/releases), and then click "`Publish release`". This publish operation will trigger all necessary downstream publish operations.

ℹ️ For more detailed instructions, please see the [Releasing Guide](https://github.com/aaronsteers/semantic-pr-release-drafter/blob/main/docs/releasing.md).
