# Investigation Report: D4 — Scorecard Dimension Floor Integrity

- **Item:** `D4` / Decision `P6`
- **Component:** `src/webhook_agent/review/review_enforcer.py`
- **Associated Tests:** `tests/unit/review/test_scorecard_floor.py`
- **Status:** Complete Investigation & Resolution of Decision P6

---

## 1. The Concern Raised in the Reclamation Plan

In `docs/plans/repo_reclamation.md` §D4 and Part 5 (Decision **P6**), a serious concern was identified:
> *Investigation warning: two of three scorecard dimensions appear unwired → decision P6.*
> *Finding: three dimension predicates — `is_contract_dimension` (:34), `is_security_dimension` (:39), `is_test_coverage_dimension` (:45). Only `is_contract_dimension` is exported and used. The other two have zero references in `src/` and are exercised only by their own tests. All three have near-identical tests (0.98 structural similarity).*
> *This may be a real bug, not dead code: the scorecard floor may only be enforcing one of the three dimensions it was designed for. Investigate before deleting.*

If the deterministic scorecard floor (the "Golden Asymmetry" rule where deterministic pre-audits lock the LLM's review scorecard to `FAIL` or `WARN`) was only running on Dimension 4 (Contract Integrity) while silently ignoring Dimension 3 (Security & Secrets) and Dimension 5 (Test Coverage & Regressions), it would represent a critical security and quality defect.

---

## 2. In-Depth Code & Pipeline Investigation

A comprehensive audit of the full webhook lifecycle was performed across `prompts.py`, `execution.py`, and `review_enforcer.py`.

### 2.1 Pre-Audit Generation (`src/webhook_agent/core/prompts.py`)
All three deterministic evaluations are actively executed in the pre-audit pipeline during user prompt construction:
1. **Dimension 4 — Contract Integrity (lines 380–396):**
   ```python
   sym_analyzer = SymbolImpactAnalyzer(repo_root=active_env.root)
   impact_reports = sym_analyzer.analyze_modified_files(...)
   contract_item = evaluate_contract_integrity(impact_reports, parse_errors=sym_analyzer.parse_errors)
   event_data["deterministic_contract_scorecard_item"] = contract_item.model_dump()
   ```
2. **Dimension 5 — Test Coverage & Regressions (lines 417–434):**
   ```python
   test_analyzer = TestImpactAnalyzer(repo_root=active_env.root)
   test_report = test_analyzer.analyze(changed_files)
   diff_pct, _ = run_diff_coverage(...)
   test_item = evaluate_test_coverage_integrity(test_report, diff_coverage_pct=diff_pct)
   event_data["deterministic_test_scorecard_item"] = test_item.model_dump()
   ```
3. **Dimension 3 — Security & Secrets (lines 454–470):**
   ```python
   sec_scanner = SecurityScanner(repo_root=sec_root)
   sec_report = sec_scanner.scan(...)
   sec_item = evaluate_security_integrity(sec_report)
   event_data["deterministic_security_scorecard_item"] = sec_item.model_dump()
   ```

### 2.2 Session State Propagation (`src/webhook_agent/core/execution.py`)
In `src/webhook_agent/core/execution.py:391-398`, all three evaluated scorecard items are extracted from `event_data` and injected into the ADK deterministic state mapping:
- `deterministic_state["deterministic_contract_scorecard_item"]`
- `deterministic_state["deterministic_security_scorecard_item"]`
- `deterministic_state["deterministic_test_scorecard_item"]`

### 2.3 Verdict & Scorecard Floor Enforcement (`src/webhook_agent/review/review_enforcer.py`)
In `src/webhook_agent/review/review_enforcer.py:277-300`, `_enforce_verdict()` parses all three items from state:
```python
if det_contract_item is None:
    det_contract_item = ScorecardItem.model_validate(state_dict_val["deterministic_contract_scorecard_item"])
if det_security_item is None:
    det_security_item = ScorecardItem.model_validate(state_dict_val["deterministic_security_scorecard_item"])
if det_test_item is None:
    det_test_item = ScorecardItem.model_validate(state_dict_val["deterministic_test_scorecard_item"])
```
And during both initial (`cr_obj`) and synchronize (`sync_obj`) review processing (lines 500–511 and 611–620), **all three items are passed to `apply_scorecard_floor`**:
```python
if det_contract_item is not None:
    cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_contract_item)
if det_security_item is not None:
    cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_security_item)
if det_test_item is not None:
    cr_obj.scorecard = apply_scorecard_floor(cr_obj.scorecard, det_test_item)
```

### 2.4 Why Did It Look Unwired?
The confusion in the reclamation plan stemmed entirely from the implementation of [`matches_dimension()`](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/review/review_enforcer.py#L51-L64) vs the three standalone predicates:
```python
def is_contract_dimension(dim: str) -> bool:
    return "contract" in dim.strip().lower()

def is_security_dimension(dim: str) -> bool:
    d = dim.strip().lower()
    return "security" in d or "secret" in d

def is_test_coverage_dimension(dim: str) -> bool:
    d = dim.strip().lower()
    return "test" in d or "coverage" in d or "regression" in d

def matches_dimension(scorecard_dim: str, target_dim: str) -> bool:
    s_dim = scorecard_dim.strip().lower()
    t_dim = target_dim.strip().lower()
    if s_dim == t_dim:
        return True
    if "contract" in t_dim and "contract" in s_dim:
        return True
    if ("security" in t_dim or "secret" in t_dim) and ("security" in s_dim or "secret" in s_dim):
        return True
    return bool(
        ("test" in t_dim or "coverage" in t_dim) and ("test" in s_dim or "coverage" in s_dim)
    )
```
1. `apply_scorecard_floor()` calls `matches_dimension(item.dimension, deterministic_item.dimension)` on line 132.
2. `matches_dimension()` implemented the substring checks **inline** rather than calling `is_contract_dimension()`, `is_security_dimension()`, or `is_test_coverage_dimension()`.
3. `is_contract_dimension` happened to be exported in `review/__init__.py:69, 143`, creating the visual illusion that it was the only "active" dimension.
4. `is_security_dimension` and `is_test_coverage_dimension` were never re-exported in `review/__init__.py`, so a simple grep for function callers returned 0 in `src/`.

---

## 3. Findings & Resolution of Decision P6

### Finding:
> **The scorecard floor is NOT broken.** It is **100% active, wired, and enforced across all three dimensions** (Contract Integrity, Security & Secrets, and Test Coverage & Regressions).

### Decision P6 Answer:
> **P6 Answer: Not a bug.** The floor mechanism operates correctly end-to-end. The only issue is code aesthetics: `matches_dimension` duplicates the logic of the three standalone helper functions.

---

## 4. WHAT Our CURRENT Options Are

| Option | Description | Pros | Cons / Tradeoffs |
| :--- | :--- | :--- | :--- |
| **Option 1: Refactor `matches_dimension` to use helper predicates (Recommended)** | Rewrite `matches_dimension` so it delegates directly to `is_contract_dimension`, `is_security_dimension`, and `is_test_coverage_dimension`. Export all three (or none) consistently in `review/__init__.py`. | • Eliminates duplicated substring matching logic.<br>• Unifies predicates with tests.<br>• Leaves zero unwired helper functions. | • Minor 5-line diff in `review_enforcer.py`. |
| **Option 2: Delete the three standalone helper functions** | Delete `is_contract_dimension`, `is_security_dimension`, `is_test_coverage_dimension` and keep only `matches_dimension`. Move test cases to test `matches_dimension`. | • Reduces function count by 3.<br>• Removes 15 lines of redundant code. | • Requires updating `test_scorecard_floor.py` test cases. |
| **Option 3: Status Quo** | Leave helper functions intact as testable utilities alongside inline `matches_dimension`. | • Zero code changes. | • Future readers will repeatedly wonder why those functions exist with zero callers in `src/`. |

---

## 5. In WHAT Order It Should Be Resolved

1. **Step 1:** Record Decision **P6 = Not a bug; floor is fully wired across all 3 dimensions** in `docs/plans/repo_reclamation.md`.
2. **Step 2:** In the upcoming cleanup PR (alongside `D3` and `D5`), adopt **Option 1**: refactor `matches_dimension` to cleanly call the three helper functions:
   ```python
   def matches_dimension(scorecard_dim: str, target_dim: str) -> bool:
       s_dim = scorecard_dim.strip().lower()
       t_dim = target_dim.strip().lower()
       if s_dim == t_dim:
           return True
       if is_contract_dimension(t_dim) and is_contract_dimension(s_dim):
           return True
       if is_security_dimension(t_dim) and is_security_dimension(s_dim):
           return True
       return is_test_coverage_dimension(t_dim) and is_test_coverage_dimension(s_dim)
   ```
3. **Step 3:** Clean up the facade export in `review/__init__.py` during the `D7` pass.
