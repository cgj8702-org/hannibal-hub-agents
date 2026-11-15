# Event Policy Layer — Hannibal Hub Agents

Single source of truth for what the agent may do in response to any GitHub webhook event.

The policy layer lives in `src/webhook_agent/core/policy.py` and replaces five scattered, duplicated implementations of "is this a review event?" with one pure function: `resolve_event_policy()`.

Every capability decision — can the bot submit a review? may it resolve threads? may it create issues? should it prefetch a diff? what thinking budget applies? — flows through this single function.

---

## `resolve_event_policy()`

```python
def resolve_event_policy(
    event_data: dict[str, Any],
    *,
    dry_run: bool = False,
) -> EventPolicy:
```

**Pure function:** same input always produces the same output. No I/O, no network, no GitHub API calls.

**Args:**
- `event_data`: Normalized webhook event dictionary (must include `canonical`, `sender`, `raw_payload`).
- `dry_run`: Whether the agent is in dry-run mode (suppresses mutations).

**Returns:** `EventPolicy` describing what the agent may do for this event.

---

## `EventPolicy` dataclass

| Field | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `mode` | `Mode` | — | Operational mode (see below) |
| `may_submit_review` | `bool` | `False` | May submit a formal PR review |
| `may_post_comment` | `bool` | `False` | May post a comment on PR/issue |
| `may_resolve_threads` | `bool` | `False` | May resolve review threads |
| `may_create_issue` | `bool` | `False` | May create a GitHub issue |
| `prefetch_diff` | `bool` | `False` | Should pre-fetch PR diff for context |
| `thinking_budget` | `int` | `1024` | Token budget for the model's thinking/planner |
| `session_key` | `str` | `""` | Scoped session key (`owner/repo#42`) |
| `reason` | `str` | `""` | Human-readable policy rationale |
| `is_explicit_request` | `bool` | `False` | Was this triggered by an explicit user request |

---

## `Mode` enum

| Mode | Description |
| :--- | :--- |
| `FORMAL_AUDIT` | PR lifecycle: clinical code review |
| `THREAD_COLLAB` | Inline review comment discussion |
| `ISSUE_TRIAGE` | Issue opened/commented: triage & delegate |
| `GENERAL_PAIR_PROGRAMMING` | General Q&A / chit-chat |
| `IGNORE` | Bot events, closed PRs, non-actionable events |

---

## Event classification rules

The policy function evaluates events in this order:

### 1. Formal review events (`FORMAL_AUDIT`)

Canonical events: `pull_request.opened`, `pull_request.reopened`, `pull_request.synchronize`, `pull_request.ready_for_review`, `pull_request_review_requested`

- `may_submit_review = True`, `may_post_comment = True`, `prefetch_diff = True`
- Thinking budget: `AUDITOR_THINKING_BUDGET` env var (default `4096`)
- `is_explicit_request = True`
- Only when automatic PR reviews are enabled (`ENABLE_AUTOMATIC_PR_REVIEWS=1`, the default)

> **Note:** `pull_request_review_requested` events bypass the CI gate (`review/ci_gate.py` `GATED_EVENTS` does not include it). Manual review requests proceed immediately without waiting for CI to go green.

### 2. Review intent commands (from PR comments / issue comments)

When a comment body contains a review slash command (`/review`, `/re-review`, `/force-review`, `/force_review`, etc.) or intent keywords (`please review`, `review this`, `audit this`, etc.):

- Same permissions as formal audit
- `is_explicit_request = True`
- Bypasses duplicate bot review suppression on explicit request

### 3. Thread collaboration (`THREAD_COLLAB`)

Canonical events starting with `pull_request_review_comment.` from human users:

- `may_post_comment = True`, `may_resolve_threads = True`, `prefetch_diff = True`
- Thinking budget: `512`
- Inline review comment discussion on PR threads

### 4. Issue triage (`ISSUE_TRIAGE`)

Canonical events starting with `issues.`:

- `may_post_comment = True`, `may_create_issue = True`
- Thinking budget: `1024`
- Partner bot issue events (e.g. Cline) are triaged; unprompted bot activity on other event types is ignored

### 5. General pair programming (`GENERAL_PAIR_PROGRAMMING`)

Canonical events starting with `issue_comment.` (comments on PRs or issues without review intent):

- `may_post_comment = True`, `may_create_issue = True`
- Thinking budget: `512`
- Conversational Q&A and chit-chat

### 6. Ignore (`IGNORE`)

Everything else, including:
- Bot-originated events (own bot, `coderabbitai[bot]`, `github-actions[bot]`, etc.) — unless it's a partner bot issue event
- `ping`, `unknown` events (read-only)
- Closed or merged PRs
- Events when `ALLOW_AUTOMATED_MUTATIONS` is disabled and not in dry-run

---

## Supporting constants

### `FORMAL_REVIEW_EVENTS`

```python
frozenset({
    "pull_request.opened",
    "pull_request.reopened",
    "pull_request.synchronize",
    "pull_request.ready_for_review",
    "pull_request_review_requested",
})
```

Canonical PR lifecycle events that trigger formal review.

### `PREFETCH_DIFF_EVENTS`

Same set as `FORMAL_REVIEW_EVENTS`. Events that carry a PR/issue context and may benefit from diff prefetch.

### `SUPPRESSED_BOT_LOGINS`

```python
frozenset({
    "coderabbitai",
    "coderabbitai[bot]",
    "github-actions[bot]",
    "github-actions",
})
```

Bot logins that are never actionable (own bot loop avoidance or automated reviewers).

### `PARTNER_BOT_LOGINS`

```python
frozenset({
    "cline-cloud[bot]",
    "cline-cloud",
})
```

Autonomous partner coding bots whose PRs are subject to zero-bypass code review. Partner bots are exempt from suppression for issue events only.

### `get_auditor_thinking_budget()`

Returns the configured thinking budget for the code auditor, reading `AUDITOR_THINKING_BUDGET` env var (default `4096`).

---

## Session key derivation

For actionable events, a session key is derived as `owner/repo#number` (e.g. `cgj8702-org/hannibal-hub-agents#42`). This scopes conversation sessions to a specific PR or issue. Raises `ValueError` if the repository full name or PR/issue number cannot be determined.

---

## Event filtering & suppression

Before policy classification, `WebhookProcessor._filter_tier_0()` (`src/webhook_agent/processor.py`) drops events at the perimeter:

### `SUPPRESSED_EVENT_NAMES`

Event names dropped before dispatch entirely:

| Event | Reason |
| :--- | :--- |
| `check_suite` | Subscribed but only `check_suite.completed` drives the CI gate; other actions are noise |
| `check_run` | Not subscribed; no payload reader exists |
| `status` | Not subscribed; no payload reader exists |
| `installation` | Installation lifecycle; no agent action |
| `installation_repositories` | Installation lifecycle; no agent action |
| `installation_target` | Installation lifecycle; no agent action |
| `pull_request_review_thread` | Review thread state changes; no agent action |
| `workflow_run` | CI infrastructure; not subscribed |
| `workflow_job` | CI infrastructure; not subscribed |
| `star`, `watch`, `fork`, `release`, `sponsorship`, `milestone` | Repository metadata noise |

### `SUPPRESSED_PR_METADATA_ACTIONS`

PR/issue metadata actions suppressed to prevent unsolicited conversational comments:

`assigned`, `auto_merge_disabled`, `auto_merge_enabled`, `converted_to_draft`, `demilestoned`, `dequeued`, `enqueued`, `labeled`, `locked`, `milestoned`, `review_request_removed`, `unassigned`, `unlabeled`, `unlocked`

### Untargeted `issues.opened`

Dropped unless the issue explicitly tags `@hannibal-hub-agents`, includes a review/triage directive, or originates from an authorized partner bot.

---

## Deterministic fast-path

Two deterministic fast-paths bypass the LLM agent entirely:

### Base branch merge sync (`review/fast_path.py` `is_base_branch_merge_sync()`)

For `pull_request.synchronize` events, returns `True` only if all checks pass:
1. Head commit has ≥ 2 parents
2. One parent equals `before_sha`, and the other satisfies `repo.compare(base_ref, base_parent).status` in `("behind", "identical")`
3. Every changed file (capped at 100) matches the exact blob SHA of `repo.get_contents(path, ref=base_parent)`

Fails closed (returns `False`) on any error.

### Dependabot dependency fast-path (`review/fast_path.py` `evaluate_dependency_fast_path()`)

For pure dependency PRs (Dependabot or lockfile updates), validates the lockfile diff deterministically. If valid, submits an `APPROVE` review without invoking the LLM agent. Falls back to full agent audit on any validation failure.

---

## Related documentation

- `docs/slash_commands_catalog.md` — Interaction modes, triggers, and routing reference
- `docs/CHECKPOINT_RESUME.md` — Review checkpoint pause & resume engine
- `review/ci_gate.py` — CI gate: hold automatic reviews until CI is green
- `review/writeback_policy.py` — Writeback policy evaluation and rate limiting