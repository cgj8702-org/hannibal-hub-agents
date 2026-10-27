#!/usr/bin/env bash
# Deploy serverless GitHub webhook ingress router to Google Cloud Run.
#
# Usage:
#   ./scripts/deploy_router.sh [PROJECT_ID] [REGION]
#
# Defaults:
#   PROJECT_ID: cgj8702-webhook-agent
#   REGION:     us-east1
#   SERVICE:    webhook-router

set -euo pipefail

PROJECT="${1:-${PUBSUB_PROJECT:-${GCP_PROJECT_ID:-cgj8702-webhook-agent}}}"
REGION="${2:-${GCP_REGION:-us-east1}}"
SERVICE_NAME="webhook-router"

echo "Deploying ${SERVICE_NAME} to Cloud Run in project [${PROJECT}], region [${REGION}]..."

gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --source="router" \
  --allow-unauthenticated \
  --service-account="webhook-agent-sa@${PROJECT}.iam.gserviceaccount.com" \
  --set-env-vars="PUBSUB_PROJECT=${PROJECT},PUBSUB_TOPIC=webhooks" \
  --set-secrets="WEBHOOK_SECRET=WEBHOOK_SECRET:latest" \
  --max-instances=1 \
  --concurrency=80 \
  --timeout=300 \
  --memory=512Mi \
  --cpu=1000m

echo "Deployment complete for ${SERVICE_NAME}."
