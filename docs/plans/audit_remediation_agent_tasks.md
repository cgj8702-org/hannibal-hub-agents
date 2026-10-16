# Audit remediation: agent task specs

Specs for autonomous coding agents working in `cgj8702-org/hannibal-hub-agents`. Suggested repo location: `docs/plans/audit_remediation_agent_tasks.md`.

Findings were produced by a code audit; items marked *reproduced* were demonstrated with scripts, the rest come from code reading. **Do not trust a finding blindly: write a failing test that reproduces it first (red), then fix it (green).** If a test cannot reproduce the problem, stop and report (see Stop conditions).

## 0. How to dispatch

- One task = one PR = one agent. Prompt: `Implement T-XX from docs/plans/audit_remediation_agent_tasks.md. Follow section 1 (Global rules) and section 2 (Decisions) exactly.`
- Run tasks in the order of section 3. Tasks with no shared files may run in parallel.
- Jules is summoned only when the human owner (`cgj8702`) attaches the `jules` label to the issue or PR. Never @mention `jules`.

## 1. Global rules (from AGENTS.md; apply to every task)

1. Use `uv` only: `uv run pytest`, `uv run ruff`, `uv run mypy`. Never bare `python`, `pip` or `pytest`. Run `uv sync` after any `pyproject.toml` change and commit the updated `uv.lock`.
2. Branch fresh off updated `main`. Never commit to `main`. Never push to a merged or closed PR branch (verify with `gh pr view`). Never merge your own PR.
3. Never use `--no-verify`.
4. Commit format: `type(scope): summary (#PR)`. Interactive agent commits are authored as `cgj8702-agents <cgj8702-agents@users.noreply.github.com>`.
5. Before opening the PR run, in order, all must exit 0: `./scripts/ruff-all.sh`; the task's targeted tests; `uv run pytest --tb=short -q`.
6. Tests: offline only (mock all network and Gemini calls), mirror the source tree under `tests/unit/`, new files start with `pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]`. Use obviously fake secrets (for example `ghs_` followed by `A` repeated).
7. Surgical edits only. No drive-by refactors. Touch only the files listed in the task plus their tests. Use `git rm` for deletions, never `rm`.
8. Do not modify `.githooks/*`, `.github/workflows/*` (except T-11), model name strings, `assets/registries/gemini_models.json`, or `docs/MODEL_CHAIN.md`.
9. Never commit or print secrets. Never make live API calls.
10. Loggers are named `webhook_agent.<package>.<module>`. Emojis only inside string literals. Tool functions keep the signature rules in AGENTS.md section 6.
11. If you move or rename a module, update every `mock.patch("webhook_agent....")` string.
12. PR body must contain: what changed, the red-to-green evidence (test names), commands run with results, and any deviations from the spec.

**Stop conditions.** If an acceptance criterion cannot be met, a required decision is not covered in section 2, or the change would need files outside the allowed scope: stop, open the PR as a **draft**, and explain the blocker in the PR body. Do not guess and do not widen scope.

## 2. Decisions already made (override only by editing this file)

- **D1.** Trigger authorization allows `author_association` in `OWNER,MEMBER,COLLABORATOR` (env `TRIGGER_ALLOWED_ASSOCIATIONS`). Exempt logins (env `TRIGGER_EXEMPT_LOGINS`): `cgj8702,cgj8702-agents,dependabot[bot],google-labs-jules[bot]`.
- **D2.** Runtime defaults are NOT changed. `ALLOW_AUTOMATED_MUTATIONS` stays `1` and `STRICT_REVIEW_GROUNDING` stays `0`; documentation is corrected to match. Flipping them needs a human.
- **D3.** `calculate_verdict` semantics are unchanged. Safety is added at the submission point (T-04).
- **D4.** Deploy auth is preserved (token stays, but is scrubbed on failure). Removing the token needs a human.
- **D5.** The dead Secret Manager fallback is deleted rather than activated (activating it would change production behavior).
- **D6.** `.ocr` block in AGENTS.md is a tool-managed block: annotate it, do not delete it.
- **D7.** Ruff `BLE` is NOT enabled (too noisy). Only `S` is.

## 3. Order and dependencies

| Wave | Tasks (parallel-safe unless noted) |
|---|---|
| 1 | T-01, T-02, T-03, T-06, T-08, T-09, T-10 |
| 2 | T-04 (after T-03: both touch review flow docs), T-07 (after T-06), T-12 |
| 3 | T-13 (after T-04), T-05 (after T-03, T-04), T-11 (human review required) |
| 4 | T-14 (after T-13), T-15 (after T-12, T-14), T-16 (last) |

---

## T-01: Scope guardrail state to a single invocation [COMPLETED - PR #318]

- **branch:** `fix/guardrails-per-invocation` | **commit:** `fix(guardrails): scope budgets to one invocation`
- **files:** `src/webhook_agent/core/guardrails.py`, `tests/unit/core/test_guardrails.py`
- **problem (reproduced):** `IterationBudgetPlugin` and `GuardrailsPlugin` keep counters (`guardrail_turn_count`, `guardrail_tool_calls_count`, `guardrail_tool_call_history`, `guardrail_tool_failures`) and `halt_reason` in ADK session state. Sessions are keyed per sender and `repo/PR`, so counters accumulate across webhook events. With production values (`max_turns=6`, `max_tool_calls=15`) a long-lived PR eventually trips the budget permanently, and every later run has its tools stripped.
- **do:**
  1. Replace the separate keys with one dict `state["guardrail_run"]` holding `invocation_id`, `turns`, `tool_calls`, `halt_reason`, `history`, `failures`.
  2. Add helper `_run_state(state, invocation_id)` that returns the stored dict only if `invocation_id` matches, else a fresh dict. Get the id from `callback_context.invocation_id` / `tool_context.invocation_id`.
  3. After every mutation, reassign `state["guardrail_run"] = run` (do not rely on in-place mutation).
  4. Both plugins share the same dict, so `halt_reason` set by one is seen by the other within the same invocation only.
  5. Keep all existing log messages and the returned error dicts unchanged.
- **tests:**
  - Update existing tests to the new structure without weakening assertions.
  - Add a multi-invocation test using a stub model (pattern: subclass `google.adk.models.base_llm.BaseLlm`, `generate_content_async` yields one `LlmResponse` with text "ok"; build `LlmAgent` + `App(plugins=[IterationBudgetPlugin(max_turns=3)])` + `Runner` + `InMemorySessionService`; run 5 invocations on one session). Assert `halt_reason` stays unset after every invocation.
  - Add a test that a single invocation exceeding `max_turns` still halts.
  - Add a test that no-progress detection does not trigger on identical calls spanning two invocations.
- **done when:** the above tests pass and `uv run pytest tests/unit/core` is green.
- **do not:** change budget values in `agent_definition.py`.

## T-02: Make base-branch merge detection unspoofable [COMPLETED - PR #322]

- **branch:** `fix/merge-sync-spoof` | **commit:** `fix(review): verify base-branch merge syncs via API`
- **files:** `src/webhook_agent/review/fast_path.py`, `tests/unit/review/test_fast_path.py`
- **problem (reproduced):** `is_base_branch_merge_sync` returns `True` from the webhook payload when the commit message looks like "Merge branch 'main'...", without checking parents. Anyone who can push can skip re-review.
- **do:**
  1. Delete both message-only shortcuts (the payload `commits` loop and the `is_merge_msg or ...` condition). Commit messages may be used only as a hint, never as proof.
  2. Return `True` only if all hold, fetched via the GitHub API: (a) head commit has >= 2 parents; (b) one parent equals `before_sha`, the other (`base_parent`) satisfies `repo.compare(base_ref, base_parent).status in ("behind", "identical")`; (c) for every file in `repo.compare(before_sha, head_sha).files` (cap 100 files; more means return `False`), the file's blob `sha` at head equals the blob sha of `repo.get_contents(path, ref=base_parent)`.
  3. Any exception, missing field or cap overflow returns `False` (fail closed: review runs).
- **tests:** spoofed single-parent commit with a "Merge branch" message returns `False` and never skips; legitimate merge of base returns `True`; evil merge (extra file content not in base) returns `False`; >100 files returns `False`; API error returns `False`. Use `MagicMock` GitHub objects.
- **done when:** tests pass and the old spoof scenario (single parent, merge-like message) returns `False`.

## T-03: Restrict who can trigger the agent [COMPLETED - PR #323]

- **branch:** `feat/trigger-authorization` | **commit:** `feat(processor): gate triggers on author association`
- **files:** `src/webhook_agent/processor.py`, `src/webhook_agent/constants.py`, `tests/unit/test_worker.py` (or a new `tests/unit/test_trigger_authorization.py`)
- **problem:** `should_process_event` has no authorization check. On a public repo any commenter can start agent runs.
- **do:**
  1. In `constants.py` add `DEFAULT_TRIGGER_ALLOWED_ASSOCIATIONS = "OWNER,MEMBER,COLLABORATOR"` and `DEFAULT_TRIGGER_EXEMPT_LOGINS` per D1.
  2. In `should_process_event`, after the bot filter: read `raw.comment.author_association` for comment events, `raw.pull_request.author_association` for PR events. If a value is present and not in the allowed set, and neither the sender login nor the author login is exempt (case-insensitive), return `False` and log at INFO with delivery id and association (never the comment body).
  3. If no association field exists (synthetic events from the CI gate or checkpoint resume, `check_suite.completed`), allow, as today.
  4. Read env overrides at call time, not import time.
- **tests:** each association value, exempt logins, synthetic events, env override, Jules and Dependabot paths still reach their existing handlers.
- **done when:** a `NONE`-association commenter saying "please review" is ignored; an `OWNER` is not.
- **do not:** touch the Jules adoption or dependency fast-path logic.

## T-04: Never APPROVE without verified patches [COMPLETED - PR #328]

- **branch:** `fix/approve-requires-grounding` | **commit:** `fix(review): downgrade APPROVE when grounding is skipped`
- **files:** `src/webhook_agent/tools/github_tools.py`, `src/webhook_agent/review/review_enforcer.py`, `src/webhook_agent/constants.py`, `tests/unit/review/test_review_tool_validation.py`
- **problem:** in `review()`, the downgrade of APPROVE to COMMENT when no patch text is available happens only in strict grounding mode. API failures or oversized diffs therefore allow ungrounded APPROVEs.
- **do:**
  1. In the `not has_usable_patches` branch, always downgrade `APPROVE` to `COMMENT`, mirroring the existing pattern (`effective_event`, `event`, `review_obj.verdict`, `parsed_data["verdict"]`, `body = json.dumps(parsed_data)`).
  2. `grep -rn "APPROVE" src` and apply the same rule at any other model-driven approval point. The deterministic Dependabot or lockfile fast path in `fast_path.py` is exempt.
  3. Fix the misleading comment in `constants.py` ("Fail closed") to say grounding is measured-only by default (D2). Do not change the default.
- **tests:** APPROVE with empty patches becomes COMMENT in non-strict mode; APPROVE with usable patches is unchanged; retrieval failure case; fast-path approval unchanged.
- **done when:** no code path submits an LLM-driven APPROVE while patches are unavailable.
- **do not:** change `calculate_verdict` (D3) or the strict-mode default (D2).

## T-05: Make SECURITY.md match the code [COMPLETED - PR #333]

- **branch:** `docs/security-md-accuracy` | **commit:** `docs(security): align claims with actual behavior`
- **files:** `SECURITY.md`
- **do:** (1) State that `ALLOW_AUTOMATED_MUTATIONS` defaults to `1` (enabled) in `constants.py` and `scripts/load_secrets.sh`. (2) Update the tool-allowlist item: both agents expose `read_file` and `search_files`; the auditor also has `review`, the conversational agent `get_issue`. (3) Describe the trigger authorization from T-03 and the APPROVE rule from T-04, with env var names. (4) Replace the `webhook-router.yaml` sentence with: signature verification happens in a router that is not in this repository and cannot be audited here. (5) Remove or correct any claim now false after T-13 and T-14.
- **done when:** every factual statement in SECURITY.md can be traced to code. List the file and symbol you checked for each in the PR body.

## T-06: Harden `search_files` and the local executor [COMPLETED - PR #324]

- **branch:** `fix/search-files-hardening` | **commit:** `fix(tools): block symlink escape and scrub subprocess env`
- **files:** `src/webhook_agent/tools/search_tools.py`, `src/webhook_agent/environment/local.py`, tests under `tests/unit/tools/` and `tests/unit/environment/`
- **problem (reproduced):** the generated search script opens files that are symlinks pointing outside the root. `LocalEnvironment.execute` passes the full `os.environ` (API keys, tokens) to the shell subprocess and on timeout kills only the shell.
- **do:**
  1. In `_build_search_script`, skip any file where `os.path.islink(_full)` is true, and require `os.path.realpath(_full)` to start with `realpath(_root) + os.sep`.
  2. In `LocalEnvironment.execute`, build the subprocess env from an allowlist (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `PYTHONIOENCODING`) plus `env_vars`. Do not copy the rest of `os.environ`.
  3. Start the subprocess with `start_new_session=True` and on timeout `os.killpg(proc.pid, signal.SIGKILL)`.
  4. In `search_files`, reject `pattern` longer than 500 characters and clamp `limit` to 1..200, returning the existing error-dict shape.
- **tests:** symlink to an outside file yields no matches (workspace in a temp dir); `WEBHOOK_PAID_KEY` set in the parent env is not visible to the subprocess; after a timeout the child PID no longer exists (`os.kill(pid, 0)` raises `ProcessLookupError`); over-long pattern error.
- **done when:** all new tests pass and existing `search_tools` tests still pass.

## T-07: Run analysis against the PR head, not the worker's checkout [COMPLETED - PR #329]

- **depends_on:** T-06. **branch:** `feat/pr-workspace` | **commit:** `feat(environment): analyze PR head via ephemeral workspace`
- **files:** new `src/webhook_agent/environment/pr_workspace.py`, `src/webhook_agent/core/execution.py`, `src/webhook_agent/core/prompts.py`, `src/webhook_agent/analysis/symbol_graph.py`, `src/webhook_agent/analysis/test_impact.py`, `src/webhook_agent/processor.py` (wiring only), matching tests
- **problem:** nothing sets the active environment, so `search_files` reads the deployed `main` checkout (`os.getcwd()`). `SymbolImpactAnalyzer` and `TestImpactAnalyzer` also use `Path.cwd()` and diff against `git show HEAD~1` of that checkout, so their reports describe unrelated code.
- **do:**
  1. New async context manager `pr_workspace(gh, repo_full_name, head_sha)` that downloads the tarball for `head_sha` (PyGithub `repo.get_archive_link("tarball", ref=head_sha)`, download with `httpx`, stream with a 100 MB cap), extracts it into `ephemeral_environment()` using `tarfile` with `filter="data"`, strips the top-level directory, rejects symlink and hardlink members, caps at 20000 files and 200 MB extracted, and cleans up on exit.
  2. Wire it around agent execution and prompt building with `use_environment`. Inspect the call order first and choose the smallest change that makes the environment active when `build_user_message` and the runner execute.
  3. `SymbolImpactAnalyzer` / `TestImpactAnalyzer`: callers must pass `repo_root` from the active workspace. If no workspace is active, omit those prompt sections rather than falling back to `Path.cwd()`.
  4. Replace the `git show HEAD~1` baseline with an optional `baseline_loader: Callable[[str], str | None]` parameter. Production supplies a loader using `repo.get_contents(path, ref=base_sha)`; default `None` means no baseline.
  5. Add env kill switch `ENABLE_PR_WORKSPACE` (default `1`). When `0`, behave as in item 3 (no local-tree analysis).
  6. On any workspace failure, log a warning and continue without local analysis (never block the review).
- **tests:** fake tarball in memory containing a traversal member, an absolute path, and a symlink, all rejected; size caps; cleanup on exception; prompts omit impact sections when no workspace; `search_files` sees workspace files, not `cwd`.
- **split allowed:** if the diff exceeds roughly 600 lines, deliver items 1 and 6's tests as PR A and the wiring as PR B (state this in PR A).
- **done when:** a test shows `search_files` run during an agent event resolves paths inside the PR workspace and never inside `Path.cwd()`.
- **do not:** remove the `is_self_repo` guard in `core/prompts.py`.

## T-08: Widen the output sanitizer [COMPLETED - PR #325]

- **branch:** `fix/sanitizer-token-formats` | **commit:** `fix(security): redact more secret formats in model output`
- **files:** `src/webhook_agent/core/sanitizer_plugin.py`, `tests/unit/core/test_plugins.py` (or the sanitizer's existing test file)
- **problem (reproduced):** only `ghp_`, `gho_`, `AIzaSy`, `sk-` and Bearer are redacted. `ghs_` (the app's own installation tokens), `ghu_`, `ghr_`, `github_pat_`, PEM private keys and `AKIA` keys pass through.
- **do:** add patterns: `gh[pousr]_[A-Za-z0-9]{36,255}` (replacing the two separate `ghp_`/`gho_` patterns), `github_pat_[A-Za-z0-9_]{22,255}`, `AKIA[0-9A-Z]{16}`, and a PEM block pattern `-----BEGIN [A-Z ]*PRIVATE KEY-----` through the matching END marker (if END is absent, redact to end of text). Keep `[REDACTED_SECRET]` as the replacement.
- **tests:** one positive test per format, one negative test (ordinary prose and short `ghs_` look-alikes), existing tests unchanged.
- **done when:** all six leak cases from the audit redact.

## T-09: Lock down credential file permissions [COMPLETED - PR #326]

- **branch:** `fix/credential-file-perms` | **commit:** `fix(security): restrict token cache and key file permissions`
- **files:** `src/webhook_agent/github/credentials.py`, `scripts/load_secrets.sh`, `tests/unit/github/test_credentials.py`
- **problem (reproduced):** the installation-token cache is written as `0644` inside a `0755` directory. `load_secrets.sh` writes key files before `chmod`, leaving a window where they are world-readable.
- **do:**
  1. `cache_path_for_installation`: create the directory with mode `0o700` and `chmod` it if it already exists.
  2. `save_cached_token`: write to a temp file created with `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)` and `os.replace` it into place.
  3. `load_secrets.sh`: `mkdir -p -m 700 /tmp/keys && chmod 700 /tmp/keys`; write each key inside a subshell `( umask 077; gcloud ... > file )`. Do not set a global `umask` (the script is sourced). Keep `set -euo pipefail` and the existing `chmod 600`.
- **tests:** with umask `022` the cache file is `0600` and its directory `0700`; atomic replace leaves no partial file; run `bash -n scripts/load_secrets.sh`.
- **done when:** the permission test passes.

## T-10: Stop scoring unparsable code as docs-only [COMPLETED - PR #327]

- **branch:** `fix/ast-partial-fragments` | **commit:** `fix(analysis): treat unparsable hunks as unknown, not docs`
- **files:** `src/webhook_agent/analysis/ast_analyzer.py`, `src/webhook_agent/tools/ast_tools.py`, `tests/unit/analysis/test_ast_analyzer.py`, `tests/unit/tools/test_ast_tools.py`
- **problem (reproduced):** `analyze_python_code` returns risk `0.2` / `DOCS_OR_COMMENTS` / 0 nodes when a fragment cannot be parsed, even if it contains `eval()`. `verify_python_ast` stitches disjoint diff hunks into one text, producing false warnings (for example "Unreachable statement") and "SYNTAX VALID" claims.
- **do:**
  1. In `analyze_python_code`, apply `textwrap.dedent` before parsing. If still unparsable, return `ASTAnalysisResult` with `parse_failed=True` (new field, default `False`), `categories_found=["UNPARSEABLE_FRAGMENT"]` and `risk_score=0.5`.
  2. In `verify_python_ast`, detect when `code_text` was reconstructed from a diff with non-contiguous line numbers. For such partial views: print `PARSE OK (partial diff view)` instead of `SYNTAX VALID`, skip adjacency-dependent structural warnings (unreachable code), and on parse failure print a neutral note (`Could not parse partial diff fragment; this is expected for hunks`) instead of the SyntaxError alarm.
  3. Full files and single contiguous snippets keep current behavior.
- **tests:** indented hunk with `eval()` is not "docs-only"; the two-hunk diff from the audit produces no "Unreachable statement" warning; a real syntax error in a full file is still reported.
- **done when:** `uv run pytest tests/unit/analysis tests/unit/tools` is green.

## T-11: Harden deploy and CI workflows (HUMAN REVIEW REQUIRED)

- **branch:** `ci/harden-workflows` | **commit:** `ci: harden deploy and PR gate workflows`
- **files:** `.github/workflows/ci.yml`, `.github/workflows/deploy.yml`, `.github/dependabot.yml` (explicitly assigned exception to rule 8)
- **warning:** `deploy.yml` changes trigger a production deploy when merged. Add label `needs-human-review` in the PR body text, open it as **draft**, and do not mark ready.
- **do:**
  1. `deploy.yml`: add `trap` so the git remote URL is reset to the token-free URL on any exit; use `uv sync --no-dev --frozen`; add `concurrency: { group: deploy-prod, cancel-in-progress: false }`.
  2. Replace the `sleep 3` health check with: wait 15 seconds, then require `systemctl --user is-active` and `systemctl --user show -p NRestarts --value hannibal-webhook-agent` equal to `0`; otherwise print status and journal tail and exit 1.
  3. `ci.yml`: add `permissions: { contents: read }` and `timeout-minutes: 15` on the job.
  4. Pin every `uses:` to a full commit SHA with the tag in a trailing comment. Resolve SHAs with `gh api repos/<owner>/<repo>/git/ref/tags/<tag> --jq .object.sha` (dereference annotated tags). Pin `setup-uv`'s `version` to the latest release resolved with `gh api repos/astral-sh/uv/releases/latest --jq .tag_name`. **If you cannot resolve a verified 40-character SHA, leave that tag unpinned and say so; never invent a SHA.**
  5. `dependabot.yml`: add the `github-actions` ecosystem, monthly.
- **done when:** `actionlint` passes if available (otherwise state it was unavailable), and the diff contains no secrets.
- **do not:** remove `GH_TOKEN` (D4).

## T-12: Make the worker fail safely [COMPLETED - PR #330]

- **branch:** `fix/worker-reliability` | **commit:** `fix(worker): retry transient errors and never ack lost dead-letters`
- **files:** `src/webhook_agent/worker.py`, `src/webhook_agent/processor.py`, `main.py`, `tests/unit/test_worker.py`, `tests/unit/test_main_entrypoint.py`
- **problem:** `publish_dead_letter` swallows failures but the message is acked anyway (lost). Every exception, including transient ones, goes straight to the dead-letter topic. `except KeyError: return 3` logs nothing. `logging.basicConfig(level=DEBUG, force=True)` runs at import and raw payloads are logged. Sweep threads can overlap. `processor.process_event` marks the delivery id as processed before executing, so an in-process retry would be suppressed as a duplicate. `main.py` exits 0 regardless of the child's exit code.
- **do:**
  1. `publish_dead_letter` returns `bool`; ack only when it returns `True`. Otherwise leave the message unacked and log an error.
  2. In `processor.process_event`, on any exception during execution remove the delivery id from `_processed_deliveries` and re-raise.
  3. Add `_is_transient(exc)` (Pub/Sub `ServiceUnavailable`/`ResourceExhausted`/`DeadlineExceeded`/`InternalServerError`, `GithubException` status 429/500/502/503/504 or a 403 containing "rate limit", `httpx.TransportError`, `TimeoutError`, `ConnectionError`, and `google.genai.errors.ServerError` if it exists). Retry transient failures up to 3 attempts total with delays from a module constant `_RETRY_DELAYS = (5.0, 20.0)` (±20% jitter), inside the lease extender. Non-transient errors go to the dead-letter topic as today.
  4. Log `logger.error("Missing required environment variable: %s", exc)` before `return 3`.
  5. Remove import-time `basicConfig` from `worker.py`. Configure logging once in `main.py` with level from `LOG_LEVEL` (default `INFO`). Replace the raw payload debug log with event name and delivery id only.
  6. Keep a reference to the sweep thread and skip starting a new sweep while it is alive.
  7. `main.py`: after `worker_proc.join()`, `sys.exit(worker_proc.exitcode or 0)`.
- **tests:** DLQ publish failure leaves the message unacked; transient error is retried then succeeds (monkeypatch `_RETRY_DELAYS` to zeros); non-transient goes to DLQ; retry is not suppressed as duplicate; overlapping sweep skipped; exit code propagation.
- **done when:** `worker.py` coverage is at least 75% (`uv run --with coverage coverage run --source=src -m pytest -q tests/unit/test_worker.py && uv run --with coverage coverage report --include='*/worker.py'`).

## T-13: Remove the unsatisfiable `verify_python_ast` gate [COMPLETED - PR #331]

- **depends_on:** T-04. **branch:** `fix/dead-investigation-gate` | **commit:** `fix(review): drop unsatisfiable AST investigation gate`
- **files:** `src/webhook_agent/tools/github_tools.py`, `src/webhook_agent/core/callbacks.py`, `src/webhook_agent/core/execution.py`, `tests/unit/review/test_review_tool_validation.py`, `tests/unit/core/test_callbacks.py`
- **problem:** `review()` rejects Python PRs unless `verify_python_ast` ran, but that tool is not in either agent's tool list, and `deterministic_precompiled_ast` is set unconditionally (never reset per sender), so the gate either always passes or cannot be satisfied.
- **do:** (1) Replace the rejection with a `logger.warning` when Python files changed and no pre-audit ran. (2) Delete the `tools_executed` bookkeeping for `verify_python_ast` in `callbacks.py` and the `user_state_map` propagation of `deterministic_precompiled_ast` in `execution.py` only if `grep` shows nothing else reads them. (3) Keep every other validation in `review()` intact.
- **tests:** update or delete only the tests asserting the gate; all other review-validation tests unchanged.
- **done when:** a Python-modifying PR review is no longer rejected for a missing `verify_python_ast`.

## T-14: Trim dependencies and dead code [COMPLETED - PR #332]

- **depends_on:** T-13. **branch:** `chore/remove-dead-code` | **commit:** `chore: remove unused dependencies and dead code`
- **files:** `pyproject.toml`, `uv.lock`, `src/webhook_agent/models/secret_manager.py`, `src/webhook_agent/github/credentials.py`, `src/webhook_agent/tools/github_tools.py`, `src/webhook_agent/tools/schemas.py`, `src/webhook_agent/tools/__init__.py`, related tests
- **do:**
  1. Remove `fastapi`, `uvicorn`, `pytz`, `tenacity` from `[project].dependencies` after confirming with `grep -rn` that nothing in `src`, `dev`, `scripts` or `tests` imports them. Run `uv lock` then `uv sync`.
  2. `secret_manager.py`: delete the Secret Manager branch and `_SECRET_CACHE` (D5); `resolve_secret` becomes env-only with the same placeholder filtering and default. Update the module docstring and tests.
  3. `credentials.py`: delete the CLI (`main`, argparse, `__main__`) and the dead usage docstring, after confirming nothing references `credentials.main`.
  4. Delete `create_issue`, `CreateIssueInput`, its `TOOL_INPUT_SCHEMAS` entry and `__all__`/lazy-import entries, and tests that exist only for it.
- **done when:** full suite and mypy are green and `uv run python -c "import webhook_agent.worker"` succeeds.

## T-15: Fix docs drift

- **depends_on:** T-12, T-14. **branch:** `docs/readme-agents-accuracy` | **commit:** `docs: correct README and AGENTS.md drift`
- **files:** `README.md`, `AGENTS.md`
- **do:** (1) README repo tree: add `dev/`, `docs/`, `assets/`. (2) Proactive sweep text: 5-minute ticker, disabled unless `ENABLE_PROACTIVE_SWEEP=1`. (3) Document the new env vars (`TRIGGER_ALLOWED_ASSOCIATIONS`, `TRIGGER_EXEMPT_LOGINS`, `ENABLE_PR_WORKSPACE`, `LOG_LEVEL`) in a short configuration section. (4) AGENTS.md: add one line above the OCR managed block saying `.ocr/` is not committed and the block should be skipped if the file is missing (D6). Do not edit the managed block itself. (5) Do not change model names anywhere.
- **done when:** every path named in README and AGENTS.md exists, except the explicitly annotated `.ocr` one.

## T-16: Tighten lint, typing and coverage

- **depends_on:** all others. **branch:** `chore/lint-typing-coverage` | **commit:** `chore: enable ruff S rules and raise coverage`
- **files:** `pyproject.toml`, targeted source fixes, new tests
- **do:**
  1. Add `"S"` to `[tool.ruff.lint].select`. Fix real findings; for genuine false positives add a targeted `# noqa: S###` with a one-line justification. Do not enable `BLE` (D7).
  2. mypy: for each library in the `ignore_missing_imports` override list (`httpx`, `jwt`, `pydantic`, `cryptography`, `github`, `google.genai`, ...), remove it alone, run `uv run mypy --show-error-codes`; keep the removal only if the result is 10 or fewer errors that you can fix surgically, otherwise restore it and list it in the PR body. Time-box this step.
  3. Add unit tests raising these modules to at least 80% line coverage: `models/genai_provider.py`, `models/model_chain.py`, `github/credentials.py`, `state/review_idempotency.py`.
  4. Optional: bound `InMemorySessionService` growth with an LRU cap (200 sessions) only if it needs nothing beyond the public `.sessions` mapping; otherwise skip and note it.
- **done when:** `./scripts/ruff-all.sh` and the full suite are green.

---

## 5. Human-only follow-ups (not for agents)

1. Check whether an APPROVE from the app counts toward required reviews in branch protection. If it does, raise T-04 priority and consider disabling APPROVE entirely.
2. Review and merge T-11 yourself; it deploys to the VM on merge.
3. Attach the `jules` label yourself to dispatch to Jules.
4. Commit a secrets-redacted copy of the Cloud Run router config so signature verification is auditable.
5. Decide whether to flip `ALLOW_AUTOMATED_MUTATIONS` and `STRICT_REVIEW_GROUNDING` defaults (D2) and whether `calculate_verdict` should default to something other than APPROVE (D3).
6. Confirm whether the GitHub App is public. The worker uses one hard-wired installation id, so other installations' events would use the wrong token.
7. Consider attaching the service account to the VM instead of storing a key file.
8. Drop `GH_TOKEN` from the deploy workflow if the repo is staying public (D4).
