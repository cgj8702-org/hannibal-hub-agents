
# Code Review Report

## 1. Metadata

* **PR Number:** #[number]
* **Confidence Level:** [1-5] — [See Section 7 for self-assessment]

---

## 2. Executive Summary

*Provide a high-level overview of the proposed changes, their purpose, and the overall quality of the implementation. Be direct and specific — avoid vague praise.*

* **Goal of the PR:** [Brief description of what this PR aims to achieve]
* **Key Findings:** [Summary of the most important issues or risks discovered]
* **Verdict Justification:** [1-2 sentences explaining WHY you chose the verdict]

---

## 3. Scorecard

*An objective assessment across key engineering metrics. Fill in ALL categories honestly.*

**Scoring Rubric:**
*   **1: Critical** - Major bugs, security vulnerabilities, or complete lack of tests. Blocks merge.
*   **2: Poor** - Significant issues affecting maintainability or performance. Strong changes needed.
*   **3: Acceptable** - Functional and safe, but lacks polish or follows sub-optimal patterns.
*   **4: Good** - High quality, follows most best practices, minimal suggestions.
*   **5: Excellent** - Production-ready, elegant implementation, exemplary patterns.

| Category                | Score (1-5) | Specific Evidence                                                 |
| :---------------------- | :---------: | :---------------------------------------------------------------- |
| **Code Correctness**    |     [ ]     | [Cite specific lines/logic that support your score]               |
| **Security & Privacy**  |     [ ]     | [Cite specific patterns — secrets, auth, input validation]        |
| **Performance & Scale** |     [ ]     | [Cite specific concerns — loops, queries, memory, concurrency]    |
| **Readability & Style** |     [ ]     | [Cite specific examples — naming, structure, documentation]       |
| **Test Coverage**       |     [ ]     | [Cite what IS and IS NOT tested]                                  |

---

## 4. Verdict Determination

**MANDATORY RULES — Apply mechanically based on scorecard:**

1. ANY category scoring **1 (Critical)** → Verdict MUST be **REQUEST_CHANGES**
2. ANY category scoring **2 (Poor)** → Verdict MUST be **REQUEST_CHANGES**
3. Average score **below 3.5** → Verdict MUST be **REQUEST_CHANGES**
4. All categories scoring **3+** AND average **≥ 3.5** → Verdict MAY be **APPROVE**
5. If confidence level is **≤ 3** → Verdict MUST NOT be APPROVE (use COMMENT instead)

* **Average Score:** [calculated average]
* **Overall Verdict:** [APPROVE | REQUEST_CHANGES | COMMENT]

---

## 5. Key Issues & Action Items

*Concrete issues that must be addressed. Every issue needs a file path and actionable fix.*

### 🔴 Critical (Must Fix Before Merge)

*Issues that block deployment, cause bugs, or introduce security vulnerabilities.*

* **[Issue Title]**
  * **File/Line:** `path/to/file.ext` (Line XX)
  * **Description:** [Explain the issue clearly]
  * **Suggested Fix:**
    ```[language]
    // Insert suggested code block here
    ```

### 🟡 Minor / Refactoring (Suggested)

*Non-blocking suggestions to improve code quality, readability, or performance.*

* **[Issue Title]**
  * **File/Line:** `path/to/file.ext` (Line XX)
  * **Description:** [Explain the recommendation]
  * **Suggested Fix:**
    ```[language]
    // Insert suggested code block here
    ```

---

## 6. Detailed Code Commentary

*Specific, line-by-line feedback. Every review MUST include at least one actionable suggestion — even for excellent code.*

| File Path (with Line) | Severity | Feedback & Recommendations |
| :------------------- | :------- | :------------------------- |
| `path/to/file1.py:12-18` | Minor    | [Your comment here]        |
| `path/to/file2.py:45` | Critical | [Your comment here]        |

---

## 7. Confidence Self-Assessment

*Rate your confidence in this review honestly:*

* **5:** I thoroughly understand every change and am confident in my assessment
* **4:** I understand the design well but may have missed minor edge cases
* **3:** I understand the intent but lack context on some implementation details
* **2:** I'm uncertain about significant portions of the changes
* **1:** I don't have enough context to provide a meaningful review

**My Confidence:** [1-5]
**Context Gaps:** [List what you're uncertain about or missing context on]

If confidence ≤ 3, you MUST:
1. Explicitly state what context is missing
2. NOT approve the PR (use COMMENT verdict instead)
3. Ask the PR author specific questions about uncertain areas
