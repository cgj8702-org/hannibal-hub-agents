#!/usr/bin/env bash
# VM User-Space Systemd Initialization Script
# Run this script on the target VM to install and start the user service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

echo "Configuring user-space systemd service for Hannibal Webhook Agent..."

mkdir -p "${SYSTEMD_USER_DIR}"

cp "${SCRIPT_DIR}/hannibal-webhook-agent.service" "${SYSTEMD_USER_DIR}/hannibal-webhook-agent.service"
echo "Copied service file to ${SYSTEMD_USER_DIR}/hannibal-webhook-agent.service"

systemctl --user daemon-reload
echo "Systemd user daemon reloaded."

if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "${USER}" || true
    echo "Linger enabled for user [${USER}]."
fi

systemctl --user enable --now hannibal-webhook-agent.service
echo "Hannibal Webhook Agent service enabled and started."

systemctl --user status hannibal-webhook-agent.service --no-pager || true
echo "Setup complete."
