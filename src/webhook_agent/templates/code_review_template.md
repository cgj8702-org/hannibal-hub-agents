
# Code Review Report

## 1. Metadata

* **Overall Verdict:** [Approved | Request Changes | Comment Only]

---

## 2. Executive Summary

*Provide a high-level overview of the proposed changes, their purpose, and the overall quality of the implementation.*

* **Goal of the PR:** [Brief description of what this PR aims to achieve]
* **Key Findings:** [Summary of major strengths or critical issues found during the review]

---

## 3. Scorecard

*An objective assessment across key engineering metrics based on the following rubric:*
*   **1: Critical** - Major bugs, security vulnerabilities, or complete lack of tests. Must be fixed.
*   **2: Poor** - Significant issues affecting maintainability or performance. Strong changes needed.
*   **3: Acceptable** - Functional and safe, but lacks polish or follows sub-optimal patterns.
*   **4: Good** - High quality, follows most best practices, minimal suggestions.
*   **5: Excellent** - Production-ready, elegant implementation, exemplary patterns.

| Category                      | Score (1-5) | Summary Comment                                                   |
| :---------------------------- | :---------: | :---------------------------------------------------------------- |
| **Code Correctness**    |     [ ]     | [Does the code work as intended? Are there logical errors?]       |
| **Security & Privacy**  |     [ ]     | [Are there vulnerabilities, hardcoded secrets, or data leaks?]    |
| **Performance & Scale** |     [ ]     | [Are there potential bottlenecks, memory leaks, or slow queries?] |
| **Readability & Style** |     [ ]     | [Is the code easy to follow? Does it adhere to style guides?]     |
| **Test Coverage**       |     [ ]     | [Are there adequate unit, integration, or regression tests?]      |

---

## 4. Key Issues & Action Items

*Use this section to highlight critical issues that must be addressed before merging.*

### 🔴 Critical (Must Fix)

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

## 5. Detailed Code Commentary

*Specific, line-by-line feedback on the codebase. Use the `File:Line` format for precise referencing.*

| File Path (with Line) | Severity | Feedback & Recommendations |
| :------------------- | :------- | :------------------------- |
| `path/to/file1.js:12-18` | Minor    | [Your comment here]        |
| `path/to/file2.py:45` | Critical | [Your comment here]        |

---

## 6. Positive Feedback & Best Practices

*Highlight parts of the code that are well-written, elegant, or demonstrate good design patterns.*

* **[Strength Name]**: [Description of what was done well, e.g., "Excellent use of the factory pattern in `payment_processor.py` to decouple code."]
