# Test Duplicate Worklist

Auto-generated from structural similarity analysis of the test suite.

Total families: 40
Total tests involved: 100
Estimated savings if collapsed to parametrized: 60

## How to use

- Each family is a set of near-identical test functions (>0.90 structural similarity).
- Pick one canonical test per family, delete the rest, or parametrize.
- Never open a PR titled 'refactor the test suite' — do this opportunistically when editing the file anyway.

## Methodology

Analysis was run against the current `main` branch of `cgj8702-org/hannibal-hub-agents`.

All test functions (782 total) were collected and compared using AST-based structural similarity:

1. Extract all `test_*` functions from `tests/`
2. Build AST token skeletons for each function body
3. Compute pairwise structural similarity using `difflib.SequenceMatcher`
4. Group functions with >0.90 similarity into families

40 near-duplicate families were found, containing 100 test functions. Collapsing each family to a single parametrized case would save approximately 60 test functions.

Largest families: [10, 4, 4, 4, 3, 3, 3, 3, 3, 3] tests each.

## Identified Duplicate Families

#### Family 1 (10 functions)
- `test_pull_request_opened`: tests/unit/test_worker.py:87
- `test_pull_request_synchronize`: tests/unit/test_worker.py:91
- `test_pull_request_closed`: tests/unit/test_worker.py:95
- `test_pull_request_ready_for_review`: tests/unit/test_worker.py:99
- `test_pull_request_reopened`: tests/unit/test_worker.py:103
- `test_issue_comment_created`: tests/unit/test_worker.py:107
- `test_pull_request_review_comment_created`: tests/unit/test_worker.py:111
- `test_pull_request_review_submitted`: tests/unit/test_worker.py:115
- `test_pull_request_review_requested`: tests/unit/test_worker.py:119
- `test_normal_event_allowed`: tests/unit/test_worker.py:166

#### Family 2 (4 functions)
- `test_review_rejects_generic_codebase_path`: tests/unit/review/test_review_tool_validation.py:75
- `test_review_rejects_missing_line_number`: tests/unit/review/test_review_tool_validation.py:96
- `test_review_rejects_missing_suggested_fix`: tests/unit/review/test_review_tool_validation.py:117
- `test_review_rejects_boilerplate_suggested_fix`: tests/unit/review/test_review_tool_validation.py:138

#### Family 3 (4 functions)
- `test_returns_true_for_503_error`: tests/unit/test_agent_core.py:158
- `test_returns_true_for_500_error`: tests/unit/test_agent_core.py:167
- `test_returns_true_for_429_error`: tests/unit/test_agent_core.py:176
- `test_returns_false_for_400_error`: tests/unit/test_agent_core.py:185

#### Family 4 (4 functions)
- `test_suppress_bot_actor`: tests/unit/test_worker.py:178
- `test_suppress_bot_comment_author`: tests/unit/test_worker.py:212
- `test_suppress_bot_comment_author_without_suffix`: tests/unit/test_worker.py:221
- `test_suppress_bot_review_author`: tests/unit/test_worker.py:231

#### Family 5 (3 functions)
- `test_compare_signatures_breaking_required_param_added`: tests/unit/analysis/test_symbol_graph.py:99
- `test_compare_signatures_breaking_param_removed`: tests/unit/analysis/test_symbol_graph.py:111
- `test_compare_signatures_breaking_default_removed`: tests/unit/analysis/test_symbol_graph.py:123

#### Family 6 (3 functions)
- `test_verify_call_site_compatibility_missing_positional`: tests/unit/analysis/test_symbol_graph.py:155
- `test_verify_call_site_compatibility_too_many_args`: tests/unit/analysis/test_symbol_graph.py:166
- `test_verify_call_site_compatibility_missing_kwonly`: tests/unit/analysis/test_symbol_graph.py:177

#### Family 7 (3 functions)
- `test_stale_commit_is_not_reviewed`: tests/unit/review/test_ci_gate.py:333
- `test_closed_pr_is_not_reviewed`: tests/unit/review/test_ci_gate.py:341
- `test_already_reviewed_commit_is_not_reviewed_again`: tests/unit/review/test_ci_gate.py:349

#### Family 8 (3 functions)
- `test_placeholder_path_is_hard`: tests/unit/review/test_grounding.py:48
- `test_unchanged_path_is_hard`: tests/unit/review/test_grounding.py:57
- `test_missing_evidence_is_hard`: tests/unit/review/test_grounding.py:88

#### Family 9 (3 functions)
- `test_critic_evaluate_candidate_drops_nit`: tests/unit/review/test_hunter_critic.py:66
- `test_critic_evaluate_candidate_drops_low_confidence`: tests/unit/review/test_hunter_critic.py:78
- `test_critic_evaluate_candidate_accepts_high_confidence`: tests/unit/review/test_hunter_critic.py:90

#### Family 10 (3 functions)
- `test_apply_scorecard_floor_overrides_pass_to_warn`: tests/unit/review/test_scorecard_floor.py:63
- `test_apply_scorecard_floor_security_overrides_pass_to_warn`: tests/unit/review/test_scorecard_floor.py:430
- `test_apply_scorecard_floor_test_coverage_warn`: tests/unit/review/test_scorecard_floor.py:562

#### Family 11 (2 functions)
- `test_is_pure_dependency_pr_dependabot_allowed`: tests/unit/analysis/test_lockfile_validator.py:61
- `test_is_pure_dependency_pr_pyproject_and_lock_allowed`: tests/unit/analysis/test_lockfile_validator.py:69

#### Family 12 (2 functions)
- `test_evaluate_security_integrity_scan_errors_unknown`: tests/unit/analysis/test_security_scanner.py:192
- `test_evaluate_security_integrity_clean_pass`: tests/unit/analysis/test_security_scanner.py:201

#### Family 13 (2 functions)
- `test_evaluate_contract_integrity_proven_fail`: tests/unit/analysis/test_symbol_graph.py:366
- `test_evaluate_contract_integrity_suspected_warn`: tests/unit/analysis/test_symbol_graph.py:385

#### Family 14 (2 functions)
- `test_prefetch_previous_bot_reviews`: tests/unit/github/test_pr_context.py:88
- `test_prefetch_previous_bot_reviews_ignores_other_bots`: tests/unit/github/test_pr_context.py:196

#### Family 15 (2 functions)
- `test_firestore_registry_unwraps_nested_503_exception`: tests/unit/models/test_depleted_registry.py:32
- `test_simple_registry_classifies_nested_503`: tests/unit/models/test_depleted_registry.py:69

#### Family 16 (2 functions)
- `test_firestore_registry_classifies_generic_429`: tests/unit/models/test_depleted_registry.py:47
- `test_firestore_registry_classifies_rpd`: tests/unit/models/test_depleted_registry.py:58

#### Family 17 (2 functions)
- `test_resolve_webhook_api_key_fast_fail_missing_free_key`: tests/unit/models/test_dual_tier_resolution.py:13
- `test_resolve_webhook_api_key_fast_fail_missing_paid_key`: tests/unit/models/test_dual_tier_resolution.py:22

#### Family 18 (2 functions)
- `test_merge_conflict_detected_on_unmergeable_pr`: tests/unit/review/test_proactive_service.py:86
- `test_merge_conflict_detected_on_dirty_state`: tests/unit/review/test_proactive_service.py:104

#### Family 19 (2 functions)
- `test_review_allows_python_pr_with_ast_verification`: tests/unit/review/test_review_tool_validation.py:247
- `test_review_skips_investigation_gate_when_no_python_files`: tests/unit/review/test_review_tool_validation.py:265

#### Family 20 (2 functions)
- `test_grounding_does_not_reject_when_not_strict`: tests/unit/review/test_review_tool_validation.py:462
- `test_grounding_soft_issues_never_reject_even_when_strict`: tests/unit/review/test_review_tool_validation.py:485

#### Family 21 (2 functions)
- `test_sync_review_request_changes_evaluates_after_grounding_allows_ungrounded_resolution`: tests/unit/review/test_review_tool_validation.py:683
- `test_sync_review_request_changes_grounds_resolutions_when_diff_text_empty`: tests/unit/review/test_review_tool_validation.py:742

#### Family 22 (2 functions)
- `test_apply_scorecard_floor_overrides_pass_to_fail`: tests/unit/review/test_scorecard_floor.py:33
- `test_apply_scorecard_floor_security_overrides_pass_to_fail`: tests/unit/review/test_scorecard_floor.py:413

#### Family 23 (2 functions)
- `test_apply_scorecard_floor_preserves_model_fail_when_deterministic_is_pass`: tests/unit/review/test_scorecard_floor.py:77
- `test_apply_scorecard_floor_security_preserves_model_fail_when_deterministic_is_pass`: tests/unit/review/test_scorecard_floor.py:444

#### Family 24 (2 functions)
- `test_enforce_verdict_direct_deterministic_contract_item_param`: tests/unit/review/test_scorecard_floor.py:148
- `test_enforce_verdict_direct_deterministic_security_item_param`: tests/unit/review/test_scorecard_floor.py:494

#### Family 25 (2 functions)
- `test_enforce_verdict_sync_review_contract_floor`: tests/unit/review/test_scorecard_floor.py:172
- `test_enforce_verdict_sync_review_security_floor`: tests/unit/review/test_scorecard_floor.py:518

#### Family 26 (2 functions)
- `test_tampered_body_is_401`: tests/unit/router/test_router.py:165
- `test_non_sha256_prefix_is_401`: tests/unit/router/test_router.py:176

#### Family 27 (2 functions)
- `test_non_utf8_body_is_400`: tests/unit/router/test_router.py:295
- `test_malformed_json_is_400`: tests/unit/router/test_router.py:307

#### Family 28 (2 functions)
- `test_bot_sender_blocked_by_writeback_policy`: tests/unit/test_agent_core.py:56
- `test_bot_sender_without_suffix_blocked`: tests/unit/test_agent_core.py:72

#### Family 29 (2 functions)
- `test_slash_command_comment_routes_to_primary_model`: tests/unit/test_agent_core.py:300
- `test_bot_mention_comment_routes_to_primary_model`: tests/unit/test_agent_core.py:310

#### Family 30 (2 functions)
- `test_build_user_message_injects_deterministic_compiler_dossier`: tests/unit/test_agent_core.py:383
- `test_build_user_message_injects_symbol_impact_dossier`: tests/unit/test_agent_core.py:404

#### Family 31 (2 functions)
- `test_read_file_returns_content`: tests/unit/test_agent_core.py:606
- `test_read_file_with_ref`: tests/unit/test_agent_core.py:623

#### Family 32 (2 functions)
- `test_conversational_comment_dispatch_posts_comment`: tests/unit/test_agent_core.py:1107
- `test_issues_opened_conversational_dispatch`: tests/unit/test_agent_core.py:1249

#### Family 33 (2 functions)
- `test_label_created`: tests/unit/test_worker.py:123
- `test_label_deleted`: tests/unit/test_worker.py:132

#### Family 34 (2 functions)
- `test_installation_created`: tests/unit/test_worker.py:136
- `test_installation_deleted`: tests/unit/test_worker.py:140

#### Family 35 (2 functions)
- `test_ping`: tests/unit/test_worker.py:144
- `test_unknown_event`: tests/unit/test_worker.py:148

#### Family 36 (2 functions)
- `test_suppress_performed_via_github_app`: tests/unit/test_worker.py:240
- `test_suppress_performed_via_github_app_in_comment`: tests/unit/test_worker.py:255

#### Family 37 (2 functions)
- `test_prefetch_previous_bot_reviews`: tests/unit/test_worker.py:783
- `test_prefetch_previous_bot_reviews_is_strictly_read_only`: tests/unit/test_worker.py:928

#### Family 38 (2 functions)
- `test_worker_dlq_publish_failure_leaves_unacked`: tests/unit/test_worker.py:1715
- `test_worker_dlq_publish_success_acks`: tests/unit/test_worker.py:1754

#### Family 39 (2 functions)
- `test_worker_transient_error_exhausted_goes_to_dlq`: tests/unit/test_worker.py:1838
- `test_worker_non_transient_error_goes_to_dlq_without_retry`: tests/unit/test_worker.py:1880

#### Family 40 (2 functions)
- `test_search_files_invalid_regex`: tests/unit/tools/test_search_tools.py:92
- `test_search_files_path_traversal_denied`: tests/unit/tools/test_search_tools.py:106

## Remediation

Collapse duplicate families opportunistically when editing the affected file. Never open a standalone "refactor the test suite" PR — that produces large, unreviewable diffs.
