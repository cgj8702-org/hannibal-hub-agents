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

## Review Replay Fixtures (`tests/fixtures/reviews/`)

Recorded bot reviews and PR metadata used by the offline replay harness
(`tests/unit/review/test_replay_harness.py`). These are **evidence**, not hand-written data:
do not edit the review bodies.

| Fixture File | Type | Description |
|---|---|---|
| `reviews/pr_258.json` | PR metadata + full per-file patches + bot reviews | The known-bad case: two bot reviews of PR #258, one of which mis-cites `github_tools.py:556` and overstates a test count. Recorded with `--patches full`. |
| `reviews/pr_241.json` | PR metadata + changed paths + bot reviews | The prior-decision case: the same bot approved removing `merge_pr` as a Zero-Bypass violation three hours before approving its restoration in #258. Recorded with `--patches none`. |

Regenerate (network required; never from a test):

```bash
python3 dev/record_review_fixtures.py 258 --patches full
python3 dev/record_review_fixtures.py 241 --patches none
```

Re-recording is byte-identical while the PRs are unchanged; if a fixture changes, a test that
depends on it should be reviewed rather than silently updated.
