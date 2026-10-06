# Firestore Review Checkpoint: Pause & Resume Engine

This document details the architecture, durability invariants, schema lifecycle, and worker sweep mechanism for the GitHub Pull Request review pause & resume engine in `hannibal-hub-agents`.

---

## 🎯 Purpose & Goals

When model quotas are exhausted (`429 RESOURCE_EXHAUSTED`), webhook orchestrators face a difficult choice:
1. Drop the event, forcing the developer to re-trigger or wait indefinitely for another commit.
2. Incessantly retry immediately, compounding upstream API exhaustion and wasting daily request limits.
3. Keep the Pub/Sub message unacknowledged, leading to rapid ack deadline timeouts, duplicate deliveries, and premature dead-lettering.

The **Pause & Resume Engine** solves this by durably recording pre-audited PR review state (diff, deterministic AST findings, Symbol Impact graphs, and delivery metadata) in a 14-day sliding TTL Firestore collection. When rate limits or backoff windows expire, the orchestrator resumes review inference without repeating expensive deterministic pre-audits or GitHub diff fetching.

---

## 💾 Schema & Invariants

Review checkpoints are stored in the `review_checkpoints` Firestore collection, keyed by a canonical identifier:
```text
{sanitized_repo}__{pr_number}__{head_sha}
```

### Document Attributes

| Field | Type | Description |
| :--- | :--- | :--- |
| `repo` | string | Full repository name (`owner/repo`). |
| `pr_number` | integer | Target Pull Request number. |
| `head_sha` | string | Pinned Git commit SHA under review. |
| `canonical` | string | Canonical webhook event key (`pull_request.opened`, `pull_request.synchronize`). |
| `status` | string | Lifecycle state: `"pending"`, `"rate_limited"`, or `"completed"`. |
| `precompiled_dossier` | string | Compiled deterministic AST, symbol graph, and test impact audit text. |
| `pr_diff` | string | Cached unified diff of the PR. |
| `changed_files` | list[string] | List of files modified in the commit. |
| `resume_after` | timestamp | Earliest UTC datetime when backoff expires and inference may resume. |
| `attempts` | integer | Cumulative processing attempts for this checkpoint. |
| `delivery_id` | string | GitHub webhook delivery ID or original trace identifier. |
| `sender` | string | GitHub login of the event actor. |
| `comment_body` | string | Comment text for issue comment or review command triggers. |
| `installation_id` | integer | GitHub App installation ID. |
| `expire_at` | timestamp | 14-day sliding TTL expiration timestamp. |
| `updated_at` | timestamp | Server timestamp of last document update. |

### Core Durability Invariants

1. **Document Size Guards:** Firestore documents have a strict 1 MiB maximum size. The checkpoint manager caps `pr_diff` at 400 KiB and `precompiled_dossier` at 500 KiB using UTF-8 byte length guards. Payloads exceeding these bounds are truncated cleanly with explanatory notices.
2. **Never-Downgrade Status Invariant:** Once a checkpoint transitions to `"rate_limited"` or `"completed"`, subsequent passes (e.g. retried webhook deliveries) never overwrite the status back to `"pending"`.
3. **Exhaustion-Only Rate Limit Marking:** Rate-limited checkpoints are marked **only** when all retry attempts and dynamic model fallback chains are completely exhausted. Transient errors that succeed on model failover do not pause the review.
4. **Local Fallback Isolation:** In-memory local caching operates when Firestore is unavailable or unconfigured. In-memory checkpoints survive within the lifetime of a single worker process; cross-restart durability requires Firestore (`ENABLE_REVIEW_CHECKPOINT=1`).

---

## 🔄 Resume Flow & Architectural Decisions

### Resume Trigger: Option B (Dedicated Worker Sweep)

The orchestrator selects **Option B (Dedicated Worker Sweep)** over Option A (Pub/Sub message redelivery):

* **Why not Option A (Pub/Sub redelivery / nack):**
  - Pub/Sub subscription ack deadlines range from 10 to 600 seconds. If a message is not acknowledged, redeliveries fire almost immediately, ignoring model-specified rate-limit backoff durations (e.g. 1–5 minutes) and quickly exceeding max delivery attempts into the Dead Letter Queue.
* **Why Option B (Dedicated Sweep):**
  - Decoupled, predictable, and polite to rate limits.
  - Webhook messages are cleanly processed and acknowledged.
  - The worker runs a periodic sweep (`REVIEW_RESUME_INTERVAL_SECONDS`, default 60s) gated by `ENABLE_REVIEW_RESUME=1`.
  - Queries `status == "rate_limited"` and filters `resume_after <= now`.
  - Synthesizes a resume event (`build_resume_payload`) and passes it to `processor.process_event`.

### Fast-Path Inference Execution

When resuming:
1. `WebhookProcessor.process_event` verifies `get_resumable(repo, pr, sha)`.
2. Diff prefetch, inline comment prefetch, and bot review history prefetch are completely bypassed.
3. `core/execution.py` adopts the stored `precompiled_dossier` directly as `genai_types.Content`, bypassing AST parsing, symbol graph generation, and test impact computation.
4. The agent directly executes model inference, writes the formal review, and marks the checkpoint `"completed"`.
