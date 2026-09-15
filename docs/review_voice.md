# Review Voice — Hannibal Hub

How an automated PR review comment must read.

---

## Two Registers Only

### Register A (60%) — A polite full sentence ending in `?`
Proper capitalization, often leading with a polite query or soft hedge:
- `Can we use subprocess.run() with a list here instead of shell=True?`
- `Perhaps this should be user_id instead of userId to match snake_case conventions?`
- `Is there a reason we're not reusing get_client() here?`
- `It seems the lock is released before the write finishes. Is that intentional?`
- `Can you please add error handling around this remote call?`

### Register B (40%) — A lowercase fragment, 2-6 words
No leading capital, no trailing period. Used for obvious, observable defects or cross-references:
- `this'll break if the list is empty`
- `missing await here`
- `same issue as above`
- `unhandled None return from get()`

---

## Strictly Banned in Review Comments

1. **No Emojis:** Zero emojis permitted in syntax, variable suggestions, or inline review comments.
2. **No Severity Labels in Comments:** Never write `[CRITICAL]`, `[MAJOR]`, `**Warning:**`, or `🔴` inside comment bodies. Severity is managed mechanically by the review verdict system.
3. **No Formatting Noise:** No Markdown headers (`#`, `##`) or bullet lists inside inline line comments.
4. **No Abstract Fluff:** Never write `"Consider refactoring to improve readability and maintainability"`. Cite concrete observable facts visible at the line.
5. **No Greeters or Sign-offs:** Avoid `"Thanks for the PR!"`, `"LGTM but..."`, or pleasantries.
6. **No Speculative Absences:** Do not claim an import or function is missing based on a 10-line diff hunk.
