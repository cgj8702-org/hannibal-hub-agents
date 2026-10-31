# Log Sink Setup for GCS Bucket Diversion

This document explains how to optionally divert debug logs (including model thoughts and responses) to a Google Cloud Storage (GCS) bucket using Cloud Logging log sinks.

## Overview

When `LOG_MODEL_THOUGHTS=1` is enabled, the application logs model prompts, responses, and thoughts at DEBUG level to Cloud Logging. For long-term storage, analysis, or compliance, you can create a log sink to export these logs to a GCS bucket.

## Prerequisites

- Google Cloud project with Cloud Logging API enabled
- Appropriate IAM permissions:
  - `logging.sinks.create`
  - `logging.sinks.update`
  - `storage.buckets.create` (if creating a new bucket)
  - `storage.objects.create` (for the sink's service account)

## Option 1: Create Log Sink via gcloud CLI

### 1. Create a GCS Bucket (if not exists)

```bash
# Replace with your desired bucket name and location
export LOGS_BUCKET_NAME="hannibal-hub-agent-logs"
export BUCKET_LOCATION="us-east1"

gcloud storage buckets create gs://${LOGS_BUCKET_NAME} \
    --location=${BUCKET_LOCATION} \
    --uniform-bucket-level-access
```

### 2. Create the Log Sink

```bash
# Replace with your project ID
export PROJECT_ID="cgj8702-webhook-agent"
export SINK_NAME="hannibal-model-thoughts-sink"

gcloud logging sinks create ${SINK_NAME} \
    storage.googleapis.com/${LOGS_BUCKET_NAME} \
    --project=${PROJECT_ID} \
    --log-filter='logName="projects/${PROJECT_ID}/logs/python" AND severity>=DEBUG AND labels.python_logger=~"webhook_agent.core.callbacks"'
```

### 3. Grant the Sink's Service Account Permission to Write to the Bucket

```bash
# Get the sink's service account
SINK_SA=$(gcloud logging sinks describe ${SINK_NAME} --project=${PROJECT_ID} --format="value(writerIdentity)")

# Grant storage.objectCreator role
gcloud storage buckets add-iam-policy-binding gs://${LOGS_BUCKET_NAME} \
    --member=${SINK_SA} \
    --role=roles/storage.objectCreator \
    --project=${PROJECT_ID}
```

## Option 2: Create Log Sink via Terraform

If you use Terraform for infrastructure management:

```hcl
# main.tf
resource "google_storage_bucket" "agent_logs" {
  name          = "hannibal-hub-agent-logs"
  location      = "US-EAST1"
  force_destroy = true

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 30  # Retain logs for 30 days
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_logging_project_sink" "model_thoughts" {
  name        = "hannibal-model-thoughts-sink"
  destination = "storage.googleapis.com/${google_storage_bucket.agent_logs.name}"
  filter      = <<-EOT
    logName="projects/${var.project_id}/logs/python"
    AND severity>=DEBUG
    AND labels.python_logger=~"webhook_agent.core.callbacks"
  EOT

  unique_writer_identity = true
}

resource "google_storage_bucket_iam_member" "sink_writer" {
  bucket = google_storage_bucket.agent_logs.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.model_thoughts.writer_identity
}
```

## Option 3: BigQuery Export (Alternative)

For analytical queries, you can also export to BigQuery:

```bash
export BQ_DATASET="agent_logs"
export BQ_TABLE="model_thoughts"

# Create dataset
bq mk --dataset ${PROJECT_ID}:${BQ_DATASET}

# Create sink to BigQuery
gcloud logging sinks create hannibal-model-thoughts-bq \
    bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${BQ_DATASET} \
    --project=${PROJECT_ID} \
    --log-filter='logName="projects/${PROJECT_ID}/logs/python" AND severity>=DEBUG AND labels.python_logger=~"webhook_agent.core.callbacks"'

# Grant permissions
SINK_SA=$(gcloud logging sinks describe hannibal-model-thoughts-bq --project=${PROJECT_ID} --format="value(writerIdentity)")
bq add-iam-policy-binding ${PROJECT_ID}:${BQ_DATASET} \
    --member=${SINK_SA} \
    --role=roles/bigquery.dataEditor
```

## Log Filter Reference

The log filter used above captures:
- **Log stream**: `python` (application logs from `setup_cloud_logging()`)
- **Severity**: `DEBUG` and above (captures DEBUG, INFO, WARNING, ERROR)
- **Logger name pattern**: `webhook_agent.core.callbacks` (where model thoughts are logged)

### Customizing the Filter

To capture all DEBUG logs from the webhook agent:
```bash
--log-filter='logName="projects/${PROJECT_ID}/logs/python" AND severity>=DEBUG AND labels.python_logger=~"webhook_agent\."'
```

To capture only model thoughts and responses:
```bash
--log-filter='logName="projects/${PROJECT_ID}/logs/python" AND severity=DEBUG AND jsonPayload.event_type=~"model_(prompt|response)"'
```

## Verification

### Check Sink Status
```bash
gcloud logging sinks describe ${SINK_NAME} --project=${PROJECT_ID}
```

### View Exported Logs in GCS
```bash
# List objects in the bucket
gcloud storage ls gs://${LOGS_BUCKET_NAME}/

# Download a log file
gcloud storage cp gs://${LOGS_BUCKET_NAME}/<log-file>.json .
```

### Test the Setup
```bash
# Enable model thoughts logging locally
export LOG_MODEL_THOUGHTS=1
export LOG_LEVEL=DEBUG

# Run the worker (will send DEBUG logs to Cloud Logging)
uv run python main.py

# Check Cloud Logging for the logs
gcloud logging read 'logName="projects/${PROJECT_ID}/logs/python" AND labels.python_logger="webhook_agent.core.callbacks"' \
    --project=${PROJECT_ID} \
    --freshness=5m \
    --limit=10
```

## Cost Considerations

- **Cloud Logging ingestion**: First 50 GiB/month free, then $0.50/GiB
- **GCS storage**: ~$0.020/GiB/month (Standard storage)
- **Log sink export**: No additional charge for the sink itself
- **BigQuery storage**: ~$0.020/GiB/month + query costs

For model thoughts logging, expect ~1-10 MB per agent run depending on prompt/response size.

## Disabling the Sink

```bash
# Disable the sink (stops exporting)
gcloud logging sinks update ${SINK_NAME} --disabled --project=${PROJECT_ID}

# Delete the sink
gcloud logging sinks delete ${SINK_NAME} --project=${PROJECT_ID}
```

## Environment Variables Summary

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_MODEL_THOUGHTS` | `0` | Enable debug logging of model prompts, responses, and thoughts |
| `LOG_LEVEL` | `INFO` | Global log level (auto-set to DEBUG when LOG_MODEL_THOUGHTS=1) |
| `PUBSUB_PROJECT` | `cgj8702-webhook-agent` | GCP project for Cloud Logging |

## Integration with Observability Stack

This log sink complements the existing observability tiers:

| Tier | Purpose | Model Thoughts |
|------|---------|----------------|
| Cloud Trace | Distributed tracing | No (controlled by `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`) |
| Prompt-Response Logging | GCS/BigQuery completions | Yes (full content via `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK=upload`) |
| **Log Sink (this doc)** | **Custom GCS export** | **Yes (DEBUG level, structured JSON)** |
| BigQuery Agent Analytics | Structured agent events | Via ADK plugin |

The log sink approach gives you full control over retention, format, and access patterns for model thoughts logs.