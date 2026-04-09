# Contributing to HappyFigure

Thank you for your interest in contributing!

## Development Setup

```bash
git clone https://github.com/qingfengtommy/HappyFigure.git
cd HappyFigure
pip install -e ".[dev,all]"
pre-commit install
```

## Making Changes

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Run linting: `ruff check .`
4. Run formatting: `ruff format --check .`
5. Run tests: `pytest tests/ -v`
6. Submit a pull request

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Write a clear title and description explaining *what* and *why*
- Link related issues (e.g., "Fixes #42")
- Ensure CI passes (lint + tests) before requesting review
- Add tests for new functionality when possible

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_svg_utils.py -v

# Run with short traceback
pytest tests/ -v --tb=short
```

Tests use `unittest.mock` and custom fixtures from `tests/conftest.py`. When adding tests:
- Place test files in `tests/` with the `test_` prefix
- Use existing fixtures (e.g., `pipeline_config`, `fake_provider`) from `conftest.py`
- Mock external calls (LLM APIs, services) — don't require live credentials

## Code Style

- Python 3.10+ with type hints on public APIs
- Max line length: 120 characters
- Use `ruff` for linting and formatting
- Follow existing patterns in the codebase
- No trailing whitespace or missing newlines at EOF

## Architecture

- **Pipeline nodes** go in `graphs/`
- **LLM providers** go in `llm/providers/`
- **Agent prompts** go in `prompts/agents/` (shared across platforms)
- **Platform adapters** go in `agents/`
- **Configuration** goes in `configs/pipeline.yaml`

## Adding a New LLM Provider

1. Create `llm/providers/your_provider.py`
2. Implement the `LLMProvider` interface (see `llm/providers/__init__.py`)
3. Register with `@register_provider("your_name")`
4. Add optional dependency in `pyproject.toml` under a new extra
5. Import the SDK lazily in the provider constructor (so it's optional at install time)

## Adding a New Agent Platform

1. Create `agents/your_platform.py`
2. Implement the `OrchestratorBase` interface (see `agents/__init__.py`)
3. Register with `@register_platform("your_name")`
4. Add the platform name to the CLI choices in `cli.py`

## Commit Messages

- Use present tense ("Add feature" not "Added feature")
- Keep the first line under 72 characters
- Reference issues when applicable

## Reporting Issues

- Use GitHub Issues for bug reports and feature requests
- Include: Python version, OS, steps to reproduce, expected vs actual behavior
- For LLM-related issues, include the provider and model being used
