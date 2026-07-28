## Plan: Webhook event orchestration

Build the webhook agent as a local GitHub App orchestrator that uses Gemma 4 to plan actions, validates them in the app, and executes GitHub writebacks safely. The first phase should make the bot react to the full webhook surface you care about — PRs, issue comments, review comments, review submissions, labels, reviewer requests, and PR creation/merging — while explicitly preventing self-trigger loops when the bot becomes the actor.

**Steps**
- [x] 1. Normalize incoming webhook payloads at the ingress boundary in `src/webhook_agent/app.py` so every delivery includes `event_name`, `action`, `sender`, `installation`, `repository`, `delivery_id`, and the raw payload. This step depends on the event shape being consistent so downstream routing can stay simple.
- [x] 2. Add an event router in `src/webhook_agent/worker.py` that maps `X-GitHub-Event` and payload action to canonical internal event categories such as `pull_request.opened`, `pull_request.synchronize`, `issue_comment.created`, `pull_request_review_comment.created`, `pull_request_review.submitted`, `pull_request_review_requested`, `label.*`, and `installation.*`. This can run in parallel with step 3 once the canonical event contract is defined.
- [x] 3. Introduce loop-avoidance and dedupe rules in the worker and agent core before any model call: ignore deliveries from `hannibal-hub-agents[bot]` unless the event is explicitly a follow-up action we want, ignore comments/reviews authored by the app itself, and record processed delivery IDs. This depends on step 1's normalized sender/installation metadata.
- [x] 4. Refactor `src/webhook_agent/agent_core.py` so `decide()` accepts a canonical event object and returns a list of structured tool calls for specific event classes rather than only generic writeback flags. Add separate planner branches for PR-created, PR-updated, issue-comment reply, review-comment reply, review-submitted review, reviewer assignment, label sync, and PR creation/merge workflows. This depends on steps 1 and 2.
- [x] 5. Expand `src/webhook_agent/gemma_planner.py` tool declarations and prompt guidance so Gemma 4 can emit only the allowed action set for the canonical event types. Keep the schema tight and action-oriented, with tool schemas for comment reply, review reply, review submission, PR creation, branch commit, label changes, reviewer requests, and merge operations. This can be done in parallel with step 6.
- [x] 6. Extend `src/webhook_agent/agent_core.py` validation and execution paths for the new tool set, including explicit reply/comment threading behavior, review submission handling, and PR lifecycle actions. Keep mutation gates and dry-run behavior intact. This depends on step 4.
- [x] 7. Add a "GitHub writeback policy" layer in the agent core that blocks self-referential loops and only allows the bot to act on its own outputs when the event is a deliberate follow-up. Use repo/actor/reason checks so the bot can eventually post PRs and comments without creating infinite recursion. This depends on step 3.
- [x] 8. Align the webhook agent behavior with the `chatbot-repo/dev/code_reviewer.py` prototype by reusing its strengths: schema-first outputs, explicit review prompts, line-aware comments, and single-pass determinism for review-style tasks. Split its review logic into reusable planning and writeback concepts so the same architecture can power both review and general automation. This can proceed after the event contract is stable.
- [x] 9. Add verification cases for each major event class: PR opened, PR synchronize, issue comment created, review comment created, review submitted, reviewer requested, label change, and bot-authored self-loop suppression. Include both dry-run and real-writeback coverage. This depends on steps 1, 3, 4, and 6.
- [x] 10. Add retry and fallback model support for server errors (503, 500, 429) with configurable `GEMMA_MODEL_MAX_RETRIES` and `GEMMA_MODEL_FALLBACK` environment variables.

**Relevant files**
- `/home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/app.py` — normalize webhook ingress payloads and forward event metadata.
- `/home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/worker.py` — route canonical events, enforce loop protection, and call the agent core.
- `/home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/agent_core.py` — event-to-tool planning, policy gates, validation, and GitHub execution.
- `/home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/gemma_planner.py` — Gemma 4 function declarations and planning prompt.
- `/home/carly/coding/synced-repos-cgj8702/hannibal/chatbot-repo/dev/code_reviewer.py` — prototype for schema-first review output and GitHub writeback style.

**Verification**
1. Add or update unit tests for canonical event routing, bot-loop suppression, and the new tool validators.
2. Run `uv sync` and `python3 -m py_compile` on the webhook agent modules after changes.
3. Exercise the worker locally with synthetic webhook payloads for PR, comment, and review events.
4. Publish a test message to the existing `webhook` topic and confirm the worker handles it without retriggering on bot-authored follow-up actions.
5. If Gemma 4 is enabled, confirm the planner emits only declared function calls and that the agent core rejects malformed arguments.

**Decisions**
- Use `hannibal-hub-agents[bot]` identity as the primary loop-suppression signal, but keep a deliberate-override path for approved follow-up actions.
- Keep the webhook agent local-first and modular: Gemini plans, the app validates, and the worker executes.
- Preserve the `code_reviewer.py` prototype’s schema-first style, but generalize it beyond PR review into a broader event router.
- Do not expand the webhook surface blindly; prioritize the event classes that map to concrete bot behaviors you already want.

**Further Considerations**
1. Do you want the bot to act on every supported event by default, or only on a curated allowlist of repos/event types initially?
2. Should self-authored comments/reviews be ignored completely, or should some of them trigger a special follow-up workflow?
3. Do you want PR creation to be fully autonomous, or should Gemma only draft a branch/PR plan unless a policy gate explicitly approves mutation?