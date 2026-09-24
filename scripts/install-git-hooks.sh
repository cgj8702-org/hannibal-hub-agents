#!/usr/bin/env bash
# Install this repository's tracked Git hooks for the current clone.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [[ ! -x .githooks/pre-commit ]]; then
    echo "[!] Tracked pre-commit hook is missing or not executable: .githooks/pre-commit" >&2
    exit 1
fi

git config --local core.hooksPath .githooks

CONFIGURED_PATH="$(git config --local --get core.hooksPath)"
if [[ "$CONFIGURED_PATH" != ".githooks" ]]; then
    echo "[!] Failed to install .githooks as core.hooksPath." >&2
    exit 1
fi

echo "[OK] Installed tracked Git hooks for this clone (core.hooksPath=.githooks)."
