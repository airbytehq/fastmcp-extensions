# Awesome Python Template

A modern Python project template with best practices and cutting-edge tooling.

## 🚀 Features

- **🔄 Automated releases** with Release Drafter and semantic versioning
- **📦 uv** for fast, reliable package management
- **🏗️ Source layout** with `src/{library-name}` structure
- **🧪 pytest** for comprehensive testing
- **🎨 ruff** for lightning-fast linting and formatting
- **🔍 deptry** for dependency analysis and unused dependency detection
- **⚡ PoeThePoet** for task automation
- **🤖 GitHub Actions** with PR welcome messages and slash command support
- **📋 Dedicated config files** instead of cramming everything into pyproject.toml

## 🛠️ Quick Start

1. **Clone and setup:**
   ```bash
   git clone <your-repo>
   cd <your-repo>
   uv sync --extra dev
   ```

2. **Run tasks with poe:**
   ```bash
   # List all available tasks
   uv run poe

   # Run tests
   uv run poe test

   # Format and lint code
   uv run poe format
   uv run poe lint

   # Run all checks
   uv run poe check
   ```

## 📋 Available Tasks

### Core Development
- `poe test` - Run all tests
- `poe test-fast` - Run tests with fast exit on first failure  
- `poe test-cov` - Run tests with coverage reporting
- `poe lint` - Check code style and quality
- `poe format` - Format code with ruff
- `poe format-check` - Check if code is properly formatted
- `poe deps` - Check for unused and missing dependencies

### Convenience Tasks  
- `poe check` - Run format check, linting, dependency check, and tests
- `poe fix` - Auto-format and fix linting issues
- `poe pre-commit` - Run pre-commit style checks

### Build & Install
- `poe install` - Install with development dependencies
- `poe install-prod` - Install production dependencies only
- `poe build` - Build the package

### Utilities
- `poe clean` - Clean up build artifacts and cache
- `poe version` - Show package version
- `poe docs` - Generate documentation (placeholder)
- `poe typecheck` - Run type checking (placeholder)
- `poe security` - Run security checks (placeholder)

## 🤖 GitHub Integration

### PR Welcome Messages
When you open a pull request, you'll automatically get a welcome message with helpful commands.

### Slash Commands
Use `/poe <task-name>` in PR comments to run tasks:

- `/poe test` - Run tests
- `/poe lint` - Check code quality
- `/poe format` - Format code
- `/poe check` - Run all checks

**Note**: For security reasons, slash commands run against the base repository code, not the PR changes. This ensures that untrusted code cannot be executed in a privileged environment.

## 📁 Project Structure

```
awesome-python-template/
├── src/
│   └── awesome_python_template/    # Main source code
│       ├── __init__.py
│       └── py.typed
├── tests/                          # Test files
│   ├── __init__.py
│   └── test_awesome_python_template.py
├── .github/
│   └── workflows/                  # GitHub Actions
│       ├── pr-welcome.yml
│       └── slash-command-dispatch.yml
├── pyproject.toml                  # Project metadata and minimal config
├── ruff.toml                       # Ruff configuration
├── pytest.ini                     # Pytest configuration
├── poe_tasks.toml                  # PoeThePoet task definitions (reference)
├── uv.lock                         # Dependency lock file
└── README.md                       # This file
```

## 🔧 Configuration Files

This template uses **dedicated configuration files** for each tool:

- **`ruff.toml`** - Ruff linting and formatting configuration
- **`pytest.ini`** - Pytest testing configuration  
- **`pyproject.toml`** - Minimal project metadata and poe tasks
- **`poe_tasks.toml`** - Reference copy of task definitions

## 🧪 Testing

Tests are organized with pytest markers:
- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests

Run specific test types:
```bash
uv run poe test-unit        # Unit tests only
uv run poe test-integration # Integration tests only
```

## 📦 Dependencies

Development dependencies are defined in `pyproject.toml`:
- **pytest** - Testing framework
- **pytest-cov** - Coverage reporting
- **ruff** - Linting and formatting
- **deptry** - Dependency analysis
- **poethepoet** - Task runner

### Dependency Analysis

This template includes `deptry` for detecting unused and missing dependencies. To ignore false positives, search for "deptry" in the repository and update the configuration in `pyproject.toml`:

```toml
[tool.deptry]
# To ignore specific error codes globally:
ignore = ["DEP004"]  # Example: ignore misplaced dev dependencies

# To ignore specific packages, use CLI options in poe tasks:
# poe deps --per-rule-ignores DEP002=package-name
```

## 🎯 Best Practices

This template follows modern Python best practices:

1. **Source layout** - Code in `src/` directory
2. **Dependency management** - uv for fast, reliable installs
3. **Code quality** - Ruff for consistent formatting and linting
4. **Testing** - Comprehensive pytest setup with coverage
5. **Task automation** - PoeThePoet for development workflows
6. **CI/CD** - GitHub Actions with PR automation
7. **Configuration** - Dedicated files for each tool

## 🔄 Development Workflow

1. Make your changes
2. Run `uv run poe fix` to auto-format and fix linting
3. Run `uv run poe test` to ensure tests pass
4. Push your changes
5. Use `/poe <task>` commands in PR comments as needed

## 🤝 Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and development guidelines.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
