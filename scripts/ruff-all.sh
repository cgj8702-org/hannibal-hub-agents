#!/bin/bash
# Ruff-All: Clinical Linting & Formatting
# Usage: bash .agents/scripts/ruff-all.sh

echo "› Running Ruff Linter (Fixing auto-fixable issues)..."
# Final check to see if any issues remain
if ! uv run ruff check --fix --unsafe-fixes; then
    echo "------------------------------------------------------------"
    echo "[!] Clinical Violation: Ruff found remaining linting issues."
    echo "    Please fix the errors above before committing."
    echo "------------------------------------------------------------"
    exit 1
fi

echo "› Running Ruff Formatter..."
uv run ruff format

echo "› Linting & Formatting Complete. Code is clinical."
exit 0
