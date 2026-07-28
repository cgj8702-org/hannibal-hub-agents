---
name: github-bot-identity
description: "Handles authentication and identity switching for GitHub CLI (gh) operations. Triggers on: 'authenticate as bot', 'switch github identity', 'gh auth switch', 'github credentials'. Governs how agents assume the correct bot identity before managing Pull Requests."
---

<div align="center">

# 🛠️ `GitHub Bot Identity`
*Clinical Operational Protocol for Agent Authentication* ⚡️

</div>

---

## 🚨 CRITICAL: Bot Identity Submission

Because agent PRs MUST be authored by `Hannibal-Hub-Agents[bot]` rather than `cgj8702`, agents MUST authenticate correctly before interacting with GitHub.

**IMPORTANT**: Agent terminals are **STATELESS**. Environment variables (like `BOT_TOKEN`) do NOT persist between tool calls. If you are required to generate an installation token, you must execute the generation and the `gh` command in the **same CLI execution** (e.g., using `&&` or a subshell).

**Token Generation Command Pattern**:
```bash
BOT_TOKEN=$(echo -e "protocol=https\nhost=github.com\n" | python3 /home/carly/git-credential-github-app.py get | grep "^password=" | cut -d= -f2)
```

## ☁️ Operational Mandate

Whenever executing operations that interact with GitHub APIs (like opening a Pull Request), you MUST ensure your `gh` CLI session is operating under the `cgj8702-agents` context. 

Prior to executing any GitHub CLI operations, use the following switch command:
```bash
gh auth switch --user cgj8702-agents
```

If the authentication fails or you are instructed to use a raw token due to the stateless environment, fallback to the combined command pattern.

<div align="center">

*Kept perfectly up to date with 💖 and lots of ☕* <br>
**Last Updated:** `2026-07-02` at `1:14` `PM` `EDT`

</div>
