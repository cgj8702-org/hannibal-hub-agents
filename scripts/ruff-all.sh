#!/bin/bash
# Ruff-All: Clinical Linting, Formatting & Type Checking
# Usage: bash scripts/ruff-all.sh

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

echo "› Running MyPy Static Type Check..."
if ! uv run mypy; then
    echo "------------------------------------------------------------"
    echo "[!] Clinical Violation: MyPy found static typing issues."
    echo "    Please fix the errors above before committing."
    echo "------------------------------------------------------------"
    exit 1
fi

echo "› Linting, Formatting & Type Validation Complete. Code is clinical."
exit 0
