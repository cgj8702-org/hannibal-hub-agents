# Review Rules — Hannibal Hub

Single source of truth for automated PR code reviews in Hannibal Hub.

---

## 1. What to Hunt (Observable Defects Only)

A finding MUST point to something directly visible in the diff at the line anchored.

1. **Logic & Boundaries:**
   - Guaranteed division by zero (`x / 0` or unverified denominator).
   - Definite `NoneType` attribute access or indexing when type annotations or upstream returns explicitly permit `None`.
   - Off-by-one indices or slice ranges.
2. **Concurrency & Async Safety:**
   - Missing `await` on coroutines.
   - Shared mutable state mutated without synchronization across async tasks.
   - Unbounded memory accumulation or unclosed client sessions.
3. **Security & Secrets:**
   - Hardcoded API keys, JWT secrets, or GCP service account credentials.
   - Unsanitized inputs interpolated into raw shell commands or queries.
4. **Contract Integrity:**
   - Breaking signature changes that leave call sites in the PR broken.

---

## 2. Never Report These (Already Enforced or Out of Scope)

Do not report defects already caught by deterministic CI or out of scope:
1. **Formatting & Imports:** Enforced deterministically by Ruff (`./scripts/ruff-all.sh`). Do not flag whitespace, indentation, quote styles, or import sorting.
2. **Static Typing Details:** Enforced deterministically by MyPy.
3. **Absent Names in Partial Diff:** In git diff hunks, definitions and imports exist outside the visible lines. Never claim an import or variable is missing unless reviewing an entire new file.
4. **Speculative Cross-File Concurrency:** Do not speculate on hypothetical interactions that cannot be proven from the visible diff.
