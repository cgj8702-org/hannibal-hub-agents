# Investigation Report: D5 — Remaining Test-Only Functions Audit

- **Item:** `D5`
- **Scope:** 6 functions across `models/` and `review/` (~120 total lines)
- **Status:** Complete Investigation & Action Plan

---

## 1. Overview of the Problem

In `docs/plans/repo_reclamation.md` §D5, six functions were flagged as having zero callers in `src/` and existing solely to satisfy their own unit tests:

| Function | Location | Lines | Origin / Context |
| :--- | :--- | :---: | :--- |
| `_get_model_tpm_limit()` | `models/model_chain.py:215` | 29 | Registry TPM rate limit reader |
| `_count_tokens_exact()` | `models/model_chain.py:246` | 19 | GenAI API count_tokens wrapper |
| `should_suppress_round()` | `models/review_budget.py:53` | 6 | Lifetime comment budget check |
| `has_genuine_summary_risk()` | `review/schemas.py:327` | 5 | Deprecated summary risk scraper stub |
| `is_security_dimension()` | `review/review_enforcer.py:39` | 4 | Scorecard Dimension 3 predicate (see D4) |
| `is_test_coverage_dimension()` | `review/review_enforcer.py:45` | 4 | Scorecard Dimension 5 predicate (see D4) |

Leaving test-only functions in production modules inflates codebase size, increases maintenance burden, and creates false impressions that certain features are active.

---

## 2. Deep Dive Analysis by Function

### 2.1 `_get_model_tpm_limit()` (`src/webhook_agent/models/model_chain.py:215-244`)
- **What it does:** Reads `assets/registries/gemini_models.json`, finds the model entry, and extracts `rate_limits[active_tier]["tpm"]`, falling back to `15,000` (free) or `100,000` (paid).
- **Why it was written:** Written when rate limiting was envisioned as a proactive client-side token bucket based on model registry metadata.
- **Why it is test-only:** Proactive client-side TPM throttling was never integrated into the pull loop. Instead, rate limiting is handled via:
  1. `_is_transient()` with exponential jittered backoff in `worker.py`.
  2. `_advance_model_chain()` which dynamically cascades across models when a `429 RESOURCE_EXHAUSTED` is returned by the API.
  3. `_COMMENT_RATE_LIMITER` for GitHub comment throttling.
- **Callers in `src/`:** Strictly **0**.
- **Recommendation:** **Delete.** Removing `_get_model_tpm_limit()` and its test in `tests/unit/models/test_model_chain.py:187-200` removes 44 lines of dead code.

---

### 2.2 `_count_tokens_exact()` (`src/webhook_agent/models/model_chain.py:246-265`)
- **What it does:** Instantiates a new `genai.Client(api_key=active_key)` and calls `client.models.count_tokens()`, falling back to `len(text) // 4`.
- **Why it is test-only:** It is completely superseded by [`count_tokens_exact()`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/core/agent_definition.py#L115-L130) in `src/webhook_agent/core/agent_definition.py:115-130`. The `agent_definition.py` version is actively exported, reuses the shared client singleton (`get_shared_genai_client()`), and avoids creating redundant client instances.
- **Callers in `src/`:** Strictly **0**.
- **Recommendation:** **Delete.** Removing `_count_tokens_exact()` from `model_chain.py` eliminates code duplication with `agent_definition.py`.

---

### 2.3 `should_suppress_round()` (`src/webhook_agent/models/review_budget.py:53-59`)
- **What it does:** Returns `total_posted_so_far >= lifetime_cap`.
- **Why it is test-only:** In `review_budget.py`, the core budget allocator [`compute_round_allowance()`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/models/review_budget.py#L22-L51) already calculates:
  ```python
  remaining_cap = max(0, lifetime_cap - total_posted_so_far)
  if remaining_cap <= 0:
      return 0
  ```
  Callers in `review_enforcer.py:409-414` check `if review_budget == 0` or clamp comment counts to `review_budget`. The boolean helper `should_suppress_round` was never used.
- **Callers in `src/`:** Strictly **0**. Only referenced in `tests/unit/test_adk_samples_features.py:324`.
- **Recommendation:** **Delete.** Inline the trivial check in tests or remove it.

---

### 2.4 `has_genuine_summary_risk()` (`src/webhook_agent/review/schemas.py:327-332`)
- **What it does:** A 5-line stub function that unconditionally returns `False`:
  ```python
  def has_genuine_summary_risk(summary: str | None) -> bool:
      # Summary scraping was deprecated to prevent false-positive critical issue synthesis.
      # Reviews must explicitly place blocking issues in critical_issues or mark resolutions as UNRESOLVED.
      return False
  ```
- **Why it exists:** In PR #369, the system removed textual regex summary scraping from `calculate_sync_verdict()` because the model's summary text was triggering false-positive `REQUEST_CHANGES` verdicts on resolved items. Rather than deleting the function, PR #369 turned it into a hardcoded `return False` stub and added a test asserting that it returns `False`.
- **Callers in `src/`:** Strictly **0**.
- **Recommendation:** **Delete.** Remove the stub and its test file `tests/unit/review/test_sync_review_risk_cleanup.py`.

---

### 2.5 `is_security_dimension()` & `is_test_coverage_dimension()` (`review_enforcer.py:39, 45`)
- **Detailed in:** `docs/investigations/d4_scorecard_dimensions.md`.
- **Finding:** Unlike the other 4 functions above, these two are **not dead features**. The scorecard floor actively enforces both Dimension 3 and Dimension 5, but `matches_dimension()` performs substring matching inline rather than calling these helpers.
- **Recommendation:** **Do NOT delete.** Wire them into `matches_dimension()`.

---

## 3. Summary of Decisions & Impact

| Function | Action | Lines Saved | Target File |
| :--- | :---: | :---: | :--- |
| `_get_model_tpm_limit()` | **Delete** | ~44 lines | `src/webhook_agent/models/model_chain.py` |
| `_count_tokens_exact()` | **Delete** | ~30 lines | `src/webhook_agent/models/model_chain.py` |
| `should_suppress_round()` | **Delete** | ~10 lines | `src/webhook_agent/models/review_budget.py` |
| `has_genuine_summary_risk()` | **Delete** | ~35 lines | `src/webhook_agent/review/schemas.py` |
| `is_security_dimension()` | **Wire Up** | 0 (clean refactor) | `src/webhook_agent/review/review_enforcer.py` |
| `is_test_coverage_dimension()` | **Wire Up** | 0 (clean refactor) | `src/webhook_agent/review/review_enforcer.py` |
| **Total Net Impact** | | **~119 lines removed** | |

---

## 4. In WHAT Order They Should Be Resolved

1. **Phase:** Grouped into a single "Dead Function and Dead Parameter Cleanup" PR alongside `D3` (`memory_bank_heuristics`).
2. **Order relative to other tasks:**
   - Execute **after D1** (circuit breaker deletion).
   - Execute **before Phase 4** (`policy.py`).
3. **Safety Guarantee:** Every function marked for deletion has been verified with ripgrep across the entire workspace to confirm 0 callers in `src/`. All deletions are behavior-neutral.
