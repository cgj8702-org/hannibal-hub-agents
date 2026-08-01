#!/usr/bin/env bash
# Load environment variables dynamically from Google Cloud Secret Manager.
# Usage: source scripts/load_secrets.sh

set -euo pipefail

PROJECT_ID="${PUBSUB_PROJECT:-cgj8702-webhook-agent}"

echo "Loading secrets from Secret Manager project [${PROJECT_ID}]..."

export GITHUB_APP_ID=$(gcloud secrets versions access latest --secret="GITHUB_APP_ID" --project="${PROJECT_ID}")
export GITHUB_INSTALLATION_ID=$(gcloud secrets versions access latest --secret="GITHUB_INSTALLATION_ID" --project="${PROJECT_ID}")
export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret="GEMINI_API_KEY" --project="${PROJECT_ID}")
export GOOGLE_API_KEY="${GEMINI_API_KEY}"
export WEBHOOK_SECRET=$(gcloud secrets versions access latest --secret="WEBHOOK_SECRET" --project="${PROJECT_ID}")

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
