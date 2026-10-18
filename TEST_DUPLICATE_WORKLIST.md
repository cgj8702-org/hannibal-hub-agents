# Test Duplicate Worklist

Auto-generated from structural similarity analysis of the test suite.

Total families: 74
Total tests involved: 195
Estimated savings if collapsed to parametrized: 121

## How to use

- Each family is a set of near-identical test functions (>0.90 structural similarity).
- Pick one canonical test per family, delete the rest, or parametrize.
- Never open a PR titled 'refactor the test suite' — do this opportunistically when editing the file anyway.

## Families

The analysis found 74 near-duplicate test families containing 195 tests total. Collapsing each family to a single parametrized test case would save approximately 121 test functions.

**Family 1 (13 tests)**
## Family 2 (8 tests)
## Family 3 (6 tests)
## Family 4 (6 tests)
## Family 5 (5 tests)
## Family 6 (5 tests)
## Family 7+ (remaining 67 families)

See `docs/plans/repo_reclamation.md` Part 6 for additional context on test hygiene (item G2: collapse duplicate families only when already editing that file).

## Methodology

Analysis was run against the test suite at the time of the repo reclamation effort. All test functions were collected and compared using AST-based structural similarity:

1. Extract all `test_*` functions from `tests/`
2. Build AST token skeletons for each function body
3. Compute pairwise structural similarity using `difflib.SequenceMatcher`
4. Group functions with >0.90 similarity into families

4 same-name function pairs were also identified on the current codebase:
- `test_prefetch_inline_comment_context`: `tests/unit/github/test_pr_context.py:68` <-> `tests/unit/test_worker.py:766` (0.649 similarity)
- `test_prefetch_previous_bot_reviews`: `tests/unit/github/test_pr_context.py:88` <-> `tests/unit/test_worker.py:784` (0.078 similarity)
- `test_is_base_branch_merge_sync_single_parent_returns_false`: `tests/unit/review/test_fast_path.py:257` <-> `tests/unit/test_worker.py:881` (0.300 similarity)
- `test_is_base_branch_merge_sync_non_synchronize_returns_false`: `tests/unit/review/test_fast_path.py:284` <-> `tests/unit/test_worker.py:908` (0.611 similarity)

None of the same-name pairs exceed the 0.90 near-duplicate threshold, but they are worth reviewing as candidates for consolidation.