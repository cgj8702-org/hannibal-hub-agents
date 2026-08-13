# Deprecation: `HANNIBAL_TIER`

This document records the deprecation of the `HANNIBAL_TIER` environment variable across the Hannibal projects.

Summary
-------
- `HANNIBAL_TIER` is deprecated and must not be used going forward.
- Replacement conventions:
  - For `hannibal-hub-agents` (agent workers, webhook agents): use `WEBHOOK_TIER` and the `WEBHOOK_*` key naming (e.g. `WEBHOOK_FREE_KEY`, `WEBHOOK_PAID_KEY`).
  - For `hannibal-hub` (chatbot/main hub): use `CHATBOT_TIER` and the `CHATBOT_*` key naming (e.g. `CHATBOT_FREE_KEY`, `CHATBOT_PAID_KEY`).

What changed
------------
- All runtime code should read only `WEBHOOK_TIER` (agents) or `CHATBOT_TIER` (main) as appropriate. No backwards-compatible aliasing is provided.
- Configuration, docs, and deployment manifests should be updated to set the correct env var names.

Action items for maintainers
----------------------------
1. Update CI / deployment manifests to export the new env var names.
2. Replace any `HANNIBAL_TIER` references in scripts, docs, and templates with the appropriate new variable.
3. Run `bash scripts/ruff-all.sh` and the test suites in both repos after changes.

Notes
-----
- This is a documentation-only follow-up PR to make the deprecation explicit and to provide a single reference for reviewers.
- If you want me to also search and replace any remaining `HANNIBAL_TIER` occurrences and apply the code edits automatically across the agents repo, I can do that in this PR — just say so.
