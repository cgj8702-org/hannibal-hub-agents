# Code Review Report

### 1. Executive Summary

* **Goal of the PR:** [Brief description of what this PR aims to achieve]
* **Verdict Justification:** [1-2 direct sentences explaining WHY you chose the verdict]

---

### 2. Scorecard Summary

> [!NOTE]
> **Scorecard Breakdown (1-5 Scale)**
> * **Code Correctness:** [Score]/5 — [Specific Evidence from diff]
> * **Security & Privacy:** [Score]/5 — [Specific Evidence from diff]
> * **Performance & Scale:** [Score]/5 — [Specific Evidence from diff]
> * **Readability & Style:** [Score]/5 — [Specific Evidence from diff]
> * **Test Coverage:** [Score]/5 — [Specific Evidence from diff]
> * **Average Score:** [Calculated Average]/5 | **Confidence:** [1-5]/5

---

### 3. Verdict Determination

* **Overall Verdict:** [APPROVE | REQUEST_CHANGES | COMMENT]

---

### 4. Mandatory Risk & Edge-Case Analysis

> [!IMPORTANT]
> *Finding zero risks or edge cases is unacceptable. Every review MUST highlight at least ONE potential failure mode, concurrency boundary, memory limit, or unhandled edge case — even for approved PRs.*

* **Potential Edge Case / Risk:** [Identify a specific edge case or risk factor, e.g., rate limits, unhandled exceptions, non-UTF8 input, missing timeouts, or concurrent execution boundaries]
* **Recommended Safeguard:** [Propose how to mitigate or monitor this risk]

---

### 5. Key Issues & Action Items

#### 🔴 Critical (Must Fix Before Merge)
*Issues that block deployment, introduce bugs, or cause security vulnerabilities.*
* *[None if no critical issues found, or list issue title with File/Line and suggested fix]*

#### 🟡 Minor / Refactoring (Actionable Suggestions)
*Non-blocking suggestions to improve code quality, maintainability, or performance.*
* *[Provide at least one actionable, objective suggestion for improvement]*

---

### 6. Confidence Self-Assessment

* **My Confidence:** [1-5]
* **Context Gaps:** [List any missing context or state None if fully understood]
