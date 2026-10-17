"""Review Replay Engine for historical baseline analysis and offline validation.

Provides structured replay of recorded PR review fixtures against their
corresponding diffs and incremental changes, producing typed evaluation reports
and statistical metrics.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from webhook_agent.review.grounding import (
    _IDENTIFIER_RE,
    CitationOverlap,
    Finding,
    GroundingIssue,
    Kind,
    check_test_count_claims,
    citation_overlap,
    diff_text_from_patches,
    extract_bare_symbols,
    extract_cited_paths,
    extract_cited_symbols,
    verify_citations,
)
from webhook_agent.review.metadata import extract_review_metadata

logger = logging.getLogger(__name__)

_BOT_LOGIN_PREFIX = "hannibal"


@dataclass(frozen=True, slots=True)
class ReviewEvaluation:
    """Evaluation result for a single recorded review."""

    review_id: Any
    login: str
    commit_id: str | None
    total_claims: int
    grounding_issues: list[GroundingIssue] = field(default_factory=list)
    overlaps: list[CitationOverlap] = field(default_factory=list)
    resolution_issues: list[tuple[str, str, Any]] = field(default_factory=list)
    test_count_issues: list[GroundingIssue] = field(default_factory=list)

    @property
    def hard_issues(self) -> list[GroundingIssue]:
        """Return hard grounding issues that represent provable hallucinations."""
        return [i for i in self.grounding_issues if i.is_hard]

    @property
    def soft_issues(self) -> list[GroundingIssue]:
        """Return soft grounding issues representing advisory or unverified citations."""
        return [i for i in self.grounding_issues if not i.is_hard]

    @property
    def hard_resolution_issues(self) -> list[tuple[str, str, Any]]:
        """Return resolution issues marked as hard severity."""
        return [r for r in self.resolution_issues if r[0] == "hard"]

    @property
    def is_grounded(self) -> bool:
        """Return True if the review contains zero hard grounding or resolution issues."""
        return len(self.hard_issues) == 0 and len(self.hard_resolution_issues) == 0

    @property
    def mean_overlap(self) -> float:
        """Average citation text overlap against diff lines (0.0 to 1.0)."""
        if not self.overlaps:
            return 0.0
        return sum(o.overlap for o in self.overlaps) / len(self.overlaps)


@dataclass(frozen=True, slots=True)
class FixtureEvaluation:
    """Evaluation report for all bot reviews in a pull request fixture."""

    pr_number: int
    repo: str
    title: str
    changed_files: list[str]
    reviews: list[ReviewEvaluation] = field(default_factory=list)

    @property
    def total_reviews(self) -> int:
        """Total number of evaluated bot reviews."""
        return len(self.reviews)

    @property
    def total_claims(self) -> int:
        """Total number of claims/findings evaluated across all reviews."""
        return sum(r.total_claims for r in self.reviews)

    @property
    def hard_issues_count(self) -> int:
        """Total count of hard citation issues."""
        return sum(len(r.hard_issues) for r in self.reviews)

    @property
    def soft_issues_count(self) -> int:
        """Total count of soft citation issues."""
        return sum(len(r.soft_issues) for r in self.reviews)

    @property
    def hard_resolutions_count(self) -> int:
        """Total count of hard resolution issues."""
        return sum(len(r.hard_resolution_issues) for r in self.reviews)

    @property
    def grounding_accuracy(self) -> float:
        """Percentage of claims that are completely free of hard issues."""
        if self.total_claims == 0:
            return 100.0
        valid_claims = max(0, self.total_claims - self.hard_issues_count)
        return (valid_claims / self.total_claims) * 100.0

    @property
    def is_clean(self) -> bool:
        """Return True if all reviews in this fixture pass without hard issues."""
        return all(r.is_grounded for r in self.reviews)


@dataclass(frozen=True, slots=True)
class BaselineReport:
    """Aggregated baseline report over multiple pull request fixtures."""

    fixtures: list[FixtureEvaluation] = field(default_factory=list)

    @property
    def total_prs(self) -> int:
        """Number of distinct PRs evaluated."""
        return len(self.fixtures)

    @property
    def total_reviews(self) -> int:
        """Total reviews evaluated."""
        return sum(f.total_reviews for f in self.fixtures)

    @property
    def total_claims(self) -> int:
        """Total claims evaluated."""
        return sum(f.total_claims for f in self.fixtures)

    @property
    def total_hard_issues(self) -> int:
        """Total hard citation failures across all fixtures."""
        return sum(f.hard_issues_count for f in self.fixtures)

    @property
    def total_soft_issues(self) -> int:
        """Total soft citation issues across all fixtures."""
        return sum(f.soft_issues_count for f in self.fixtures)

    @property
    def total_hard_resolutions(self) -> int:
        """Total hard resolution provenance failures."""
        return sum(f.hard_resolutions_count for f in self.fixtures)

    @property
    def overall_accuracy(self) -> float:
        """Overall provenance accuracy percentage across all evaluated claims."""
        if self.total_claims == 0:
            return 100.0
        valid = max(0, self.total_claims - self.total_hard_issues)
        return (valid / self.total_claims) * 100.0

    @property
    def hallucination_rate(self) -> float:
        """Overall citation hallucination rate percentage (100 - accuracy)."""
        return 100.0 - self.overall_accuracy

    @property
    def hard_issues_by_code(self) -> dict[str, int]:
        """Breakdown of hard issues by issue code."""
        counter: Counter[str] = Counter()
        for f in self.fixtures:
            for r in f.reviews:
                for issue in r.hard_issues:
                    counter[issue.code] += 1
        return dict(counter)

    @property
    def soft_issues_by_code(self) -> dict[str, int]:
        """Breakdown of soft issues by issue code."""
        counter: Counter[str] = Counter()
        for f in self.fixtures:
            for r in f.reviews:
                for issue in r.soft_issues:
                    counter[issue.code] += 1
        return dict(counter)


_INVARIANT_CITATION_RE = re.compile(
    r"(?:^|[\s(])`?([a-zA-Z0-9_\-./]+\.[a-zA-Z0-9_\-]+):(\d+)(?:[-,]\d+)*`?"
)


def extract_findings_from_metadata(metadata: Mapping[str, Any]) -> list[Finding]:
    """Extract cited findings from a review's structured metadata."""
    findings: list[Finding] = []
    kind_by_key: dict[str, Kind] = {
        "critical_issues": "critical",
        "minor_suggestions": "suggestion",
        "verified_invariants": "invariant",
    }
    for key, kind in kind_by_key.items():
        for item in metadata.get(key) or []:
            if isinstance(item, dict):
                text = item.get("description") or item.get("invariant") or ""
                findings.append(
                    Finding(
                        kind=kind,
                        path=str(item.get("path") or ""),
                        line=item.get("line"),
                        evidence=str(item.get("evidence") or text),
                    )
                )
            elif isinstance(item, str) and kind == "invariant":
                match = _INVARIANT_CITATION_RE.search(item)
                if match:
                    findings.append(
                        Finding(
                            kind="invariant",
                            path=match.group(1),
                            line=int(match.group(2)),
                            evidence=item,
                        )
                    )
    return findings


def extract_findings_from_body(body: str) -> list[Finding]:
    """Extract findings from review body (using metadata footer or fallback)."""
    meta = extract_review_metadata(body)
    if meta:
        return extract_findings_from_metadata(meta)
    return []


def evaluate_resolution_sequence(
    reviews: Sequence[Mapping[str, Any]],
) -> dict[Any, list[tuple[str, str, Any]]]:
    """Evaluate resolution provenance across an ordered sequence of reviews.

    Returns mapping of review_id -> list of (severity, code, review_id).
    """
    findings_by_review: dict[Any, list[tuple[str, str, Any]]] = {}
    previous_commit: str | None = None

    for review in reviews:
        rev_id = review.get("id")
        findings_by_review[rev_id] = []
        if not str(review.get("login") or "").startswith(_BOT_LOGIN_PREFIX):
            continue

        commit = review.get("commit_id")
        tokens = set(review.get("incremental_tokens") or [])
        files = {str(f).lower() for f in (review.get("incremental_files") or [])}
        file_basenames = {f.rsplit("/", 1)[-1] for f in files}

        body = str(review.get("body") or "")
        meta = extract_review_metadata(body) or {}
        for item in meta.get("resolutions") or []:
            if not isinstance(item, dict) or item.get("status") != "RESOLVED":
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
                findings_by_review[rev_id].append(
                    ("hard", "same_commit_as_previous_review", rev_id)
                )
            if not symbols and not paths:
                findings_by_review[rev_id].append(("soft", "resolution_names_no_symbol", rev_id))
                continue
            if symbols and all(symbol not in tokens for symbol in symbols):
                findings_by_review[rev_id].append(
                    ("hard", "resolved_without_evidence_in_diff", rev_id)
                )
            if paths and not any(
                p in files or p in file_basenames or any(f.endswith(f"/{p}") for f in files)
                for p in paths
            ):
                findings_by_review[rev_id].append(("hard", "resolved_file_not_in_diff", rev_id))

        previous_commit = commit or previous_commit

    return findings_by_review


def evaluate_review(
    review: Mapping[str, Any],
    diff_text: str,
    changed_files: Sequence[str],
    resolution_issues: Sequence[tuple[str, str, Any]] | None = None,
) -> ReviewEvaluation:
    """Evaluate a single review against diff text and changed file list."""
    body = str(review.get("body") or "")
    findings = extract_findings_from_body(body)
    grounding_issues = verify_citations(findings, diff_text, changed_paths=changed_files)
    overlaps = citation_overlap(findings, diff_text)
    test_count_issues = check_test_count_claims(body, diff_text)

    return ReviewEvaluation(
        review_id=review.get("id"),
        login=str(review.get("login") or ""),
        commit_id=review.get("commit_id"),
        total_claims=len(findings),
        grounding_issues=grounding_issues,
        overlaps=overlaps,
        resolution_issues=list(resolution_issues or []),
        test_count_issues=test_count_issues,
    )


def evaluate_fixture(fixture: Mapping[str, Any]) -> FixtureEvaluation:
    """Evaluate an entire recorded pull request fixture."""
    pr_number = int(fixture.get("number") or 0)
    repo = str(fixture.get("repo") or "")
    title = str(fixture.get("title") or "")
    changed_files = [str(f) for f in (fixture.get("changed_files") or [])]
    raw_patches = fixture.get("patches") or {}
    diff_text = diff_text_from_patches(raw_patches)

    raw_reviews = [
        r
        for r in (fixture.get("reviews") or [])
        if str(r.get("login") or "").startswith(_BOT_LOGIN_PREFIX)
    ]
    res_findings_map = evaluate_resolution_sequence(raw_reviews)

    evaluations: list[ReviewEvaluation] = []
    for r in raw_reviews:
        r_id = r.get("id")
        evaluations.append(
            evaluate_review(
                r,
                diff_text,
                changed_files,
                resolution_issues=res_findings_map.get(r_id, []),
            )
        )

    return FixtureEvaluation(
        pr_number=pr_number,
        repo=repo,
        title=title,
        changed_files=changed_files,
        reviews=evaluations,
    )


def evaluate_fixtures(fixtures: Sequence[Mapping[str, Any]]) -> BaselineReport:
    """Evaluate multiple recorded fixtures and return an aggregated BaselineReport."""
    results = [evaluate_fixture(f) for f in fixtures]
    return BaselineReport(fixtures=results)


def load_fixture_file(path: str | Path) -> dict[str, Any]:
    """Read and parse a JSON fixture file."""
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def evaluate_fixture_dir(directory: str | Path) -> BaselineReport:
    """Discover all pr_*.json fixtures in a directory and evaluate them."""
    p = Path(directory)
    fixture_files = sorted(p.glob("pr_*.json"))
    fixtures = [load_fixture_file(f) for f in fixture_files]
    return evaluate_fixtures(fixtures)


def format_baseline_table(report: BaselineReport) -> str:
    """Format a BaselineReport into a clean CLI ASCII scorecard table."""
    lines: list[str] = []
    lines.append("=" * 86)
    lines.append("       HANNIBAL HUB GROUNDING & BASELINE PROVENANCE EVALUATION REPORT")
    lines.append("=" * 86)
    lines.append(
        f"{'PR #':<8} {'Reviews':<9} {'Claims':<8} {'Hard Issues':<14} {'Soft Issues':<14} {'Accuracy':<10} {'Status':<10}"
    )
    lines.append("-" * 86)

    for f in report.fixtures:
        status = "✅ PASS" if f.is_clean else "⚠️ WARN"
        hard_str = f"{f.hard_issues_count}"
        soft_str = f"{f.soft_issues_count}"
        acc_str = f"{f.grounding_accuracy:.1f}%"
        lines.append(
            f"#{f.pr_number:<7} {f.total_reviews:<9} {f.total_claims:<8} {hard_str:<14} {soft_str:<14} {acc_str:<10} {status:<10}"
        )

    lines.append("-" * 86)
    lines.append(
        f"TOTALS: {report.total_prs} PR(s) | {report.total_reviews} Review(s) | {report.total_claims} Total Claims"
    )
    lines.append(
        f"        Overall Grounded Accuracy: {report.overall_accuracy:.1f}% | Hallucination Rate: {report.hallucination_rate:.1f}%"
    )
    lines.append(
        f"        Hard Failures: {report.total_hard_issues} | Soft Warnings: {report.total_soft_issues} | Hard Resolutions: {report.total_hard_resolutions}"
    )

    if report.hard_issues_by_code:
        lines.append("")
        lines.append("Hard Issue Breakdown (Provable Inaccuracies):")
        for code, count in sorted(report.hard_issues_by_code.items(), key=lambda x: -x[1]):
            lines.append(f"  - {code}: {count}")

    if report.soft_issues_by_code:
        lines.append("")
        lines.append("Soft Issue Breakdown (Unverifiable / Context References):")
        for code, count in sorted(report.soft_issues_by_code.items(), key=lambda x: -x[1]):
            lines.append(f"  - {code}: {count}")

    lines.append("=" * 86)
    return "\n".join(lines)


def format_baseline_markdown(report: BaselineReport) -> str:
    """Format a BaselineReport into GitHub-flavored markdown."""
    lines: list[str] = [
        "## 📊 Grounding & Baseline Provenance Report",
        "",
        "| PR | Reviews | Claims | Hard Issues | Soft Issues | Grounding Accuracy | Status |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for f in report.fixtures:
        status = "`PASS`" if f.is_clean else "`WARN`"
        lines.append(
            f"| #{f.pr_number} | {f.total_reviews} | {f.total_claims} | {f.hard_issues_count} | {f.soft_issues_count} | {f.grounding_accuracy:.1f}% | {status} |"
        )
    lines.extend(
        [
            "",
            "### Summary Metrics",
            f"- **Evaluated PRs:** {report.total_prs}",
            f"- **Total Reviews:** {report.total_reviews}",
            f"- **Total Claims Checked:** {report.total_claims}",
            f"- **Overall Grounding Accuracy:** `{report.overall_accuracy:.1f}%`",
            f"- **Hallucination Rate:** `{report.hallucination_rate:.1f}%`",
            f"- **Hard Failures:** {report.total_hard_issues}",
            f"- **Soft Warnings:** {report.total_soft_issues}",
        ]
    )
    return "\n".join(lines)
