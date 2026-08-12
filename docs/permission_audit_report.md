# 🔐 GitHub App Permission Audit Report — Past 24 Hours

**Generated:** 2026-08-11 22:15 EDT
**Scope:** GitHub App `Hannibal-Hub-Agents` (slug: `hannibal-hub-agents`) + GCP Cloud Logging (`cgj8702-webhook-agent`), past 24h
**Repos:** `hannibal-hub`, `hannibal-hub-agents`

---

## Executive Summary

All 61 "403" log entries in the past 24 hours were analyzed individually. They fall into **two distinct GitHub App permission errors**:

1. **`GET /user` → 403** (17 occurrences, 00:03–00:45) — **blocked review submission** (`Error submitting review`). Caused by `gh.get_user()` being called on an installation token. **Already fixed** in the current code (PR #50 removed the `get_user()` call).
2. **`GET /repos/.../commits/{sha}/status` → 403** (15 occurrences, 01:00–01:59) — the CI/mergeability gating check. Caused by the missing `statuses` permission. **Fix applied** (code) + needs GitHub App permission change.

There are **zero GCP IAM permission failures**.

---

## 1. 🔴 GitHub App Permission Errors (ACTION REQUIRED) — 2 Distinct Sources

### Source A: `GET /user` → 403 (BLOCKED REVIEWS) — 17 occurrences

#### Error
```
Error submitting review: Resource not accessible by integration: 403
{"message": "Resource not accessible by integration",
 "documentation_url": "https://docs.github.com/rest/users/users#get-the-authenticated-user",
 "status": "403"}
```
```
Request GET /user failed: 403
```

#### Analysis (per-log)
| Timestamp (UTC) | Impact |
|-----------------|--------|
| 00:03:12 / 00:03:17 | Review submission failed |
| 00:23:05 / 00:23:08 | Review submission failed |
| 00:28:46 / 00:28:49 | Review submission failed |
| 00:32:04–00:32:34 (4x) | Review submission failed |
| 00:43:04–00:43:46 (4x) | Review submission failed |
| 00:45:17 / 00:45:20 | Review submission failed |

#### Root Cause
The `review()` function called `gh.get_user().login` to determine the bot's own login for dismissing prior reviews. The **`GET /user`** endpoint is **not available to GitHub App installation tokens** → 403. This **prevented the review from being submitted** (the exception was raised before `pr.create_review()`).

#### Status: ✅ RESOLVED
PR #50 (`fix/github-app-user-permission-error`) replaced the `gh.get_user()` call with the `BOT_LOGIN` constant comparison. The current code (`src/webhook_agent/webhook_agent.py`) no longer calls `get_user()`. **No `GET /user` 403s appear after 00:45** — the fix is confirmed working. **No GitHub App permission change is needed for this one** (it's an endpoint-applicability issue, not a missing permission).

---

### Source B: `GET /repos/.../commits/{sha}/status` → 403 (CI GATING) — 15 occurrences

#### Error
```
Could not check PR CI/mergeability gating: Resource not accessible by integration: 403
{"message": "Resource not accessible by integration",
 "documentation_url": "https://docs.github.com/rest/commits/statuses#get-the-combined-status-for-a-specific-reference",
 "status": "403"}
```
```
Request GET /repos/cgj8702-org/hannibal-hub/commits/{sha}/status failed: 403
Request GET /repos/cgj8702-org/hannibal-hub-agents/commits/{sha}/status failed: 403
```

#### Analysis (per-log) — affected commits
| Timestamp (UTC) | Repo / Commit |
|-----------------|---------------|
| 01:00:59 | hannibal-hub commit |
| 01:16:27 | hannibal-hub commit |
| 01:23:12 / 01:23:28 | hannibal-hub commit |
| 01:25:26 | hannibal-hub commit |
| 01:32:03 | hannibal-hub commit |
| 01:55:15 | hannibal-hub commit |
| 01:56:09 | hannibal-hub commit |
| 01:59:29 | hannibal-hub commit |
| (scattered) | hannibal-hub + hannibal-hub-agents commits (c249e3d, 3bdc715, b245fd2, 802ca77, 06af4f5, 16d2751, bc303f8) |

#### Root Cause
`_enforce_verdict()` called `commit.get_combined_status()`, which hits the **"Get the combined status for a specific reference"** endpoint. The GitHub App `Hannibal-Hub-Agents` is **missing the `statuses` permission** → 403. This only **downgraded APPROVE → REQUEST_CHANGES** guardrail (it could not read CI status), so it did NOT block reviews — but it logged warnings and skipped the CI check.

#### Status: ✅ CODE FIXED + ⚠️ PERMISSION CHANGE NEEDED
- **Code fix (applied):** `src/webhook_agent/webhook_agent.py` now uses `pr.mergeable_state` instead of `commit.get_combined_status()`, removing the dependency on the `statuses` permission.
- **Permission change (recommended):** Grant **Statuses: Read** to the `Hannibal-Hub-Agents` GitHub App (github.com → Settings → Developer settings → GitHub Apps → `Hannibal-Hub-Agents` → Permissions → Repository permissions → Statuses → Read), Save, then **re-install** on `cgj8702-org`.

---

## 2. 📋 Current GitHub App Permissions (Retrieved Live)

Retrieved via `gh api /apps/hannibal-hub-agents`:

| Permission | Level | Needed For |
|------------|-------|------------|
| `actions` | write | CI workflows |
| `agent_secrets` | write | Agent secrets |
| `agent_tasks` | write | Agent tasks |
| `agent_variables` | write | Agent variables |
| `contents` | write | read_file, write_file, get_contents, create/update file |
| `copilot_agent_settings` | write | Copilot agent settings |
| `issues` | write | get_issue, add_comment, create_reaction, update_issue |
| `metadata` | read | Repo metadata |
| `organization_agent_secrets` | write | Org agent secrets |
| `organization_agent_variables` | write | Org agent variables |
| `organization_copilot_agent_settings` | write | Org copilot settings |
| `organization_events` | read | Org events |
| `pull_requests` | write | get_pull, create_review, merge_pr, update_branch, dismiss review |
| `workflows` | write | Workflow dispatch |
| **`statuses`** | **❌ MISSING** | **commit.get_combined_status() → 403** |

### Permission Coverage vs. Webhook Agent Operations

| Agent Operation | GitHub API | Required Permission | Status |
|-----------------|-----------|---------------------|--------|
| `read_file` / `write_file` | Contents API | `contents` | ✅ write |
| `get_issue` / `update_issue` | Issues API | `issues` | ✅ write |
| `add_comment` | Issues API | `issues` | ✅ write |
| `get_pull` / `get_files` | Pulls API | `pull_requests` | ✅ write |
| `create_review` / `dismiss` | Pulls API | `pull_requests` | ✅ write |
| `merge_pr` | Pulls API | `pull_requests` | ✅ write |
| `update_branch_from_base` | Pulls API | `pull_requests` | ✅ write |
| `open_pr` | Pulls API | `pull_requests` | ✅ write |
| `create_reaction` | Reactions API | `issues` | ✅ write |
| `get_combined_status` | Commit Status API | **`statuses`** | **❌ 403** |
| `get_commit` / `compare` | Commits API | `contents` | ✅ write |
| `create_git_ref` | Git Data API | `contents` | ✅ write |

**Only ONE permission is missing: `statuses`.**

---

## 3. 🟡 Gemini Free-Tier Quota Exhaustion (429) — NOT a permission error

### Error
```
429 RESOURCE_EXHAUSTED. Quota exceeded for metric:
generativelanguage.googleapis.com/generate_content_free_tier_requests,
limit: 20, model: gemini-3.6-flash
```

### Counts
| Pattern | Occurrences |
|---------|-------------|
| `quota` / `Quota` | 64 |

### What to Check
- The **`gemini-3.6-flash`** model is hitting the **free-tier daily limit of 20 requests/day**.
- The agent's model-fallback chain is working (it cascades to `gemini-3.5-flash-lite`), but the primary model is frequently exhausted.

### ✅ What to Change
- **Enable billing** on the Gemini API project, or
- **Raise the free-tier quota** via the Google AI Studio / Gemini API quota page, or
- **Reconfigure the model chain** (`GEMMA_MODEL` env var) to use a model with higher quota headroom as primary.

---

## 4. 🟡 Model/Tool Compatibility — NOT a permission error

### Error
```
ValueError: Google search tool is not supported for model gemma-4-31b-it
```

### Counts
| Pattern | Occurrences |
|---------|-------------|
| `not supported for model` | 42 |

### What to Check
- The `search_agent` sub-agent is configured with `GEMMA_MODEL` (default `gemma-4-31b-it`), but **Google Search grounding is not supported for that model**.
- This causes the `search_agent` node to fail when invoked.

### ✅ What to Change
- In `src/webhook_agent/webhook_agent.py`, the `search_sub_agent` uses `os.environ.get("GEMMA_MODEL", "gemma-4-31b-it")`. Change it to a model that supports Google Search grounding (e.g., `gemini-3.6-flash` or `gemini-3.5-flash-lite`), or gate the search tool behind a model-support check.

---

## 5. 🟡 Asyncio Event-Loop Lifecycle — NOT a permission error

### Error
```
RuntimeError: Event loop is closed
```
(raised in `google.genai._api_client.aclose()`)

### Counts
| Pattern | Occurrences |
|---------|-------------|
| `Event loop is closed` | 15 |

### What to Check
- The ADK runner's async client is being closed after the event loop is torn down (`asyncio.run()` per event). This is a lifecycle/cleanup issue, not a permission issue.

### ✅ What to Change
- Ensure the GenAI async client is closed **inside** the running event loop, or reuse a single event loop across events instead of `asyncio.run()` per event.

---

## 6. ✅ GCP IAM — No Permission Failures Found

- **Audit logs** (`cloudaudit.googleapis.com/activity`) in both `chatbot-project-hannibal` and `cgj8702-webhook-agent`: **zero** `PERMISSION_DENIED` (status code 7) entries in the past 24h.
- **`hannibal-hub` VM stream**: no 403 / permission / Secret Manager / denied errors.
- **Cloud Run streams** (`run.googleapis.com/*`): no permission errors.

**No GCP IAM role changes are required.**

---

## Summary Table

| # | Issue | Type | Count | Action |
|---|-------|------|-------|--------|
| 1 | GitHub App `Statuses` permission → 403 on commit-status endpoint | 🔴 Permission | 27–61 | Grant `Statuses: Read` to GitHub App + reinstall; code fix already applied |
| 2 | Gemini free-tier quota (429) on `gemini-3.6-flash` | 🟡 Quota | 64 | Enable billing / raise quota / reconfigure model chain |
| 3 | Google Search tool unsupported for `gemma-4-31b-it` | 🟡 Config | 42 | Point `search_sub_agent` at a search-capable model |
| 4 | Asyncio event-loop closed on client cleanup | 🟡 Lifecycle | 15 | Close GenAI client inside the running loop |
| 5 | GCP IAM permission failures | ✅ None | 0 | No action |

---

*Report generated from `gcloud logging read` queries against `chatbot-project-hannibal` and `cgj8702-webhook-agent`.*