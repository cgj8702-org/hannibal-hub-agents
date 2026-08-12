Please follow these protocols to ensure team coordination remains professional, productive, and efficient:

* **Zero-Bypass Architecture:** All agents, including Antigravity, are strictly prohibited from committing directly to `main`. Every code change MUST be submitted via Pull Request and pass automated CI/CD gating and peer review.
* **PR Lifecycle Verification:** Prior to pushing commits or targeting PR branches, verify the live PR state via `gh pr view`. Never push follow-up commits to merged or closed PR branches. Always checkout a fresh branch off updated `main` for new changes.
* **Sequential Thinking:** Prior to executing complex or multi-step operations, utilize the `sequential-thinking` tool to structure execution paths and dependencies.
* **Operating Philosophy:** High-efficiency engineering characterized by a supportive, warm, and informal communication style. "Efficiency is elegant. Predictability is beautiful."
* **Aesthetic Boundaries:** Use strictly UTF-8 encoding. Emojis are permitted in string literals (e.g., logs and print statements), UI, and Markdown docs, but remain strictly prohibited in syntax, variable names, or inline comments.
* **Non-Destructive Operations:** Prohibited from using `rm`, `rmdir`, or `dd` commands on source code, documentation, or assets.
* **GitHub App Identity:** GitHub App installation tokens represent integrations and cannot access user-specific endpoints like `GET /user`. Always use `BOT_LOGIN` (`"hannibal-hub-agents[bot]"`) or bot login matching for bot checks.
* **ADK Tool & Search Grounding:** Google Search grounding tools (`google_search`) in ADK are supported strictly on Gemini models (`gemini-3.5-flash-lite`). Do not assign `google_search` tools to Gemma models (`gemma-4-31b-it`).
* **Dependency Management:** All environment management must use **`uv`**. Execute `uv sync` immediately following any modification to `pyproject.toml`.
* **Linting Compliance:** Execute the **`scripts/ruff-all.sh`** bash script for linting and formatting validation prior to task completion.
* **Precision Editing:** Default to targeted surgical edits over complete file rewrites.
* **Rollback Strategy:** Every automated deployment, data mutation, or complex file manipulation MUST include a deterministic rollback protocol to recover from partial failures.

When executing terminal commands and scripts, follow these protocols to ensure reliable asynchronous coordination:

* **Execution Verification:** Append `&& echo "CMD_COMPLETE"` to asynchronous bash executions.
* **Session Persistence:** Utilize `send_command_input` for long-running or interactive background processes.
* **Autonomy:** Operations must default to non-interactive CLI flags (`-y`, `--quiet`, `--silent`).
* **Verify, Don't Assume:** Do NOT rely on CLI output text (e.g. "Build successful") as proof of system health. You MUST verify the actual state.
