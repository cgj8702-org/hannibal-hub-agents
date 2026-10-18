# Test Duplicate Worklist

Auto-generated from structural similarity analysis of the test suite.

Total families: 75
Total tests involved: 191
Estimated savings if collapsed to parametrized: 116

## How to use

- Each family is a set of near-identical test functions (>0.90 structural similarity).
- Pick one canonical test per family, delete the rest, or parametrize.
- Never open a PR titled 'refactor the test suite' — do this opportunistically when editing the file anyway.

## Methodology

Analysis was run against the current `main` branch of `cgj8702-org/hannibal-hub-agents`.

All test functions (756 total) were collected and compared using AST-based structural similarity:

1. Extract all `test_*` functions from `tests/`
2. Build AST token skeletons for each function body
3. Compute pairwise structural similarity using `difflib.SequenceMatcher`
4. Group functions with >0.90 similarity into families

75 near-duplicate families were found, containing 191 test functions. Collapsing each family to a single parametrized case would save approximately 116 test functions.

Largest families: [13, 6, 6, 5, 4, 4] tests each.

## Same-name families (4 pairs, lower similarity)

These are functions with the same name but different bodies — worth checking if they're truly distinct or accidental duplication:

- `test_prefetch_inline_comment_context`: tests/unit/github/test_pr_context.py:68 <-> tests/unit/test_worker.py:766 (similarity: 0.649)
- `test_prefetch_previous_bot_reviews`: tests/unit/github/test_pr_context.py:88 <-> tests/unit/test_worker.py:784 (similarity: 0.078)
- `test_is_base_branch_merge_sync_single_parent_returns_false`: tests/unit/review/test_fast_path.py:257 <-> tests/unit/test_worker.py:881 (similarity: 0.300)
- `test_is_base_branch_merge_sync_non_synchronize_returns_false`: tests/unit/review/test_fast_path.py:284 <-> tests/unit/test_worker.py:908 (similarity: 0.611)

## Remediation

Collapse duplicate families opportunistically when editing the affected file. Never open a standalone "refactor the test suite" PR — that produces large, unreviewable diffs.
