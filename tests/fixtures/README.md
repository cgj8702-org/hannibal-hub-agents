# Test Fixtures (`tests/fixtures/`)

This directory contains static fixtures and canonical sample webhook payloads used for unit testing, payload normalization, and parser regression verification.

## Catalog

| Fixture File | Type | Description |
|---|---|---|
| `reply_to_bot_review_comment.json` | Webhook Payload (`issue_comment` / PR review reply) | Real GitHub webhook event payload simulating a user replying directly to an automated bot review comment on PR #160. |
| `webhook_agent_creates_comment.json` | Webhook Payload (`issue_comment` creation) | Real GitHub webhook event payload simulating the webhook agent bot creating an issue comment. |

## Usage

These fixtures are loaded in unit tests (under `tests/unit/`) to verify:
1. `processor.py` event routing and bot loop prevention logic.
2. `schemas.py` and `bot_identity.py` user login extraction and bot identification.
3. Payload normalization and deterministic idempotency hashing.
