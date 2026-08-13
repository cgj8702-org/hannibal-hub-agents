#!/usr/bin/env bash
# Load environment variables dynamically from Google Cloud Secret Manager.
# Usage: source scripts/load_secrets.sh

set -euo pipefail

PROJECT_ID="${PUBSUB_PROJECT:-cgj8702-webhook-agent}"

echo "Loading secrets from Secret Manager project [${PROJECT_ID}]..."

# GITHUB_APP_ID and GITHUB_INSTALLATION_ID are GCE VM metadata, NOT secrets.
# They are already defined in .envrc and exposed via the VM metadata server.
# Do NOT override them from Secret Manager — doing so produces empty strings
# that crash int() in processor.py (see GH issue where webhook agent broke
# after commit a073079 added cross-repo RAG_MODE gates).
# Instead, ensure they are available for the worker/processor.
if [ -z "${GITHUB_APP_ID:-}" ]; then
    echo "WARNING: GITHUB_APP_ID not set; expect it from .envrc or VM metadata"
fi
if [ -z "${GITHUB_INSTALLATION_ID:-}" ]; then
    echo "WARNING: GITHUB_INSTALLATION_ID not set; expect it from .envrc or VM metadata"
fi

# --- Secrets that actually belong in Secret Manager ---

# Fetch API keys from Secret Manager by their ACTUAL secret names:
#   - Secret Manager: WEBHOOK_FREE_KEY / WEBHOOK_PAID_KEY
#   - Code reads:     WEBHOOK_FREE_KEY / WEBHOOK_PAID_KEY (primary)
#                              FREE_KEY / PAID_KEY (secondary fallback)
#   - google.genai:   GEMINI_API_KEY / GOOGLE_API_KEY (standard)
export WEBHOOK_FREE_KEY=$(gcloud secrets versions access latest --secret="WEBHOOK_FREE_KEY" --project="${PROJECT_ID}" 2>/dev/null || echo "")
export WEBHOOK_PAID_KEY=$(gcloud secrets versions access latest --secret="WEBHOOK_PAID_KEY" --project="${PROJECT_ID}" 2>/dev/null || gcloud secrets versions access latest --secret="PAID_KEY" --project="${PROJECT_ID}" 2>/dev/null || gcloud secrets versions access latest --secret="GEMINI_API_KEY" --project="${PROJECT_ID}" 2>/dev/null || echo "")
# Mirror under legacy/secondary names so all code paths resolve the key
export FREE_KEY="${WEBHOOK_FREE_KEY}"
export PAID_KEY="${WEBHOOK_PAID_KEY}"
export GEMINI_API_KEY="${WEBHOOK_PAID_KEY:-${WEBHOOK_FREE_KEY:-}}"
export GOOGLE_API_KEY="${GEMINI_API_KEY}"

if [ -z "${GEMINI_API_KEY}" ]; then
    echo "FATAL: No API key resolved — GEMINI_API_KEY is empty after loading secrets" >&2
    exit 1
fi
export WEBHOOK_SECRET=$(gcloud secrets versions access latest --secret="WEBHOOK_SECRET" --project="${PROJECT_ID}")
if [ -z "$WEBHOOK_SECRET" ]; then
    echo "FATAL: WEBHOOK_SECRET resolved to empty from Secret Manager (project=${PROJECT_ID})" >&2
    exit 1
fi

# Save private key to ephemeral path
mkdir -p /tmp/keys
gcloud secrets versions access latest --secret="GITHUB_PRIVATE_KEY" --project="${PROJECT_ID}" > /tmp/keys/github-app-private-key.pem
chmod 600 /tmp/keys/github-app-private-key.pem
export GITHUB_PRIVATE_KEY_PATH="/tmp/keys/github-app-private-key.pem"
export PRIVATE_KEY_PATH="${GITHUB_PRIVATE_KEY_PATH}"

# Fetch webhook-agent service account key for Pub/Sub & Logging auth
gcloud secrets versions access latest --secret="WEBHOOK_AGENT_SA_KEY" --project="${PROJECT_ID}" > /tmp/keys/webhook-agent-sa-key.json
chmod 600 /tmp/keys/webhook-agent-sa-key.json
export GOOGLE_APPLICATION_CREDENTIALS="/tmp/keys/webhook-agent-sa-key.json"

export PUBSUB_PROJECT="${PROJECT_ID}"
export PUBSUB_TOPIC="projects/${PROJECT_ID}/topics/webhooks"
export PUBSUB_SUBSCRIPTION="projects/${PROJECT_ID}/subscriptions/webhooks-sub"
export PUBSUB_DEAD_LETTER_TOPIC="projects/${PROJECT_ID}/topics/webhooks-dead-letter"
export ALLOW_AUTOMATED_MUTATIONS="1"

echo "Secrets loaded into environment memory successfully."