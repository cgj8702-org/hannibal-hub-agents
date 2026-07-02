# Webhook agent TODO (root)

This checklist is the root copy of the starter TODO and maps the project plan to first-PR tasks.

## Phase 1 — GitHub App Setup
- [x] Create GitHub App and record App ID
- [x] Generate private key and store in secret manager
- [x] Generate webhook secret and store in secret manager
- [x] Choose minimal permissions and installations to test

## Phase 2 — Webhook Receiver
- [x] Implement raw-body reading and HMAC verification (implemented in `src/webhook_agent/app.py`)
- [x] Capture `X-GitHub-Delivery` and use it as idempotency key
- [x] Return 202 after enqueueing
- [x] Add health endpoint (done)

## Phase 3 — Durable Queue
- [x] Select broker (options: Redis+RQ, Redis+RQScheduler, Cloud Tasks, Pub/Sub, SQS) (Pub/Sub selected and integrated)
- [x] Implement enqueue client with retry/backoff
- [x] Implement dead-letter sink for permanent failures
- [ ] Persist delivery id to prevent double-processing (idempotency key is captured, persistence storage to be integrated)

## Phase 4 — Worker & GitHub Auth
- [x] Implement JWT generation from app private key
- [x] Exchange JWT for installation token and cache until expiry
- [x] Worker process that consumes queue and runs agent core

## Phase 5 — Agent Core
- [x] Define stable system instruction and tool schemas
- [x] Implement strict argument validation before executing any side-effect tool
- [x] Preserve trace ID across queue/worker/model/writeback

## Phase 6 — GitHub Writeback
- [x] Write issue comments and PR review comments in an idempotent way
- [x] Gate branch writes behind explicit policy and human review

## Ops and Acceptance
- [x] Structured logging for each lifecycle step
- [x] Retry transient failures, surface permanent failures
- [x] Ensure webhook ACK within 10s under normal load
- [ ] End-to-end test: webhook → queue → worker → GitHub writeback (integration verification pending)
