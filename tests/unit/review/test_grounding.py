"""Unit tests for deterministic review-citation grounding checks.

These tests exercise :mod:`webhook_agent.review.grounding` with hand-written diffs. They
assert both the accept and the reject path, because a verifier that only ever rejects is as
useless as one that only ever accepts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from webhook_agent.review.grounding import (
    Finding,
    citation_overlap,
    diff_text_from_patches,
    extract_bare_symbols,
    extract_cited_paths,
    extract_cited_symbols,
    extract_identifiers,
    hard_reasons,
    touched_lines_by_file,
    verify_citations,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

# src/main.py: new-side lines 10 and 11 are added; line 12 is unchanged context.
SAMPLE_DIFF = (
    "--- a/src/main.py\n"
    "+++ b/src/main.py\n"
    "@@ -10,2 +10,3 @@\n"
    "+new_line_1\n"
    "+new_line_2\n"
    " context_line\n"
)


def test_added_and_context_lines_are_tracked() -> None:
    """Lines 10-11 are added (anchors); line 12 exists as context only."""
    issues = verify_citations(
        [Finding("invariant", "src/main.py", 10, "new_line_1 is present")],
        SAMPLE_DIFF,
        ["src/main.py"],
    )
    assert issues == []


def test_placeholder_path_is_hard() -> None:
    """A placeholder path is a hard failure, matching review()'s existing gate."""
    issues = verify_citations(
        [Finding("invariant", "codebase", 10, "some invariant")], SAMPLE_DIFF, ["src/main.py"]
    )
    assert [i.code for i in issues] == ["empty_or_placeholder_path"]
    assert issues[0].is_hard


def test_unchanged_path_is_hard() -> None:
    """Citing a file the PR does not touch is a hard failure."""
    issues = verify_citations(
        [Finding("critical", "src/other.py", 4, "something is wrong")],
        SAMPLE_DIFF,
        ["src/main.py"],
    )
    assert [i.code for i in issues] == ["path_not_changed"]
    assert issues[0].is_hard


def test_line_outside_the_diff_is_soft() -> None:
    """A line the diff never displays is unverifiable, not automatically wrong."""
    issues = verify_citations(
        [Finding("invariant", "src/main.py", 99, "guard holds")], SAMPLE_DIFF, ["src/main.py"]
    )
    assert [i.code for i in issues] == ["line_outside_diff"]
    assert not issues[0].is_hard


def test_context_only_line_is_soft() -> None:
    """Citing an unchanged context line is suspicious but not a hard failure."""
    issues = verify_citations(
        [Finding("invariant", "src/main.py", 12, "context_line is unchanged")],
        SAMPLE_DIFF,
        ["src/main.py"],
    )
    assert [i.code for i in issues] == ["context_only_line"]
    assert not issues[0].is_hard


def test_missing_evidence_is_hard() -> None:
    """A citation with no evidence is a hard failure."""
    issues = verify_citations(
        [Finding("invariant", "src/main.py", 10, "   ")], SAMPLE_DIFF, ["src/main.py"]
    )
    assert [i.code for i in issues] == ["missing_evidence"]
    assert issues[0].is_hard


def test_changed_file_without_patch_is_unverifiable_not_unwanted() -> None:
    """A changed binary file with no patch must be 'soft', never 'path_not_changed'.

    This is the false-positive guard: the authoritative changed-path list is what
    distinguishes "the PR did not touch this" from "there is no patch text to check".
    """
    issues = verify_citations(
        [Finding("invariant", "assets/logo.png", 3, "asset role is unchanged")],
        SAMPLE_DIFF,
        ["src/main.py", "assets/logo.png"],
    )
    assert [i.code for i in issues] == ["path_unverifiable"]
    assert not issues[0].is_hard


def test_prefixed_and_bare_paths_are_equivalent() -> None:
    """git's a/ and b/ prefixes are stripped before comparison."""
    issues = verify_citations(
        [Finding("invariant", "b/src/main.py", 10, "new_line_1 is present")],
        SAMPLE_DIFF,
        ["src/main.py"],
    )
    assert issues == []


def test_hard_reasons_filters_out_soft_issues() -> None:
    """hard_reasons() returns only the model-facing hard failures."""
    issues = verify_citations(
        [
            Finding("invariant", "codebase", 10, "evidence"),
            Finding("invariant", "src/main.py", 99, "evidence"),
        ],
        SAMPLE_DIFF,
        ["src/main.py"],
    )
    assert len(issues) == 2
    assert len(hard_reasons(issues)) == 1
    assert "no exact file path" in hard_reasons(issues)[0]


def test_low_overlap_is_advisory_and_never_gates() -> None:
    """A legitimate paraphrased claim can score 0.0 overlap and must still pass.

    Executable documentation of why citation_overlap is a reporting metric only: on the
    real PR #258 fixture the bot's legitimate DRY suggestion scores 0.0.
    """
    issues = verify_citations(
        [
            Finding(
                "suggestion",
                "src/main.py",
                10,
                "Extract the duplicated alias resolution into a shared helper mixin",
            )
        ],
        SAMPLE_DIFF,
        ["src/main.py"],
    )
    overlaps = citation_overlap(
        [Finding("suggestion", "src/main.py", 10, "Extract the duplicated alias resolution")],
        SAMPLE_DIFF,
    )
    assert issues == [], "prose claims must not be rejected for low lexical overlap"
    assert overlaps and overlaps[0].overlap == 0.0


def test_diff_text_from_patches_skips_missing_patches() -> None:
    """Files with patch=None (binary or truncated) contribute no diff text."""
    diff = diff_text_from_patches({"a/b.py": None, "b/c.py": "@@ -1,1 +1,1 @@\n+x\n"})
    assert "+++ b/c.py" in diff
    assert "b.py" not in diff.replace("+++ b/c.py", "")


def test_extract_identifiers_lowercases_and_drops_digits_and_short_tokens() -> None:
    """The diff-side tokenizer keeps word-like tokens and lowercases them."""
    tokens = extract_identifiers("Foo bar _x_ grounding_patches 42 pr")
    assert tokens == {"foo", "bar", "_x_", "grounding_patches"}


def test_extract_cited_symbols_reads_only_backticked_spans() -> None:
    """The claim-side extractor trusts backticks, which is the reviewer's convention."""
    text = "`_collect_pr_patches()` and pr.get_files() and `STRICT_REVIEW_GROUNDING`"
    assert extract_cited_symbols(text) == {"_collect_pr_patches", "strict_review_grounding"}


def test_extract_cited_symbols_ignores_unbackticked_code() -> None:
    """Prose that mentions code without backticks yields no symbols (R2, not R1)."""
    assert extract_cited_symbols("the patch collection reuses cached data") == set()


def test_extract_bare_symbols_reads_snake_case_without_backticks() -> None:
    """The backtick dodge is closed: bare _collect_pr_patches is still a symbol."""
    text = "_collect_pr_patches now reuses cached patches"
    assert extract_bare_symbols(text) == {"_collect_pr_patches"}


def test_extract_bare_symbols_ignores_plain_english_prose() -> None:
    """Prose cannot manufacture a pass: no code-like token, no symbol."""
    assert extract_bare_symbols("the patch collection fallback now reuses cached data") == set()


def test_extract_bare_symbols_reads_camel_case_and_skips_stopwords() -> None:
    """camelCase counts as code; stopwords never do, even with underscores nearby."""
    assert extract_bare_symbols("mergePr was removed") == {"mergepr"}
    assert extract_bare_symbols("the invariant still holds") == set()


def test_extract_cited_paths_reads_bare_and_full_paths() -> None:
    """Both 'fixed in github_tools.py' and 'see src/foo.py' are paths."""
    assert extract_cited_paths("fixed in github_tools.py") == {"github_tools.py"}
    assert extract_cited_paths("see src/webhook_agent/tools/github_tools.py") == {
        "src/webhook_agent/tools/github_tools.py"
    }


def test_extract_cited_paths_ignores_prose_without_paths() -> None:
    """'the fallback path' has no slash or extension, so it is not a path."""
    assert extract_cited_paths("the patch collection fallback now reuses cached data") == set()


def test_extract_cited_paths_ignores_dotted_calls() -> None:
    """`subprocess.run` and `gh.api` are symbols, not file paths."""
    assert extract_cited_paths("uses subprocess.run with check=False") == set()
    assert extract_cited_paths("calls gh.api paginate") == set()


def test_touched_lines_by_file() -> None:
    """touched_lines_by_file parses unified diff patches and handles None patch."""
    patches = {
        "src/main.py": "@@ -10,2 +10,3 @@\n+new_line_1\n+new_line_2\n context_line\n",
        "assets/logo.png": None,
    }
    touched = touched_lines_by_file(patches)
    assert touched["src/main.py"] == {10, 11}
    assert touched["assets/logo.png"] == set()


def test_verify_citations_with_touched_dict() -> None:
    """verify_citations accepts (findings, touched, diff_text)."""
    patches = {
        "src/main.py": "@@ -10,2 +10,3 @@\n+new_line_1\n+new_line_2\n context_line\n",
    }
    touched = touched_lines_by_file(patches)
    diff = diff_text_from_patches(patches)

    issues = verify_citations(
        [
            Finding("invariant", "src/main.py", 10, "`new_line_1`"),
            Finding("invariant", "src/main.py", 99, "`new_line_1`"),
        ],
        touched,
        diff,
    )
    assert len(issues) == 1
    assert issues[0].code == "line_outside_diff"


# ---------------------------------------------------------------------------
# Phase 8: Resolution Integrity & Deletion-Aware Evidence Tests
# ---------------------------------------------------------------------------

DELETION_DIFF = (
    "--- a/src/worker.py\n"
    "+++ b/src/worker.py\n"
    "@@ -20,4 +20,1 @@\n"
    "-unsafe_fallback_call()\n"
    "-legacy_clean_step()\n"
    "-extra_line()\n"
    "+safe_call()\n"
)


def test_resolution_unresolved_when_evidence_not_in_diff() -> None:
    """A RESOLVED claim whose evidence is not supported by current diff/code stays UNRESOLVED."""
    from webhook_agent.review.grounding import verify_resolutions

    # Finding with RESOLVED status, but evidence describes something NOT in the diff
    res_items = [
        {
            "item_description": "pr.get_files() latency overhead risk",
            "status": "RESOLVED",
            "evidence": "fallback only runs on un-investigated PR paths",
            "path": "src/webhook_agent/tools/github_tools.py",
        }
    ]
    # In DELETION_DIFF, src/webhook_agent/tools/github_tools.py isn't even touched, nor is that text present.
    checked = verify_resolutions(res_items, DELETION_DIFF, ["src/worker.py"])
    assert len(checked) == 1
    assert checked[0]["status"] == "UNRESOLVED"
    assert "not grounded in current code diff" in checked[0].get("grounding_note", "").lower()


def test_resolution_deletion_aware_evidence_accepts_removal_hunk() -> None:
    """When cited code was deleted, pre-image removal hunk ('-') acts as supporting evidence."""
    from webhook_agent.review.grounding import verify_resolutions

    res_items = [
        {
            "item_description": "unsafe_fallback_call security flaw",
            "status": "RESOLVED",
            "evidence": "-unsafe_fallback_call() removed from worker",
            "path": "src/worker.py",
            "line": 20,
        }
    ]
    checked = verify_resolutions(res_items, DELETION_DIFF, ["src/worker.py"])
    assert len(checked) == 1
    assert checked[0]["status"] == "RESOLVED"


def test_behavioral_invariant_without_diff_quote_or_test_is_rejected() -> None:
    """A behavioral invariant claim without test or diff quote evidence fails grounding."""
    from webhook_agent.review.grounding import Finding, verify_citations

    # Static fact invariant vs Behavioral claim invariant
    static_inv = Finding(
        kind="invariant",
        path="src/main.py",
        line=10,
        evidence="DEFAULT_STRICT_REVIEW_GROUNDING = '0'",
    )
    behavioral_inv = Finding(
        kind="invariant",
        path="src/main.py",
        line=10,
        evidence="grounding skips gracefully when patch text is unavailable",
    )

    issues_static = verify_citations([static_inv], SAMPLE_DIFF, ["src/main.py"])
    issues_behavioral = verify_citations([behavioral_inv], SAMPLE_DIFF, ["src/main.py"])

    assert issues_static == []
    assert len(issues_behavioral) == 1
    assert issues_behavioral[0].code == "unsubstantiated_behavioral_invariant"


def test_resolution_path_only_without_symbols_is_unresolved() -> None:
    """A resolution citing only a changed path but no symbols cannot claim RESOLVED."""
    from webhook_agent.review.grounding import verify_resolutions

    res_items = [
        {
            "item_description": "Cleaned up worker file",
            "status": "RESOLVED",
            "evidence": "Fixed things properly",
            "path": "src/worker.py",
        }
    ]
    checked = verify_resolutions(res_items, DELETION_DIFF, ["src/worker.py"])
    assert len(checked) == 1
    assert checked[0]["status"] == "UNRESOLVED"


def test_resolution_symbol_evidence_scoped_to_cited_path() -> None:
    """Symbol evidence from another file cannot ground a resolution for a cited path."""
    from webhook_agent.review.grounding import verify_resolutions

    multi_file_diff = (
        "--- a/src/alpha.py\n"
        "+++ b/src/alpha.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+alpha_feature_flag = True\n"
        "--- a/src/beta.py\n"
        "+++ b/src/beta.py\n"
        "@@ -1,2 +1,3 @@\n"
        "+beta_helper_call()\n"
    )
    # Resolution cites src/beta.py, but evidence references a symbol present only in src/alpha.py
    res_items = [
        {
            "item_description": "Update beta logic",
            "status": "RESOLVED",
            "evidence": "alpha_feature_flag enables logic",
            "path": "src/beta.py",
        }
    ]
    checked = verify_resolutions(res_items, multi_file_diff, ["src/alpha.py", "src/beta.py"])
    assert len(checked) == 1
    assert checked[0]["status"] == "UNRESOLVED"

    # When citing the symbol that actually appears in src/beta.py, it is grounded as RESOLVED
    res_items_valid = [
        {
            "item_description": "Update beta logic",
            "status": "RESOLVED",
            "evidence": "beta_helper_call invoked",
            "path": "src/beta.py",
        }
    ]
    checked_valid = verify_resolutions(
        res_items_valid, multi_file_diff, ["src/alpha.py", "src/beta.py"]
    )
    assert len(checked_valid) == 1
    assert checked_valid[0]["status"] == "RESOLVED"


def test_behavioral_invariant_with_diff_quote_or_test_passes() -> None:
    """A behavioral invariant passes when supported by a verified diff quote or test function citation."""
    from webhook_agent.review.grounding import Finding, verify_citations

    # 1. Backtick quote present in diff passes
    inv_quote = Finding(
        kind="invariant",
        path="src/main.py",
        line=10,
        evidence="skips processing when `new_line_1` encountered",
    )
    assert verify_citations([inv_quote], SAMPLE_DIFF, ["src/main.py"]) == []

    # 2. Test citation token passes
    inv_test = Finding(
        kind="invariant",
        path="src/main.py",
        line=10,
        evidence="gracefully handles retries verified in test_retry_fallback",
    )
    assert verify_citations([inv_test], SAMPLE_DIFF, ["src/main.py"]) == []

    # 3. Prose containing '=' but no diff quote or test citation is rejected
    inv_prose_equals = Finding(
        kind="invariant",
        path="src/main.py",
        line=10,
        evidence="handles retries when retry_count = None",
    )
    issues = verify_citations([inv_prose_equals], SAMPLE_DIFF, ["src/main.py"])
    assert len(issues) == 1
    assert issues[0].code == "unsubstantiated_behavioral_invariant"


def test_backticked_path_citation_not_treated_as_code_quote() -> None:
    """A backticked file path or line citation in evidence must not trigger evidence_not_in_diff."""
    from webhook_agent.review.grounding import Finding, verify_citations

    finding = Finding(
        kind="invariant",
        path="src/main.py",
        line=10,
        evidence="Shadow mode telemetry logging verified (`src/main.py:10`)",
    )
    issues = verify_citations([finding], SAMPLE_DIFF, ["src/main.py"])
    assert issues == []


def test_multi_symbol_prose_does_not_concatenate_into_diff_failure() -> None:
    """Multiple backticked symbols in prose must not be concatenated into a continuous diff query."""
    from webhook_agent.review.grounding import Finding, verify_citations

    finding = Finding(
        kind="suggestion",
        path="src/main.py",
        line=10,
        evidence="Consider refactoring `new_line_1` and `new_line_2` into a single helper.",
    )
    issues = verify_citations([finding], SAMPLE_DIFF, ["src/main.py"])
    assert issues == []


def test_long_untrue_code_quote_still_rejected() -> None:
    """A long code quote (> 24 chars) that does not exist in the diff must be rejected."""
    from webhook_agent.review.grounding import Finding, verify_citations

    finding = Finding(
        kind="critical",
        path="src/main.py",
        line=10,
        evidence="Fabricated code quote: `this_function_does_not_exist_anywhere_in_the_diff()`",
    )
    issues = verify_citations([finding], SAMPLE_DIFF, ["src/main.py"])
    assert len(issues) == 1
    assert issues[0].code == "evidence_not_in_diff"
    assert issues[0].is_hard


def test_is_path_citation_helper() -> None:
    """Verify _is_path_citation accurately distinguishes file paths from code symbols."""
    from webhook_agent.review.grounding import _is_path_citation

    # File paths and line citations
    assert _is_path_citation("src/main.py:10", "src/main.py") is True
    assert _is_path_citation(".github/dependabot.yml:11-19") is True
    assert _is_path_citation("src/webhook_agent/processor.py") is True
    assert _is_path_citation("main.py", "src/main.py") is True
    assert _is_path_citation("main.py") is True

    # Code symbols and expressions
    assert _is_path_citation("ToolInputBase") is False
    assert _is_path_citation("calculate_strict_verdict") is False
    assert _is_path_citation("logger.info('hello')") is False
    assert _is_path_citation("x = 1") is False
    assert _is_path_citation("self.diff") is False
    assert _is_path_citation("cls.handler") is False


def test_is_machine_verified_invariant() -> None:
    """Verify is_machine_verified_invariant distinguishes machine-checked from semantic claims."""
    from webhook_agent.review.grounding import is_machine_verified_invariant

    # 1. Path:line citation with non-behavioral prose -> Machine-Verified
    inv_fact = "Telemetry logging configured (`src/main.py:10`)"
    assert is_machine_verified_invariant(inv_fact, SAMPLE_DIFF, ["src/main.py"]) is True

    # 2. Path:line citation with behavioral keyword AND diff quote -> Machine-Verified
    inv_diff_quote = "skips processing when `new_line_1` encountered (`src/main.py:10`)"
    assert is_machine_verified_invariant(inv_diff_quote, SAMPLE_DIFF, ["src/main.py"]) is True

    # 3. Path:line citation with behavioral keyword AND test citation -> Machine-Verified
    inv_test = "gracefully handles retries verified in test_retry_fallback (`src/main.py:10`)"
    assert is_machine_verified_invariant(inv_test, SAMPLE_DIFF, ["src/main.py"]) is True

    # 4. Path:line citation with behavioral keyword WITHOUT diff quote or test -> Semantic
    inv_unsubstantiated = "handles retries when retry_count = None (`src/main.py:10`)"
    assert is_machine_verified_invariant(inv_unsubstantiated, SAMPLE_DIFF, ["src/main.py"]) is False

    # 5. Path not in diff -> Not machine verified (Semantic or ungrounded)
    inv_wrong_path = "Logging configured (`src/other.py:10`)"
    assert is_machine_verified_invariant(inv_wrong_path, SAMPLE_DIFF, ["src/main.py"]) is False

    # 6. No path citation, broad natural language assertion -> Semantic
    inv_prose = "Backward compatibility preserved across async workers."
    assert is_machine_verified_invariant(inv_prose, SAMPLE_DIFF, ["src/main.py"]) is False

    # 7. No path citation, but cites test present in diff -> Machine-Verified
    diff_with_test = SAMPLE_DIFF + "\n+def test_worker_pool(): pass\n"
    inv_test_diff = "Worker pool concurrency verified in test_worker_pool."
    assert is_machine_verified_invariant(inv_test_diff, diff_with_test, ["src/main.py"]) is True


def test_partition_invariants() -> None:
    """Verify partition_invariants cleanly splits mixed invariant lists."""
    from webhook_agent.review.grounding import partition_invariants

    invariants = [
        "Telemetry logging configured (`src/main.py:10`)",
        "handles retries when retry_count = None (`src/main.py:10`)",
        "Backward compatibility preserved across async workers.",
        "skips processing when `new_line_1` encountered (`src/main.py:10`)",
    ]

    verified, semantic = partition_invariants(invariants, SAMPLE_DIFF, ["src/main.py"])

    assert verified == [
        "Telemetry logging configured (`src/main.py:10`)",
        "skips processing when `new_line_1` encountered (`src/main.py:10`)",
    ]
    assert semantic == [
        "handles retries when retry_count = None (`src/main.py:10`)",
        "Backward compatibility preserved across async workers.",
    ]


def test_verify_semantic_invariant_rejects_nonexistent_file() -> None:
    """An invariant citing a file that does not exist in changed_paths or repo is hallucinated."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Ensures valid transactions in `src/nonexistent/phantom_db.py:42`"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "HALLUCINATED"
    assert "phantom_db.py" in reason


def test_verify_semantic_invariant_rejects_nonexistent_test() -> None:
    """An invariant claiming a test that does not exist in diff or suite is hallucinated."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Thread safety guaranteed by test_phantom_concurrency_race_condition"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "HALLUCINATED"
    assert "test_phantom_concurrency_race_condition" in reason


def test_verify_semantic_invariant_rejects_phantom_symbol() -> None:
    """An invariant citing domain symbols that exist nowhere in diff or repo is hallucinated."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Maintains that `PhantomDistributedMutex` prevents lock contention"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "HALLUCINATED"
    assert "phantomdistributedmutex" in reason


def test_verify_semantic_invariant_accepts_grounded_invariant() -> None:
    """An invariant citing a valid changed file and diff symbol is grounded."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Ensures that `new_line_1` properly initializes logger in `src/main.py`"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "GROUNDED"
    assert "Grounded" in reason


def test_verify_semantic_invariant_demotes_unanchored_claim() -> None:
    """An invariant asserting unverified claims or residual uncertainty is demoted to unverified."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Residual uncertainty regarding whether telemetry storage scales linearly"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "UNVERIFIED"
    assert "unverified_claims" in reason


def test_filter_and_ground_semantic_invariants_triages_and_purges() -> None:
    """filter_and_ground_semantic_invariants keeps grounded, demotes unanchored, and purges hallucinations."""
    from webhook_agent.review.grounding import filter_and_ground_semantic_invariants

    invariants = [
        "Ensures that `new_line_1` properly initializes logger in `src/main.py`",
        "Ensures valid transactions in `src/nonexistent/phantom_db.py:42`",
        "Residual uncertainty regarding global cluster memory scaling",
        "Thread safety guaranteed by test_phantom_concurrency_race_condition",
        "Maintains that `PhantomDistributedMutex` prevents lock contention",
    ]

    grounded, demoted = filter_and_ground_semantic_invariants(
        invariants, SAMPLE_DIFF, ["src/main.py"]
    )

    assert grounded == [
        "Ensures that `new_line_1` properly initializes logger in `src/main.py`",
    ]
    assert demoted == [
        "Residual uncertainty regarding global cluster memory scaling",
    ]


def test_extract_json_payload_recovers_from_trailing_commas() -> None:
    """extract_json_payload must cleanly recover full review object when trailing commas are present."""
    from webhook_agent.review.formatter import extract_json_payload

    raw = """```json
    {
      "executive_summary": "Comprehensive architectural overhaul.",
      "scorecard": [
        {"dimension": "Logic & Boundaries", "status": "PASS", "notes": "Solid."},
      ],
      "verified_invariants": [
        "Invariant A",
        "Invariant B",
      ],
      "verdict": "APPROVE",
    }
    ```"""
    parsed = extract_json_payload(raw)
    assert parsed is not None
    assert parsed["executive_summary"] == "Comprehensive architectural overhaul."
    assert len(parsed["scorecard"]) == 1
    assert len(parsed["verified_invariants"]) == 2
    assert parsed["verdict"] == "APPROVE"


def test_verify_semantic_invariant_rejects_fabricated_directory_with_real_basename() -> None:
    """A fabricated path with a common basename (e.g. src/nonexistent/main.py) must be rejected."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Maintains event integrity in `src/nonexistent/main.py:15`"
    # Even though main.py exists in changed_paths, src/nonexistent/main.py does not!
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "HALLUCINATED"
    assert "src/nonexistent/main.py" in reason


def test_test_exists_in_suite_ignores_commented_definitions(tmp_path: Path) -> None:
    """_test_exists_in_suite must ignore comments like '# def test_foo' in diff text."""
    from webhook_agent.review.grounding import _test_exists_in_suite

    diff_with_comment = """
--- a/src/test_example.py
+++ b/src/test_example.py
@@ -10,3 +10,4 @@
+# def test_commented_fake(): pass
+# def test_something_else(self):
    return True
"""
    assert not _test_exists_in_suite("test_commented_fake", diff_with_comment, repo_root=tmp_path)
    assert not _test_exists_in_suite("test_something_else", diff_with_comment, repo_root=tmp_path)

    diff_with_real_def = """
--- a/src/test_example.py
+++ b/src/test_example.py
@@ -10,3 +10,4 @@
+def test_real_new_test():
+    assert True
"""
    assert _test_exists_in_suite("test_real_new_test", diff_with_real_def, repo_root=tmp_path)


def test_verify_semantic_invariant_preserves_prose_slashes() -> None:
    """Prose containing slashes (e.g. read/write, async/await) must not be purged as missing files."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    inv = "Preserves read/write ordering and handles async/await concurrency under high load"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "GROUNDED"
    assert "Grounded" in reason


def test_verify_semantic_invariant_rejects_compound_symbol_with_phantom_token() -> None:
    """A claim citing a compound symbol with a phantom token must be rejected even if one token exists."""
    from webhook_agent.review.grounding import verify_semantic_invariant

    # 'open' or 'acquire' might exist in builtins/codebase, but 'PhantomDistributedMutex' does not!
    inv = "Maintains that `PhantomDistributedMutex.acquire()` serializes callers across nodes"
    status, reason = verify_semantic_invariant(inv, SAMPLE_DIFF, ["src/main.py"])
    assert status == "HALLUCINATED"
    assert "phantomdistributedmutex" in reason.lower()


def test_symbol_exists_in_codebase_token_level_match(tmp_path: Path) -> None:
    """_symbol_exists_in_codebase must match diff identifiers as tokens, not substrings."""
    from webhook_agent.review.grounding import _symbol_exists_in_codebase

    diff_text = """
+    # Diff mentioning running workers
+    is_phantom_worker_active = True
"""
    # 'phantom_worker' is a substring of 'is_phantom_worker_active', but not a standalone token
    assert not _symbol_exists_in_codebase(
        "phantom_worker", diff_text, changed_paths=["src/main.py"], repo_root=tmp_path
    )
    assert _symbol_exists_in_codebase(
        "is_phantom_worker_active", diff_text, changed_paths=["src/main.py"], repo_root=tmp_path
    )
