# Test Fixtures (`tests/fixtures/`)

This directory contains static fixtures and canonical sample webhook payloads used for unit testing, payload normalization, and parser regression verification.

## Catalog

| Fixture File | Type | Description |
|---|---|---|
| `reply_to_bot_review_comment.json` | Webhook Payload (`issue_comment` / PR review reply) | Real GitHub webhook event payload simulating a user replying directly to an automated bot review comment on PR #160. |
| `webhook_agent_creates_comment.json` | Webhook Payload (`issue_comment` creation) | Real GitHub webhook event payload simulating the webhook agent bot creating an issue comment. |

## Status of the two legacy webhook fixtures

> ⚠️ **Currently unreferenced.** Neither `reply_to_bot_review_comment.json` nor
> `webhook_agent_creates_comment.json` is loaded by any test — nothing globs this directory
> either. They are retained as reference payloads only. Either wire them into a test or
> remove them; the newer convention below is name-addressed, which makes the same drift
> visible rather than silent.

## Webhook Payload Fixtures (`tests/fixtures/webhooks/`)

Raw webhook payload bodies, recorded from real GitHub deliveries, named `<event>.<action>.json`.

**The filename is the only metadata a test needs.** The event and action are derivable from it, and the worker's input envelope carries `event_name` and `delivery_id` as flat strings — so request headers are deliberately *not* recorded.

Recorded from the App deliveries API, which requires **App-level** JWT auth (it rejects installation tokens, GitHub App user tokens, and fine-grained PATs).

| Fixture File | Event | Action | Purpose |
|---|---|---|---|
| `check_suite.completed.json` | `check_suite` | `completed` | CI gate evaluation tests |
| `issue_comment.created.json` | `issue_comment` | `created` | Review intent / conversational thread tests |
| `issue_comment.edited.json` | `issue_comment` | `edited` | Comment update routing and suppression |
| `pull_request.closed.json` | `pull_request` | `closed` | PR closure short-circuit tests |
| `pull_request.opened.json` | `pull_request` | `opened` | Clinical review trigger tests |
| `pull_request.synchronize.json` | `pull_request` | `synchronize` | Push/sync incremental review tests |
| `pull_request_review.submitted.json` | `pull_request_review` | `submitted` | Review submitted lifecycle tests |
| `pull_request_review_comment.created.json` | `pull_request_review_comment` | `created` | Inline thread collaboration tests |
| `pull_request_review_thread.resolved.json` | `pull_request_review_thread` | `resolved` | Thread resolution lifecycle tests |

Regenerate (network required; never from a test):

```bash
uv run python dev/record_webhook_fixtures.py --list
uv run python dev/record_webhook_fixtures.py pull_request.opened issue_comment.created
```

The deliveries API returns the payload as *parsed JSON*, so the original signed bytes are not recoverable. These fixtures are for routing, normalization, and suppression tests — **not** for signature verification. Construct synthetic bodies and a test secret for that.

---

## Review Replay Fixtures (`tests/fixtures/reviews/`)

Recorded bot reviews and PR metadata used by the offline replay harness (`tests/unit/review/test_replay_harness.py`) and baseline evaluator (`dev/replay_baseline.py`). These are **evidence**, not hand-written data: do not edit the review bodies.

| Fixture File | Type | Description |
|---|---|---|
| `reviews/pr_241.json` | PR metadata + changed paths + bot reviews | The prior-decision case: bot approved removing `merge_pr` as a Zero-Bypass violation. |
| `reviews/pr_258.json` | PR metadata + full per-file patches + bot reviews | The known-bad case: two bot reviews of PR #258 with patch grounding tests. |
| `reviews/pr_260.json` | PR metadata + diffs + bot reviews | Clean approval replay with metadata parity assertions. |
| `reviews/pr_360.json` | PR metadata + diffs + bot reviews | AST import tracking and test boundary verification. |
| `reviews/pr_361.json` | PR metadata + diffs + bot reviews | Soft issue citation evaluation on diff context lines. |
| `reviews/pr_364.json` | PR metadata + diffs + bot reviews | Large multi-file diff grounding validation. |
| `reviews/pr_365.json` | PR metadata + diffs + bot reviews | Multi-turn review comment claim validation. |
| `reviews/pr_368.json` | PR metadata + diffs + bot reviews | Complex patch citation overlap checks. |
| `reviews/pr_369.json` | PR metadata + diffs + bot reviews | Re-review verdict resolution tracking. |

### Running Replay Evaluation

```bash
# Run baseline evaluation across all recorded review fixtures
uv run python dev/replay_baseline.py

# Run offline replay unit test harness
uv run pytest tests/unit/review/test_replay_harness.py
```

Regenerate (network required; never from a test):

```bash
uv run python dev/record_review_fixtures.py 258 --patches full
uv run python dev/record_review_fixtures.py 241 --patches none
```
