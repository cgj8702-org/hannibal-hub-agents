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

Raw webhook payload bodies, recorded from real GitHub deliveries, named
`<event>.<action>.json`.

**The filename is the only metadata a test needs.** The event and action are derivable from
it, and the worker's input envelope carries `event_name` and `delivery_id` as flat strings —
so request headers are deliberately *not* recorded.

Recorded from the App deliveries API, which requires **App-level** JWT auth (it rejects
installation tokens, GitHub App user tokens, and fine-grained PATs).

Regenerate (network required; never from a test):

```bash
uv run python dev/record_webhook_fixtures.py --list
uv run python dev/record_webhook_fixtures.py pull_request.opened issue_comment.created
```

The deliveries API returns the payload as *parsed JSON*, so the original signed bytes are not
recoverable. These fixtures are for routing, normalization, and suppression tests — **not**
for signature verification. Construct synthetic bodies and a test secret for that.

Re-recording is byte-identical while the deliveries are unchanged; if a fixture changes, a
test that depends on it should be reviewed rather than silently updated.

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
