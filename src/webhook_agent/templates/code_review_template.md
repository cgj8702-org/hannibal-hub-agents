# 🛡️ Code Review Report

### 1. Executive Summary

* **PR Scope:** `[dev_docs | minor_fix | core_backend]`
* **Verdict Justification:** [1-2 direct sentences explaining the architectural intent and verdict rationale]

---

### 2. Verdict Determination

* **Overall Verdict:** `[APPROVE | REQUEST_CHANGES | COMMENT]`
* **Confidence Rating:** `[0.0-5.0]/5.0`

---

### 3. Critical Blocking Issues (Must Fix Before Merge)

> [!CAUTION]
> *Issues that block deployment, introduce bugs, cause race conditions, or break API contracts.*

* **[File:Line Citation]**: `[file_path]:[line_range]`
* **Failure Mechanism**: [Clinical explanation of the flaw and edge case]
* **Remediation**:
```python
# Concrete, actionable code fix snippet
```

---

### 4. Diff-Anchored Risk & Edge-Case Analysis

> [!IMPORTANT]
> *High-signal analysis of potential failure modes, rate limits, concurrency boundaries, memory usage, or security considerations.*

* **[Category]**: `[concurrency | memory | security | breaking_change]`
* **Impact**: [Specific operational risk and edge case]
* **Recommended Safeguard**: [Recommended code mitigation or safeguard]

---

### 5. Actionable Maintainability & Code Quality Notes

* **[File:Line Citation]**: `[file_path]:[line_range]`
* **Suggestion**: [Actionable suggestion to improve readability, performance, or test coverage]
