"""Offline replay harness over recorded bot reviews (Phase 1 of the hardening plan).

Reads fixtures under ``tests/fixtures/reviews/``; regenerate them with
``dev/record_review_fixtures.py``. No network and no model calls: recorded artifacts in,
deterministic assertions out.

Three rules govern this file:

1. **Every extractor must be proven non-vacuous.** A regex that matches nothing returns an
   empty list, and an empty list looks exactly like a clean review. Two extractors in this
   harness were vacuous on first draft (a ``^``-anchored regex missing ``re.M``, and a
   numeric pattern that could not see an adjective between the number and the noun). Both
   are now guarded by :func:`test_extractors_are_not_vacuous`.
2. **Assertions encoding a known defect use ``xfail(strict=True)``.** Fixing the defect
   turns the test green, and the suite then reminds us to drop the marker.
3. **Assertions encoding a known blind spot pass today and say so in their docstring**, so
   that nobody later mistakes a blind spot for coverage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from webhook_agent.review.grounding import (
    Finding,
    Kind,
    citation_overlap,
    diff_text_from_patches,
    verify_citations,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "reviews"
FIXTURE_258 = _FIXTURES / "pr_258.json"
FIXTURE_241 = _FIXTURES / "pr_241.json"

_METADATA_RE = re.compile(r"<!--\s*hannibal-review-metadata:\s*(\{.*?\})\s*-->", re.S)
_ADDED_TEST_RE = re.compile(r"^\+\s*(?:async )?def test_", re.M)
_TEST_CLAIM_RE = re.compile(r"(\d+)[^\d\n]{0,30}?tests?\b", re.I)
_BODY_INVARIANTS_SECTION = "Verified Invariants & Edge Cases"


def _load(path: Path) -> dict[str, Any]:
    """Load a recorded fixture."""
    return json.loads(path.read_text(encoding="utf-8"))


def _bot_reviews(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the reviews authored by this bot."""
    return [r for r in fixture["reviews"] if str(r.get("login") or "").startswith("hannibal")]


def _metadata(body: str) -> dict[str, Any]:
    """Parse the machine-readable review footer, or {} when absent."""
    match = _METADATA_RE.search(body or "")
    return json.loads(match.group(1)) if match else {}


def _diff_text(fixture: dict[str, Any]) -> str:
    """Reconstruct the review's diff from its recorded per-file patches."""
    return diff_text_from_patches(fixture["patches"])


def _findings(metadata: dict[str, Any]) -> list[Finding]:
    """Extract cited findings from a review's machine-readable footer."""
    findings: list[Finding] = []
    kind_by_key: dict[str, Kind] = {
        "critical_issues": "critical",
        "minor_suggestions": "suggestion",
        "verified_invariants": "invariant",
    }
    for key, kind in kind_by_key.items():
        for item in metadata.get(key) or []:
            text = item.get("description") or item.get("invariant") or ""
            findings.append(
                Finding(
                    kind=kind,
                    path=item.get("path") or "",
                    line=item.get("line"),
                    evidence=item.get("evidence") or text,
                )
            )
    return findings


def _added_test_count(fixture: dict[str, Any]) -> int:
    """Count test functions the diff adds, across every recorded patch."""
    return sum(len(_ADDED_TEST_RE.findall(p)) for p in fixture["patches"].values() if p)


def _claimed_test_counts(body: str) -> list[int]:
    """Extract every '<n> ... tests' claim from a review body."""
    return [int(n) for n in _TEST_CLAIM_RE.findall(body or "")]


def test_fixtures_are_recorded() -> None:
    """Structural guard: the fixtures must contain real evidence, not empty shells.

    Runs before every other assertion's expectations are trusted. If someone re-records a
    fixture with ``--patches none`` for PR #258, this fails loudly instead of silently
    turning the citation checks into no-ops.
    """
    fixture = _load(FIXTURE_258)
    assert fixture["patches"], "PR #258 fixture needs --patches full"
    assert len(fixture["changed_files"]) > 10
    assert len(_bot_reviews(fixture)) == 2
    parsed = [_metadata(r["body"] or "") for r in _bot_reviews(fixture)]
    assert all(parsed), "every recorded bot review should carry a metadata footer"


def test_extractors_are_not_vacuous() -> None:
    """Both content extractors must find something in the real fixture.

    This is the guard against the worst kind of green test: one whose regex matches
    nothing, so its assertion trivially holds.
    """
    fixture = _load(FIXTURE_258)
    bodies = [(r["body"] or "") for r in _bot_reviews(fixture)]
    assert _added_test_count(fixture) > 0, "added-test regex matched nothing"
    assert any(_claimed_test_counts(b) for b in bodies), "numeric-claim regex matched nothing"


def test_every_citation_points_at_a_line_the_diff_displays() -> None:
    """Regression guard: no finding may cite a line the diff does not show.

    Passes on the recorded #258 reviews. It exists so that a future reviewer citing from
    memory trips a test instead of producing a plausible-looking review.
    """
    fixture = _load(FIXTURE_258)
    diff_text = _diff_text(fixture)
    for review in _bot_reviews(fixture):
        findings = _findings(_metadata(review["body"] or ""))
        issues = verify_citations(findings, diff_text, fixture["changed_files"])
        unseen = [i for i in issues if i.code == "line_outside_diff"]
        assert unseen == [], f"review {review['id']} cites lines the diff never shows: {unseen}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PR #258 review 1 claims '31 robust unit tests'; the diff adds 23 "
        "(19 in the three files it names). Fixed by grounding the claim or dropping it."
    ),
)
def test_review_does_not_overstate_the_test_count() -> None:
    """Every numeric test-count claim must match a recount of the diff."""
    fixture = _load(FIXTURE_258)
    actual = _added_test_count(fixture)
    for review in _bot_reviews(fixture):
        for claimed in _claimed_test_counts(review["body"] or ""):
            assert claimed == actual, (
                f"review {review['id']} claims {claimed} tests; diff adds {actual}"
            )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "verified_invariants are rendered in the review body but never serialized into "
        "the hannibal-review-metadata footer (both to_markdown metadata blocks in "
        "review/schemas.py omit the key), so the verdict's evidence is unreadable to any "
        "downstream consumer, including the resolution tracker."
    ),
)
def test_body_and_metadata_agree_about_verified_invariants() -> None:
    """Anything rendered as verified invariants must survive into the durable record."""
    fixture = _load(FIXTURE_258)
    for review in _bot_reviews(fixture):
        body = review["body"] or ""
        if _BODY_INVARIANTS_SECTION not in body:
            continue
        assert _metadata(body).get("verified_invariants"), (
            f"review {review['id']} renders verified invariants but drops them from metadata"
        )


def test_wrong_line_inside_a_changed_hunk_is_a_known_blind_spot() -> None:
    """Documented blind spot: a right-file, wrong-line citation passes every check.

    Recorded evidence: review 2 cites ``tools/github_tools.py:556`` for a claim about the
    draft/mergeable gate, which lives at lines 550-553. Line 556 is ``if status.merged:``
    -- it exists and is part of this diff, so ``path``/``line`` checks cannot see the
    error. Measured claim/cited-line token overlap is ~0.2, which is why
    ``citation_overlap`` is advisory only. The real fix is requiring a quoted source line
    (see the Phase 3 notes in the plan) -- do not assume this is covered today.
    """
    fixture = _load(FIXTURE_258)
    diff_text = _diff_text(fixture)
    claim = Finding(
        kind="invariant",
        path="src/webhook_agent/tools/github_tools.py",
        line=556,
        evidence=(
            "Explicitly verifies pr.draft and pr.mergeable status prior to invoking pr.merge()"
        ),
    )
    issues = verify_citations([claim], diff_text, fixture["changed_files"])
    assert issues == [], "if this ever fails, a check got stricter -- update this docstring"
    overlaps = citation_overlap([claim], diff_text)
    assert overlaps and overlaps[0].overlap < 0.5, "overlap should be a weak signal here"
    assert "status.merged" in overlaps[0].cited_text


def test_cross_pr_reversal_is_detectable_from_the_fixtures() -> None:
    """The #241 -> #258 reversal is a fact pattern in the fixtures, not an opinion.

    #241 removed ``merge_pr``; this bot approved it and described the removal as enforcing
    the Zero-Bypass paradigm. #258 re-adds ``merge_pr`` and the same bot approved it as
    "legitimate". Phase 4 tracks whether a later review *acknowledges* such a reversal;
    acknowledgement is deliberately not asserted here, because the fixtures capture the
    pre-fix state.
    """
    p241, p258 = _load(FIXTURE_241), _load(FIXTURE_258)
    bodies = [r["body"] or "" for r in _bot_reviews(p241)]
    assert any("merge_pr" in b for b in bodies), "#241 review never names merge_pr"
    assert any("APPROVE" in b for b in bodies), "no APPROVE verdict recorded for #241"
    assert any("+def merge_pr(" in (patch or "") for patch in p258["patches"].values()), (
        "#258 diff does not re-add merge_pr"
    )
