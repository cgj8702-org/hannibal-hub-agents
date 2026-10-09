"""Unit tests for deterministic review-citation grounding checks.

These tests exercise :mod:`webhook_agent.review.grounding` with hand-written diffs. They
assert both the accept and the reject path, because a verifier that only ever rejects is as
useless as one that only ever accepts.
"""

from __future__ import annotations

import pytest

from webhook_agent.review.grounding import (
    Finding,
    citation_overlap,
    diff_text_from_patches,
    extract_cited_symbols,
    extract_identifiers,
    hard_reasons,
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
