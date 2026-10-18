# Architectural Evaluation: Cloud Run Migration vs. GCE VM Worker

**Status:** Proposed Architecture  
**Author:** Antigravity & Maintainers  
**Date:** October 2026  
**Subject:** Migrating Hannibal Webhook Worker from GCE VM to Google Cloud Run  

---

## 1. Executive Summary

Hannibal Hub Agents currently runs a split-host topology:
- **Cloud Run Router (`cgj8702-webhook-agent`):** Verifies incoming GitHub webhooks and publishes payloads to a Pub/Sub topic.
- **Compute Host Worker (`chatbot-project-hannibal`):** A long-running GCE VM instance (`hannibal-hub-free` in `us-east1-d`) executing a systemd user daemon (`systemctl --user restart hannibal-webhook-agent`) that continuously pulls from the Pub/Sub queue, processes events through `WebhookProcessor`, and runs the Gemini ADK review agent.
- **API Quota Projects:** API keys resolve against `gen-lang-client-0615466973` (free tier) or `cgj8702-webhook-agent` (paid tier).

This cross-project, stateful VM setup introduces operational friction ("jankiness"): cross-project IAM complexity, IAP SSH deployment fragility, local filesystem drift, and a lack of horizontal autoscaling.

This document evaluates the trade-offs of migrating the background worker to **Google Cloud Run**, compares it against the status quo, and analyzes alternative serverless patterns.

---

## 2. In-Depth Pros & Cons

### Option A: Status Quo (GCE VM Worker in Separate GCP Project)

#### Pros
1. **Zero Execution Time Limits:** The VM pull loop is an unconstrained process. It can execute a heavy thinking-mode audit (budget 4096) with multi-turn tool loops lasting 5–10 minutes without encountering HTTP request timeouts or gateway cutoffs.
2. **Persistent Local Disk:** Fast local scratch disk with cached virtualenvs and Git repositories. No container image download or initialization delay between webhooks.
3. **Predictable Flat Base Cost:** If running on an `e2-micro` instance, it fits inside Google Cloud's Always Free tier or incurs a small, fixed monthly compute fee.
4. **No Cold Starts:** The worker process daemon is always alive and listening to the Pub/Sub subscription.

#### Cons
1. **Cross-Project Split Friction:** The VM host (`chatbot-project-hannibal`) is completely separated from the Pub/Sub queue and primary service account (`cgj8702-webhook-agent`), and the free-tier API project (`gen-lang-client-0615466973`). This mandates complex cross-project IAM role bindings, fragmented Cloud Logging streams across multiple GCP consoles, and cross-project secret resolution rules.
2. **Stateful Pet Maintenance:** A VM is a "pet," not "cattle." It requires OS security patching, disk cleanup (preventing `/tmp` or Docker caches from filling up), and systemd user unit management (`systemctl --user`).
3. **Fragile Deployment Pipeline:** The deploy workflow (`.github/workflows/deploy.yml`) must connect over IAP SSH (`gcloud compute ssh --tunnel-through-iap`), run `git pull`, `uv sync`, and restart systemd. If an SSH key expires, an IAP tunnel drops, or a network blip occurs during `uv sync`, the VM is left in an indeterminate or offline state.
4. **Head-of-Line Blocking & Zero Autoscaling:** A single VM worker pulling sequentially handles concurrent bursts poorly. If multiple pull requests or synchronized commits arrive simultaneously, they must either compete for a single VM's vCPU/memory or wait in queue.

---

### Option B: Cloud Run Service (Triggered via Pub/Sub Push Subscription)

#### Pros
1. **Clean Single-Project Consolidation:** The entire pipeline (Router, Pub/Sub, Worker, Service Account, Secret Manager, Cloud Logging) can reside natively inside a single project (`cgj8702-webhook-agent`). This eliminates cross-project IAM grants, simplifies billing, and centralizes observability.
2. **Hermetic, Immutable Deployments:** GitHub Actions builds a versioned container image and deploys it to Cloud Run with automatic zero-downtime traffic switching and instant 1-click rollbacks. No live git operations or `uv sync` on disk.
3. **Elastic Autoscaling (Scale to Zero & Scale Out):**
   - **Cost Efficiency:** When no webhooks arrive, the service scales to 0 instances ($0 compute cost).
   - **Burst Parallelism:** If 5 PRs are pushed at the same instant, Cloud Run spins up 5 isolated container instances in parallel. Each PR audit receives dedicated vCPU and memory without head-of-line blocking.
4. **Clean Ephemeral Sandboxing:** Each container instance has an isolated, in-memory `/tmp` filesystem (tmpfs). Downloads and extracted PR archives (`pr_workspace`) are automatically discarded when the container completes, preventing disk bloat or cross-execution corruption.
5. **Native Secret Manager Mounting:** Secrets (`WEBHOOK_FREE_KEY`, `WEBHOOK_PAID_KEY`, `GITHUB_APP_PRIVATE_KEY`) can be mounted directly into container environment variables natively by Cloud Run, eliminating custom secret-loader scripts.

#### Cons
1. **Pub/Sub Push Ack Deadlines vs. Deep LLM Latency:**
   - Cloud Run service request timeouts can be configured up to **60 minutes** (3600 seconds).
   - However, standard Cloud Run Pub/Sub **Push subscriptions** enforce a maximum acknowledgment deadline of **10 minutes** (600 seconds).
   - *Risk:* If a large PR triggers Gemini Thinking Mode (4096 tokens), runs multi-turn tool investigations (`search_files`, `read_file`), and encounters a 65-second rate-limit retry backoff, execution could approach 6–8 minutes. If it exceeds 10 minutes, Pub/Sub will assume failure and redeliver the webhook, causing duplicate reviews unless carefully leased.
2. **Cold Starts:** Scaling to zero introduces a 4–10 second container cold-start on the first webhook (can be mitigated by setting `min-instances: 1` or optimizing the base image).
3. **CPU Throttling Model:** In Cloud Run, CPU is throttled outside active request handling unless `cpu-always-allocated` is enabled. The worker logic must run strictly within the request lifecycle.

---

## 3. Alternative Architectures

### Option C (Recommended): Cloud Run Service + Cloud Tasks Queue
Instead of routing webhooks through Pub/Sub to a push subscription:
- The Cloud Run Router verifies GitHub webhook signatures in `<50ms` and enqueues a task onto a **Cloud Tasks** queue.
- Cloud Tasks delivers an HTTP request to the Cloud Run Agent Worker.

**Why Cloud Tasks beats Pub/Sub for LLM Agents:**
1. **30-Minute HTTP Execution Timeout:** Cloud Tasks supports target execution timeouts up to 30 minutes (3x higher than Pub/Sub push's 10m limit).
2. **Rate Limiting & Concurrency Throttling:** Cloud Tasks allows setting `max-concurrent-dispatches` (e.g. max 2 concurrent reviews) to strictly avoid hitting Gemini TPM quota limits.
3. **Automatic Deduplication & Backoff:** Built-in task naming guarantees exactly-once dispatch and exponential backoff retry policies without ghost duplicates.

### Option D: Cloud Run Jobs (Eventarc Triggered)
- When a PR event arrives, the Router triggers a **Cloud Run Job** execution.
- Cloud Run Jobs run up to **24 hours**, have 100% CPU allocation throughout, and have no HTTP request timeout semantics.
- *Downside:* Starting a Cloud Run Job instance has slightly higher startup latency (~10–15s) compared to routing to a warm Cloud Run service instance.

### Option E: Unified Single Cloud Run Service
- Consolidates the Router and Agent Worker into a single codebase and single deployable service.
- The external path (`/api/webhook`) receives GitHub webhooks, returns `200 OK` to GitHub, and self-enqueues an internal task or invokes background workers via Cloud Tasks.
- Minimizes operational surface area to exactly 1 service in 1 GCP project.

---

## 4. Feature Matrix Comparison

| Feature | Option A: GCE VM | Option B: Cloud Run (Pub/Sub) | Option C: Cloud Run (Cloud Tasks) | Option D: Cloud Run Jobs |
| :--- | :--- | :--- | :--- | :--- |
| **GCP Projects Required** | 2–3 (Split) | 1 (`cgj8702-webhook-agent`) | 1 (`cgj8702-webhook-agent`) | 1 (`cgj8702-webhook-agent`) |
| **Deploy Mechanism** | IAP SSH + git pull + uv sync | Docker container build | Docker container build | Docker container build |
| **Timeout Ceiling** | Unlimited | 10 mins (Pub/Sub Ack) | 30 mins (Cloud Tasks) | 24 hours |
| **Concurrency Scaling** | Fixed (1 VM process) | Elastic Horizontal (0 to N) | Elastic with Controlled Concurrency | Elastic Horizontal |
| **Idle Cost** | Flat (~$0-$7/mo) | $0.00 (Scale to zero) | $0.00 (Scale to zero) | $0.00 (Scale to zero) |
| **Operational Complexity** | High (Stateful pet) | Low (Containerized) | Low (Containerized) | Medium (Job orchestration) |

---

## 5. Decision & Recommended Roadmap

1. **Phase 1 (Preparation):** Containerize the agent worker using the project's existing `Dockerfile` and ensure all dependencies (`uv`, `google-genai`, `PyGithub`) build cleanly in non-interactive mode.
2. **Phase 2 (Cloud Tasks Migration):** Transition from Pub/Sub to Cloud Tasks in project `cgj8702-webhook-agent`. Set dispatch concurrency to 2 to protect Gemini API quotas, and set task timeout to 15–20 minutes.
3. **Phase 3 (Deprecate GCE VM):** Decommission the `hannibal-hub-free` VM in `chatbot-project-hannibal` and sunset the `.github/workflows/deploy.yml` IAP SSH workflow in favor of a standard Cloud Run deployment action.
