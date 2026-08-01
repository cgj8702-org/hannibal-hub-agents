---
name: gcloud-logging
description: "Clinical operational protocol and command catalog for Google Cloud Logging, covering Cloud Run, Python app streams, audit logs, and project disambiguation."
---

<div align="center">

# 🛠️ `GCloud Logging`
*Clinical Operational Protocol for Google Cloud Project Logs* ⚡️

</div>

---

## 🎯 Purpose & Scope

This skill governs log inspection, troubleshooting, and diagnostic workflows across Google Cloud Projects in the Hannibal ecosystem. Use this protocol whenever:
* Investigating webhook delivery or routing failures.
* Debugging Python worker / ADK agent execution streams.
* Diagnosing Cloud Run readiness probe timeouts or HTTP errors.
* Verifying IAM or SecretManager audit activities.

---

## 🗺️ Project Reference Matrix

Always verify your active gcloud project configuration prior to executing log queries:

```bash
gcloud config list
```

| Component / Resource | GCP Project ID | Log Stream Name / Type |
| :--- | :--- | :--- |
| **Cloud Run Router** | `cgj8702-webhook-agent` | `run.googleapis.com%2Frequests`<br>`run.googleapis.com%2Fvarlog%2Fsystem` |
| **Python Worker & Agent** | `cgj8702-webhook-agent` | `projects/cgj8702-webhook-agent/logs/python` |
| **SecretManager & IAM Audit** | `cgj8702-webhook-agent` | `cloudaudit.googleapis.com%2Factivity`<br>`cloudaudit.googleapis.com%2Fdata_access` |
| **GCE Infrastructure VM** | `chatbot-project-hannibal` | `gce_instance` |

---

## ⏱️ Time-Filtering Protocol

Support both absolute ISO 8601 timestamps and relative offsets depending on diagnostic context.

### 1. Relative Time Queries (Real-Time Debugging)
Use relative offsets when inspecting recent activity:
```bash
# Query python logs from the past 15 minutes
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/python" AND timestamp >= "-15m"' --project=cgj8702-webhook-agent --order=asc --limit=100
```

### 2. Absolute Bounded Window Queries (Historical Traceability)
For historical incident analysis, specify both a start (`timestamp >= ...`) and optional end bound (`timestamp <= ...`) to prevent pagination traps:
```bash
# Bounded time window query
gcloud logging read 'timestamp >= "2026-07-31T20:22:44Z" AND timestamp <= "2026-07-31T23:59:59Z"' --project=cgj8702-webhook-agent --order=asc --limit=500
```

---

## 📚 Core Command Catalog

### 1. Python Application Logs (`worker` & `processor`)
Inspect python logging streams produced by `setup_cloud_logging()`:
```bash
# View all python logger messages in ascending order
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/python"' --project=cgj8702-webhook-agent --order=asc --limit=200

# Filter specifically by logger name (e.g. worker or processor)
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/python" AND labels.python_logger="processor"' --project=cgj8702-webhook-agent --order=asc --limit=100
```

### 2. Cloud Run HTTP Request Logs
Inspect inbound HTTP webhook deliveries arriving at `webhook-router`:
```bash
# View HTTP POST request logs with status codes and latencies
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/run.googleapis.com%2Frequests"' --project=cgj8702-webhook-agent --order=asc --limit=100
```

### 3. Cloud Run Container & Probe System Logs
Diagnose container autoscaling and readiness probe health:
```bash
# View Cloud Run container startup and readiness probe output
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/run.googleapis.com%2Fvarlog%2Fsystem"' --project=cgj8702-webhook-agent --order=asc --limit=100
```

### 4. Cloud Audit Activity Logs
Inspect SecretManager version updates or IAM service account credentials:
```bash
# View audit logs for SecretManager or IAM operations
gcloud logging read 'logName="projects/cgj8702-webhook-agent/logs/cloudaudit.googleapis.com%2Factivity"' --project=cgj8702-webhook-agent --order=asc --limit=50
```

### 5. Severity-Filtered Queries
Isolate errors and warnings across all project log streams:
```bash
# Isolate WARNING and ERROR severity logs
gcloud logging read 'severity>=WARNING' --project=cgj8702-webhook-agent --limit=50
```

---

## 🚨 Known Gotchas & Troubleshooting Protocol

### 1. Python Cloud Logging Level Default (`INFO` vs `DEBUG`)
* **Gotcha:** Calling `client.setup_logging()` in `google.cloud.logging` defaults to `logging.INFO`.
* **Impact:** Any `logger.debug(...)` calls in python source code (such as Pub/Sub message pulls or acks) will be silently filtered out by GCP Cloud Logging unless `client.setup_logging(log_level=logging.DEBUG)` is configured or `logger.info(...)` is used.

### 2. Active Agent Delegation vs. Stubbed Processor
* **Gotcha:** Ensure `process_event()` in `processor.py` actively instantiates `AgentCore` and executes `agent.run(...)`.
* **Impact:** If `process_event()` only logs `"Event processed: ..."` without calling `AgentCore`, the worker will act as a stub and silently drop event execution.

### 3. Pagination & Unbounded Query Traps
* **Gotcha:** Running `gcloud logging read 'timestamp >= ...'` with `--order=asc` and high limits over large date ranges can cause GCP API pagination to return earlier high-volume log streams instead of target entries.
* **Remedy:** Combine `logName` filtering with bounded timestamp ranges (`timestamp >= ... AND timestamp <= ...`).
