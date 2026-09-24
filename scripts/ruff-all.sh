#!/bin/bash
# Ruff-All: Clinical Linting, Formatting & Type Checking
# Usage:
#   bash scripts/ruff-all.sh           # auto-fix, then validate
#   bash scripts/ruff-all.sh --check   # validate only, no writes (CI / commit gate)

CHECK_MODE=0
if [ "${1:-}" = "--check" ]; then
    CHECK_MODE=1
fi

if [ "$CHECK_MODE" -eq 1 ]; then
    echo "› Running Ruff Linter (check-only)..."
    if ! uv run ruff check; then
        echo "------------------------------------------------------------"
        echo "[!] Clinical Violation: Ruff found linting issues."
        echo "    Run 'bash scripts/ruff-all.sh' to auto-fix before committing."
        echo "------------------------------------------------------------"
        exit 1
    fi

    echo "› Running Ruff Formatter (check-only)..."
    if ! uv run ruff format --check; then
        echo "------------------------------------------------------------"
        echo "[!] Clinical Violation: Ruff found formatting issues."
        echo "    Run 'bash scripts/ruff-all.sh' to auto-format before committing."
        echo "------------------------------------------------------------"
        exit 1
    fi
else
    echo "› Running Ruff Linter (Safe auto-fixes only)..."
    if ! uv run ruff check --fix; then
        echo "------------------------------------------------------------"
        echo "[!] Clinical Violation: Ruff found remaining linting issues."
        echo "    Please fix the errors above before committing."
        echo "------------------------------------------------------------"
        exit 1
    fi

    echo "› Running Ruff Formatter..."
    uv run ruff format
fi

echo "› Running MyPy Static Type Check..."
if ! uv run mypy --show-error-codes; then
    echo "------------------------------------------------------------"
    echo "[!] Clinical Violation: MyPy found static typing issues."
    echo "    Please fix the errors above before committing."
    echo "------------------------------------------------------------"
    exit 1
fi

echo "› Linting, Formatting & Type Validation Complete. Code is clinical."
exit 0
