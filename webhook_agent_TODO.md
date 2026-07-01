# Webhook agent TODO (root)

This checklist is the root copy of the starter TODO and maps the project plan to first-PR tasks.

## Phase 1 — GitHub App Setup
- [ ] Create GitHub App and record App ID
- [ ] Generate private key and store in secret manager
- [ ] Generate webhook secret and store in secret manager
- [ ] Choose minimal permissions and installations to test

## Phase 2 — Webhook Receiver
- [ ] Implement raw-body reading and HMAC verification (see `src/webhook_agent/app.py`)
- [ ] Capture `X-GitHub-Delivery` and use it as idempotency key
- [ ] Return 202 after enqueueing
- [ ] Add health endpoint (done)

## Phase 3 — Durable Queue
- [ ] Select broker (options: Redis+RQ, Redis+RQScheduler, Cloud Tasks, Pub/Sub, SQS)
- [ ] Implement enqueue client with retry/backoff
- [ ] Implement dead-letter sink for permanent failures
- [ ] Persist delivery id to prevent double-processing

## Phase 4 — Worker & GitHub Auth
- [ ] Implement JWT generation from app private key
- [ ] Exchange JWT for installation token and cache until expiry
- [ ] Worker process that consumes queue and runs agent core

## Phase 5 — Agent Core
- [ ] Define stable system instruction and tool schemas
- [ ] Implement strict argument validation before executing any side-effect tool
- [ ] Preserve trace ID across queue/worker/model/writeback

## Phase 6 — GitHub Writeback
- [ ] Write issue comments and PR review comments in an idempotent way
- [ ] Gate branch writes behind explicit policy and human review

## Ops and Acceptance
- [ ] Structured logging for each lifecycle step
- [ ] Retry transient failures, surface permanent failures
- [ ] Ensure webhook ACK within 10s under normal load
- [ ] End-to-end test: webhook → queue → worker → GitHub writeback (stubbed)
