# Contributing to SpecterQA

Thanks for your interest in contributing to SpecterQA! This guide will help you get started.

## Development Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/SyncTek-LLC/specterqa.git
   cd specterqa
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # .venv\Scripts\activate   # Windows
   ```

3. **Install in development mode**

   ```bash
   pip install -e ".[dev]"
   ```

4. **Install Playwright browsers** (needed for integration tests)

   ```bash
   playwright install chromium
   ```

## Running Tests

```bash
# Run all unit tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=src/specterqa --cov-report=term-missing

# Skip integration tests (which require a running app and API key)
pytest tests/ -v -m "not integration"
```

## Code Quality

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
# Check for lint issues
ruff check src/

# Auto-fix lint issues
ruff check src/ --fix

# Check formatting
ruff format --check src/

# Auto-format
ruff format src/
```

Type checking with mypy:

```bash
mypy src/specterqa
```

## Submitting Changes

1. **Fork the repository** and create a branch from `main`.
2. **Make your changes.** Add tests for new functionality.
3. **Ensure all checks pass:**
   ```bash
   ruff check src/
   ruff format --check src/
   pytest tests/ -v
   ```
4. **Open a pull request** against `main`. Fill out the PR template.
5. A maintainer will review your PR. Address any feedback, then it will be merged.

## Reporting Bugs

Use the [bug report template](https://github.com/SyncTek-LLC/specterqa/issues/new?template=bug_report.yml) on GitHub Issues.

## Requesting Features

Use the [feature request template](https://github.com/SyncTek-LLC/specterqa/issues/new?template=feature_request.yml) on GitHub Issues.

## License

By contributing to SpecterQA, you agree that your contributions will be licensed under the [MIT License](LICENSE). All contributions are subject to this project's license terms.
