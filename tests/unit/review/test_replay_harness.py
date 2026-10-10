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

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from webhook_agent.review.grounding import (
    _IDENTIFIER_RE,
    Finding,
    Kind,
    citation_overlap,
    diff_text_from_patches,
    extract_bare_symbols,
    extract_cited_paths,
    extract_cited_symbols,
    extract_identifiers,
    verify_citations,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "reviews"
FIXTURE_258 = _FIXTURES / "pr_258.json"
FIXTURE_241 = _FIXTURES / "pr_241.json"
FIXTURE_260 = _FIXTURES / "pr_260.json"

_METADATA_RE = re.compile(r"<!--\s*hannibal-review-metadata:\s*(\{.*?\})\s*-->", re.S)
_ADDED_TEST_RE = re.compile(r"^\+\s*(?:async )?def test_", re.M)
_TEST_CLAIM_RE = re.compile(r"(\d+)[^\d\n]{0,30}?tests?\b", re.I)


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
    assert len(_bot_reviews(fixture)) == 3
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


def test_review_does_not_overstate_the_test_count() -> None:
    """Every numeric test-count claim must match a recount of the diff."""
    fixture = _load(FIXTURE_258)
    actual = _added_test_count(fixture)
    for review in _bot_reviews(fixture):
        for claimed in _claimed_test_counts(review["body"] or ""):
            assert claimed == actual, (
                f"review {review['id']} claims {claimed} tests; diff adds {actual}"
            )


def test_wrong_line_inside_a_changed_hunk_is_a_known_blind_spot() -> None:
    """Documented blind spot: a right-file, wrong-line citation passes every check.

    Live evidence from PR #260's review 5464909992, which cites
    ``tools/github_tools.py:300`` for the claim that ``_grounding_findings`` iterates the
    finding categories. Line 300 is a **blank line** inside the added hunk (the function
    starts at 310), so every path/line check passes it. ``citation_overlap`` scores 0.0
    against an empty cited line -- which is why it is a triage signal, never a gate.

    The durable fix is requiring the reviewer to quote the source line it cites (Phase 3);
    do not assume this is covered today.
    """
    fixture = _load(FIXTURE_260)
    diff_text = _diff_text(fixture)
    claim = Finding(
        kind="invariant",
        path="src/webhook_agent/tools/github_tools.py",
        line=300,
        evidence="def _grounding_findings(review_obj) iterates over all finding categories",
    )
    issues = verify_citations([claim], diff_text, fixture["changed_files"])
    assert issues == [], "if this ever fails, a check got stricter -- update this docstring"
    overlaps = citation_overlap([claim], diff_text)
    assert overlaps and overlaps[0].overlap == 0.0
    assert overlaps[0].cited_text == "", "the cited line should be blank for this demo"


def test_cross_pr_reversal_was_withdrawn_before_merge() -> None:
    """#241's review approved removing ``merge_pr``; #258's final diff does not re-add it.

    The reversal this fixture originally captured was withdrawn before merge (see the
    scope-reduction commit on #258), so the assertion now pins the withdrawn state: the
    tool is absent from the merged diff while the bot's reviews still discuss it.
    """
    p241, p258 = _load(FIXTURE_241), _load(FIXTURE_258)
    bodies_241 = [r["body"] or "" for r in _bot_reviews(p241)]
    assert any("merge_pr" in b for b in bodies_241), "#241 review never names merge_pr"
    assert any("APPROVE" in b for b in bodies_241), "no APPROVE verdict recorded for #241"
    assert not any("+def merge_pr(" in (patch or "") for patch in p258["patches"].values()), (
        "#258's final diff re-adds merge_pr -- the scope reduction was reverted"
    )
    assert any("merge_pr" in (r["body"] or "") for r in _bot_reviews(p258)), (
        "#258's reviews no longer discuss merge_pr; the reversal evidence moved"
    )


# ---------------------------------------------------------------------------
# Resolution provenance: a review may not claim to have resolved something that
# the diff it was reviewing did not change.
# ---------------------------------------------------------------------------

_BOT = "hannibal-hub-agents[bot]"
_ALL_FIXTURES = (("258", FIXTURE_258), ("241", FIXTURE_241), ("260", FIXTURE_260))


def _meta_body(resolutions: list[dict[str, Any]]) -> str:
    """Build a review body carrying a machine-readable metadata footer."""
    meta = {
        "version": 1,
        "type": "sync",
        "verdict": "APPROVE",
        "resolutions": resolutions,
        "critical_issues": [],
        "minor_suggestions": [],
    }
    return f"<!-- hannibal-review-metadata: {json.dumps(meta)} -->"


def _resolution_findings(reviews: list[dict[str, Any]]) -> list[tuple[str, str, Any]]:
    """Return ``(severity, code, review_id)`` for every resolution-provenance problem.

    R0 hard  same_commit_as_previous_review    -- nothing changed, so nothing was resolved
    R1 hard  resolved_without_evidence_in_diff -- the symbols it names are not in this diff
    R1 hard  resolved_file_not_in_diff         -- the file it names is not in this diff
    R2 soft  resolution_names_no_symbol        -- unverifiable evidence; log, never reject

    R1 checks three signals, strongest first: backticked symbols (explicit
    citation), file paths (``github_tools.py`` or ``src/foo.py``), then bare
    code-like identifiers (snake_case/camelCase without backticks). Plain
    English prose matches none of them and stays soft R2 -- prose cannot
    manufacture a pass, but it is never punished for being prose.

    A fourth candidate rule was dropped during implementation: "the incremental diff
    touches only test files" is redundant with R1 in practice, because a test-only diff
    contains no production symbols, so R1 already fires on it.
    """
    findings: list[tuple[str, str, Any]] = []
    previous_commit: str | None = None
    for review in reviews:
        if not str(review.get("login") or "").startswith("hannibal"):
            continue
        commit = review.get("commit_id")
        tokens = set(review.get("incremental_tokens") or [])
        files = {f.lower() for f in (review.get("incremental_files") or [])}
        file_basenames = {f.rsplit("/", 1)[-1] for f in files}
        for item in _metadata(review.get("body") or "").get("resolutions") or []:
            if item.get("status") != "RESOLVED":
                continue
            text = f"{item.get('item_description', '')} {item.get('evidence', '')}"
            paths = extract_cited_paths(text)
            path_tokens = {
                token
                for path in paths
                for token in _IDENTIFIER_RE.findall(path.replace("/", " ").replace(".", " "))
            }
            path_tokens = {t.lower() for t in path_tokens}
            symbols = (extract_cited_symbols(text) | extract_bare_symbols(text)) - path_tokens
            if commit and previous_commit and commit == previous_commit:
                findings.append(("hard", "same_commit_as_previous_review", review.get("id")))
            if not symbols and not paths:
                findings.append(("soft", "resolution_names_no_symbol", review.get("id")))
                continue
            if symbols and all(symbol not in tokens for symbol in symbols):
                findings.append(("hard", "resolved_without_evidence_in_diff", review.get("id")))
            if paths and not any(
                p in files or p in file_basenames or any(f.endswith(f"/{p}") for f in files)
                for p in paths
            ):
                findings.append(("hard", "resolved_file_not_in_diff", review.get("id")))
        previous_commit = commit or previous_commit
    return findings


def _hard_codes(findings: list[tuple[str, str, Any]]) -> list[str]:
    """Return only the codes of hard findings."""
    return [code for severity, code, _ in findings if severity == "hard"]


def test_resolution_rule_flags_same_commit_as_previous_review() -> None:
    """R0: if the commit did not change between reviews, nothing was resolved."""
    first = {"login": _BOT, "id": 1, "commit_id": "aaa", "incremental_tokens": [], "body": ""}
    second = {
        "login": _BOT,
        "id": 2,
        "commit_id": "aaa",
        "incremental_tokens": ["foo"],
        "body": _meta_body(
            [{"item_description": "`foo` is slow", "status": "RESOLVED", "evidence": "fixed"}]
        ),
    }
    assert _hard_codes(_resolution_findings([first, second])) == ["same_commit_as_previous_review"]


def test_resolution_rule_flags_symbol_missing_from_the_diff() -> None:
    """R1: a RESOLVED item whose named symbol the diff never touched is unfounded."""
    review = {
        "login": _BOT,
        "id": 7,
        "commit_id": "bbb",
        "incremental_tokens": ["unrelated"],
        "body": _meta_body(
            [
                {
                    "item_description": "`_collect_pr_patches` adds an API call",
                    "status": "RESOLVED",
                    "evidence": "`_collect_pr_patches` now reuses cached patches",
                }
            ]
        ),
    }
    assert _hard_codes(_resolution_findings([review])) == ["resolved_without_evidence_in_diff"]


def test_resolution_rule_passes_when_the_diff_changed_the_named_symbol() -> None:
    """Negative case: a genuinely resolved item must not be flagged."""
    review = {
        "login": _BOT,
        "id": 8,
        "commit_id": "ccc",
        "incremental_tokens": ["merge_pr", "removed"],
        "body": _meta_body(
            [
                {
                    "item_description": "`merge_pr` lacks an authorization gate",
                    "status": "RESOLVED",
                    "evidence": "`merge_pr` was removed from the agent toolchain",
                }
            ]
        ),
    }
    assert _hard_codes(_resolution_findings([review])) == []


def test_resolution_rule_treats_symbol_less_evidence_as_advisory() -> None:
    """R2: evidence naming no symbol is unverifiable, not a rejection."""
    review = {
        "login": _BOT,
        "id": 9,
        "commit_id": "ddd",
        "incremental_tokens": [],
        "body": _meta_body(
            [
                {
                    "item_description": "latency concern",
                    "status": "RESOLVED",
                    "evidence": "now handled properly",
                }
            ]
        ),
    }
    findings = _resolution_findings([review])
    assert _hard_codes(findings) == []
    assert findings == [("soft", "resolution_names_no_symbol", 9)]


def test_resolution_rule_flags_bare_snake_case_symbol_missing_from_the_diff() -> None:
    """R1 without backticks: _collect_pr_patches named bare is still checkable."""
    review = {
        "login": _BOT,
        "id": 10,
        "commit_id": "eee",
        "incremental_tokens": ["unrelated"],
        "body": _meta_body(
            [
                {
                    "item_description": "_collect_pr_patches adds an API call",
                    "status": "RESOLVED",
                    "evidence": "_collect_pr_patches now reuses cached patches",
                }
            ]
        ),
    }
    assert _hard_codes(_resolution_findings([review])) == ["resolved_without_evidence_in_diff"]


def test_resolution_rule_passes_when_bare_symbol_is_in_the_diff() -> None:
    """Bare-symbol negative case: the diff really did touch the named symbol."""
    review = {
        "login": _BOT,
        "id": 11,
        "commit_id": "fff",
        "incremental_tokens": ["_collect_pr_patches", "cached"],
        "body": _meta_body(
            [
                {
                    "item_description": "_collect_pr_patches adds an API call",
                    "status": "RESOLVED",
                    "evidence": "_collect_pr_patches now reuses cached patches",
                }
            ]
        ),
    }
    assert _hard_codes(_resolution_findings([review])) == []


def test_resolution_rule_prose_without_code_like_tokens_stays_advisory() -> None:
    """R2, not R1: 'the patch collection fallback' names no code, so no rejection."""
    review = {
        "login": _BOT,
        "id": 12,
        "commit_id": "ggg",
        "incremental_tokens": [],
        "body": _meta_body(
            [
                {
                    "item_description": "potential latency overhead",
                    "status": "RESOLVED",
                    "evidence": "the patch collection fallback now reuses cached data",
                }
            ]
        ),
    }
    findings = _resolution_findings([review])
    assert _hard_codes(findings) == []
    assert findings == [("soft", "resolution_names_no_symbol", 12)]


def test_resolution_rule_flags_file_missing_from_the_diff() -> None:
    """R1 file signal: 'fixed in github_tools.py' while the diff touched tests only."""
    review = {
        "login": _BOT,
        "id": 13,
        "commit_id": "hhh",
        "incremental_tokens": ["test", "context"],
        "incremental_files": ["tests/unit/review/test_review_tool_validation.py"],
        "body": _meta_body(
            [
                {
                    "item_description": "latency overhead in the fallback",
                    "status": "RESOLVED",
                    "evidence": "fixed in github_tools.py",
                }
            ]
        ),
    }
    assert _hard_codes(_resolution_findings([review])) == ["resolved_file_not_in_diff"]


def test_resolution_rule_passes_when_named_file_is_in_the_diff() -> None:
    """File-signal negative case: basename match against the incremental files."""
    review = {
        "login": _BOT,
        "id": 14,
        "commit_id": "iii",
        "incremental_tokens": ["patch"],
        "incremental_files": ["src/webhook_agent/tools/github_tools.py"],
        "body": _meta_body(
            [
                {
                    "item_description": "latency overhead in the fallback",
                    "status": "RESOLVED",
                    "evidence": "fixed in github_tools.py",
                }
            ]
        ),
    }
    assert _hard_codes(_resolution_findings([review])) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PR #260 reviews 5464906355 and 5464909992 both mark the `_collect_pr_patches` "
        "API-call risk RESOLVED while the incremental diffs they reviewed (a blank line; "
        "a test-context fix) contain none of the symbols their evidence names."
    ),
)
def test_recorded_reviews_do_not_claim_unfounded_resolutions() -> None:
    """No recorded review may mark something RESOLVED that its diff did not change."""
    offenders = {}
    for name, path in _ALL_FIXTURES:
        codes = _hard_codes(_resolution_findings(_load(path)["reviews"]))
        if codes:
            offenders[name] = codes
    assert offenders == {}, f"unfounded RESOLVED claims: {offenders}"


def test_fixture_records_review_commits_and_incremental_diffs() -> None:
    """Vacuity guard: the recorder must supply the data this rule depends on."""
    for name, path in _ALL_FIXTURES:
        for review in _bot_reviews(_load(path)):
            assert review.get("commit_id"), f"#{name} review {review.get('id')} has no commit_id"
            assert "incremental_tokens" in review, f"#{name} review missing incremental_tokens"
    assert any(review.get("incremental_tokens") for review in _bot_reviews(_load(FIXTURE_260))), (
        "no incremental tokens recorded anywhere -- the rule would pass vacuously"
    )


def test_recorder_tokenizer_matches_grounding_extractor() -> None:
    """The recorder duplicates the tokenizer to stay stdlib-only; pin them together."""
    path = Path(__file__).resolve().parents[3] / "dev" / "record_review_fixtures.py"
    spec = importlib.util.spec_from_file_location("record_review_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sample = "`_collect_pr_patches()` and pr.get_files() and STRICT_REVIEW_GROUNDING"
    assert module._identifier_tokens(sample) == sorted(extract_identifiers(sample))
