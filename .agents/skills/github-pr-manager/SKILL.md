---
name: github-pr-manager
description: "Use this skill for the end-to-end management of the GitHub Pull Request lifecycle for the Hannibal Hub. This includes branch management, surgical staging, high-quality PR submission, deep code review, implementing review suggestions, and final merging/cleanup. Triggers on: 'create pull request', 'open pr', 'submit pr', 'manage prs', 'stage changes', 'address pr comments', 'fix pr comments', 'resolve pr feedback', 'review PR', 'implement PR suggestions', 'merge PR', 'git pr management'."
---

# GitHub PR Manager

Clinical protocol for Pull Request lifecycle and Git operations.

---

## Identity & Auth

Before running ANY `gh` commands, switch to the correct identity:

```bash
gh auth switch --user cgj8702-agents
```

All interactive commits MUST use author identity:
```bash
git commit --author="cgj8702-agents <cgj8702-agents@users.noreply.github.com>"
```

> `hannibal-hub-agents[bot]` is reserved exclusively for autonomous VM workers.

---

## Phases

### 1. Branch & Stage

```bash
git fetch origin
git checkout main && git pull origin main
git checkout -b <type>/<topic>          # e.g. fix/auth-bug, chore/cleanup
```

**Rules:**
- **NEVER commit or push on `main`.** Always create a feature branch.
- Before adding commits to an existing branch, verify PR state first:
  ```bash
  gh pr view <branch_or_number> --json state -q '.state'
  ```
  If `MERGED` or `CLOSED`: abandon the branch, checkout `main`, pull, and create a fresh branch.
- Stage surgically. One logical change per PR.

### 2. Validate

Run the test suite before committing:

```bash
uv run pytest                          # Full test suite
```

All tests must pass with zero errors before proceeding.

### 3. Commit & Push

Use [Conventional Commits](https://www.conventionalcommits.org/) format:

```bash
git add <files>
git commit --author="cgj8702-agents <cgj8702-agents@users.noreply.github.com>" \
  -m "type(scope): concise description"
git push -u origin <branch>
```

### 4. Submit PR

Create the PR using the repo's official template structure (`.github/PULL_REQUEST_TEMPLATE.md`):

```bash
gh pr create \
  --title "type(scope): description" \
  --body-file /tmp/pr-body.md
```

The PR body should follow the template sections:
1. **Description** — What, Why, How
2. **Testing** — Commands run + results
3. **Configuration Impact** — Dependency/env changes
4. **Security Checklist** — Secrets, auth, HMAC verification
5. **Related** — Linked issues

### 5. Auditor Review Loop

After PR submission, `hannibal-hub-agents[bot]` (the webhook auditor) automatically reviews the PR.

**Step A — Wait for and read the auditor's review:**
```bash
gh pr view <number> --reviews
```

**Step B — Address Critical items:**
If the auditor flags any 🔴 **Critical (Must Fix Before Merge)** items:
1. Apply surgical fixes on the PR branch.
2. Re-run validation (Phase 2).
3. Commit and push:
   ```bash
   git commit --author="cgj8702-agents <cgj8702-agents@users.noreply.github.com>" \
     -m "fix(scope): address auditor feedback"
   git push
   ```

**Step C — Discuss edge cases with the user:**
If the auditor flags 🟡 **Suggestions** or **Potential Risks & Edge Cases**, present them to the user and implement any the user wants addressed.

**Step D — Wait for re-review:**
After pushing fixes, the auditor automatically re-reviews. Wait for it and read the new review:
```bash
gh pr view <number> --reviews
```
Repeat Steps B–D until the auditor returns **APPROVE**.

### 6. Merge (Requires User Approval)

Once the auditor approves, confirm with the user before merging:

```bash
gh pr merge <number> --squash --delete-branch
git checkout main && git pull origin main
```

Verify merge succeeded:
```bash
gh pr view <number> --json state -q '.state'   # Should return "MERGED"
```

---

## Quick Reference

| Action | Command |
|---|---|
| Switch identity | `gh auth switch --user cgj8702-agents` |
| Check PR state | `gh pr view <n> --json state -q '.state'` |
| Read auditor review | `gh pr view <n> --reviews` |
| Test suite | `uv run pytest` |
| Create PR | `gh pr create --title "..." --body-file <file>` |
| Squash merge | `gh pr merge <n> --squash --delete-branch` |
| Sync main | `git checkout main && git pull origin main` |
