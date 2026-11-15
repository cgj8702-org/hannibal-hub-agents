#!/bin/bash
# Clinical Linting, Formatting & Type Checking

VENV_BIN="/home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/.venv/bin"
if [ -x "$VENV_BIN/ruff" ]; then
    RUFF="$VENV_BIN/ruff"
    MYPY="$VENV_BIN/mypy"
else
    RUFF="uv run ruff"
    MYPY="uv run mypy"
fi

echo "› Running Ruff Linter (Safe auto-fixes only)..."
if ! $RUFF check --fix; then
    echo "------------------------------------------------------------"
    echo "[!] Clinical Violation: Ruff found remaining linting issues."
    echo "    Please fix the errors above before committing."
    echo "------------------------------------------------------------"
    exit 1
fi

echo "› Running Ruff Formatter..."
$RUFF format

echo "› Running MyPy Static Type Check..."
if ! $MYPY --show-error-codes; then
    echo "------------------------------------------------------------"
    echo "[!] Clinical Violation: MyPy found static typing issues."
    echo "    Please fix the errors above before committing."
    echo "------------------------------------------------------------"
    exit 1
fi

echo "› Linting, Formatting & Type Validation Complete. Code is clinical."
exit 0
