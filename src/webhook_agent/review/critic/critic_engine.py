"""Critic Sub-Agent & Mechanical Gate for adversarial falsification of candidate findings.

Lifting patterns from Google's adk-recipes/long-horizon-harness and google/adk-samples:
Stage 2 of the Native 'Hunter -> Critic -> Gate' pipeline.
Acts as a skeptical Principal Engineer and 100% deterministic machine gate:
- WindowMatcher: Verifies quoted diff window lines against disk/diff with NFKC ASCII skeleton comparator.
- CheapnessGate: Flags and drops expensive multi-file or speculative attacker findings.
- AddressabilityChecker: Verifies right-side diff hunk boundaries.
- Drops findings with confidence < 8 or severity == 'NIT'.
- Records structured Google Cloud Logging events (event: finding_falsified).
"""

from __future__ import annotations

import logging
import re

from webhook_agent.review.critic.addressability import AddressabilityChecker
from webhook_agent.review.critic.cheapness_gate import CheapnessGate
from webhook_agent.review.critic.window_matcher import WindowMatcher
from webhook_agent.review.schemas import CandidateFinding, IssueItem, VerificationVerdict

logger = logging.getLogger("webhook_agent.review.critic")

MIN_VERIFIED_CONFIDENCE = 8


class CriticEngine:
    """Executes deterministic adversarial falsification against candidate review findings."""

    def __init__(
        self,
        min_confidence: int = MIN_VERIFIED_CONFIDENCE,
        diff_text: str = "",
        window_matcher: WindowMatcher | None = None,
        cheapness_gate: CheapnessGate | None = None,
        addressability_checker: AddressabilityChecker | None = None,
    ) -> None:
        self.min_confidence = min_confidence
        self.diff_text = diff_text
        self.window_matcher = window_matcher or WindowMatcher()
        self.cheapness_gate = cheapness_gate or CheapnessGate()
        self.addressability_checker = addressability_checker or AddressabilityChecker(diff_text)

    def update_diff(self, diff_text: str) -> None:
        """Update active diff context across sub-components."""
        self.diff_text = diff_text
        self.addressability_checker.update_diff(diff_text)

    def evaluate_candidate(
        self,
        candidate: CandidateFinding,
        verdict: VerificationVerdict | None = None,
        file_lines: dict[int, str] | None = None,
    ) -> bool:
        """Evaluate if a candidate finding survives the adversarial critic stage."""
        # 1. Automatic drop of NITs
        if candidate.severity == "NIT":
            logger.info(
                "🛡️ Critic dropped finding %s: NIT severity",
                candidate.id or candidate.file_path,
                extra={
                    "json_fields": {
                        "event": "finding_falsified",
                        "category": "critic_drop",
                        "reason": "NIT severity",
                        "candidate_id": candidate.id,
                        "file_path": candidate.file_path,
                        "severity": candidate.severity,
                    }
                },
            )
            return False

        # 2. Cheapness verification: drop expensive verification procedures
        steps_to_check = (
            f"{candidate.falsification_check} {getattr(candidate, 'verify_steps', '')}".strip()
        )
        is_cheap, cheap_reason = self.cheapness_gate.is_cheaply_verifiable(steps_to_check)
        if not is_cheap:
            logger.info(
                "🛡️ Critic dropped finding %s: %s",
                candidate.id or candidate.file_path,
                cheap_reason,
                extra={
                    "json_fields": {
                        "event": "finding_falsified",
                        "category": "critic_not_cheap",
                        "reason": cheap_reason,
                        "candidate_id": candidate.id,
                        "file_path": candidate.file_path,
                    }
                },
            )
            return False

        # 3. Window verification against diff or file lines if window is present
        window = getattr(candidate, "window", None)
        if window:
            lines = file_lines or self.addressability_checker.get_file_lines(candidate.file_path)
            verified_window, snapped_line, window_reason = self.window_matcher.check_window(
                window, candidate.line_number, lines
            )
            if not verified_window:
                logger.info(
                    "🛡️ Critic dropped finding %s: window mismatch (%s)",
                    candidate.id or candidate.file_path,
                    window_reason,
                    extra={
                        "json_fields": {
                            "event": "finding_falsified",
                            "category": "critic_window_mismatch",
                            "reason": window_reason,
                            "candidate_id": candidate.id,
                            "file_path": candidate.file_path,
                            "line": candidate.line_number,
                        }
                    },
                )
                return False
            # Correct line arithmetic drift if snapped
            if snapped_line != candidate.line_number:
                candidate.line_number = snapped_line

        # 4. If verdict is provided, check falsification verdict
        if verdict is not None:
            if not verdict.verified:
                logger.info(
                    "🛡️ Critic disproved finding %s: %s",
                    candidate.id,
                    verdict.disproof_reason or "Disproven by adversarial check",
                    extra={
                        "json_fields": {
                            "event": "finding_falsified",
                            "category": "critic_disproved",
                            "reason": verdict.disproof_reason or "Disproven by adversarial check",
                            "candidate_id": candidate.id,
                            "file_path": candidate.file_path,
                            "confidence": verdict.confidence_score,
                        }
                    },
                )
                return False
            if verdict.confidence_score < self.min_confidence:
                logger.info(
                    "🛡️ Critic dropped finding %s: confidence %d < threshold %d",
                    candidate.id,
                    verdict.confidence_score,
                    self.min_confidence,
                    extra={
                        "json_fields": {
                            "event": "finding_falsified",
                            "category": "critic_drop",
                            "reason": f"confidence {verdict.confidence_score} < threshold {self.min_confidence}",
                            "candidate_id": candidate.id,
                            "file_path": candidate.file_path,
                            "confidence": verdict.confidence_score,
                        }
                    },
                )
                return False
            return True

        # 5. Default confidence check if no separate verdict
        if candidate.confidence_score < self.min_confidence:
            logger.info(
                "🛡️ Critic dropped candidate %s: confidence %d < %d",
                candidate.id,
                candidate.confidence_score,
                self.min_confidence,
                extra={
                    "json_fields": {
                        "event": "finding_falsified",
                        "category": "critic_drop",
                        "reason": f"confidence {candidate.confidence_score} < threshold {self.min_confidence}",
                        "candidate_id": candidate.id,
                        "file_path": candidate.file_path,
                        "confidence": candidate.confidence_score,
                    }
                },
            )
            return False

        return True

    def filter_and_verify(
        self,
        candidates: list[CandidateFinding],
        verdicts: dict[str, VerificationVerdict] | None = None,
        file_lines_by_path: dict[str, dict[int, str]] | None = None,
    ) -> list[IssueItem]:
        """Adversarially evaluate all candidates and convert verified survivors to IssueItems."""
        verdicts_map = verdicts or {}
        file_lines_map = file_lines_by_path or {}
        survivors: list[IssueItem] = []

        for cand in candidates:
            verdict = verdicts_map.get(cand.id)
            file_lines = file_lines_map.get(cand.file_path)
            if self.evaluate_candidate(cand, verdict, file_lines=file_lines):
                # Update description or fix if revised by critic
                if verdict and verdict.revised_description:
                    cand.description = verdict.revised_description
                if verdict and verdict.revised_suggested_fix:
                    cand.suggested_fix = verdict.revised_suggested_fix
                survivors.append(cand.to_issue_item())

        logger.info(
            "Critic evaluation: %d candidate(s) evaluated -> %d verified survivor(s)",
            len(candidates),
            len(survivors),
        )
        return survivors

    def filter_issues(
        self,
        issues: list[IssueItem],
        min_confidence: int | None = None,
    ) -> list[IssueItem]:
        """Filter out low-confidence findings (< min_confidence) and NITs from IssueItems."""
        threshold = min_confidence if min_confidence is not None else self.min_confidence
        survivors: list[IssueItem] = []
        for issue in issues:
            # 1. Severity or Category check
            sev = str(getattr(issue, "severity", "") or "").strip().upper()
            cat = str(getattr(issue, "category", "") or "").strip().upper()
            if sev == "NIT" or cat == "NIT":
                logger.info(
                    "🛡️ Critic dropped IssueItem at %s:%s: NIT severity/category (%s/%s)",
                    issue.path,
                    issue.line,
                    sev,
                    cat,
                    extra={
                        "json_fields": {
                            "event": "finding_falsified",
                            "category": "critic_drop",
                            "reason": f"NIT severity/category ({sev}/{cat})",
                            "path": issue.path,
                            "line": issue.line,
                        }
                    },
                )
                continue

            # 2. Confidence score check (if score is explicitly tagged on issue)
            raw_conf = getattr(issue, "confidence_score", None)
            if raw_conf is None:
                raw_conf = getattr(issue, "confidence", None)
            if raw_conf is not None:
                try:
                    conf = int(raw_conf)
                    if conf < threshold:
                        logger.info(
                            "🛡️ Critic dropped IssueItem at %s:%s: confidence %d < threshold %d",
                            issue.path,
                            issue.line,
                            conf,
                            threshold,
                            extra={
                                "json_fields": {
                                    "event": "finding_falsified",
                                    "category": "critic_drop",
                                    "reason": f"confidence {conf} < threshold {threshold}",
                                    "path": issue.path,
                                    "line": issue.line,
                                    "confidence": conf,
                                }
                            },
                        )
                        continue
                except (ValueError, TypeError):
                    pass

            # 3. Description NIT tag check
            desc = issue.description.strip()
            if re.match(r"^(?:\[nit\]|\(nit\)|nit[:\s\-]|\bstyle nit\b)", desc, re.IGNORECASE):
                logger.info(
                    "🛡️ Critic dropped IssueItem at %s:%s: NIT indicator in description",
                    issue.path,
                    issue.line,
                    extra={
                        "json_fields": {
                            "event": "finding_falsified",
                            "category": "critic_drop",
                            "reason": "NIT indicator in description",
                            "path": issue.path,
                            "line": issue.line,
                        }
                    },
                )
                continue

            survivors.append(issue)

        if len(survivors) < len(issues):
            logger.info(
                "Critic filtered issues: %d input -> %d survivor(s) (dropped %d)",
                len(issues),
                len(survivors),
                len(issues) - len(survivors),
            )
        return survivors

    def partition_and_filter_issues(
        self,
        critical_issues: list[IssueItem],
        minor_suggestions: list[IssueItem],
    ) -> tuple[list[IssueItem], list[IssueItem]]:
        """Filter critical issues and minor suggestions with smart demotion.

        - Issues in critical_issues with confidence >= min_confidence (and not NIT) survive as critical.
        - Issues in critical_issues with moderate confidence (5 <= confidence < min_confidence)
          are demoted to minor_suggestions rather than dropped into the void.
        - Issues in minor_suggestions survive if confidence >= 5 (or untagged) and not pure cosmetic nits.
        - Style NITs and very low confidence (< 5) items are dropped.
        """
        surviving_critical: list[IssueItem] = []
        candidate_minor: list[IssueItem] = list(minor_suggestions)

        for issue in critical_issues:
            sev = str(getattr(issue, "severity", "") or "").strip().upper()
            cat = str(getattr(issue, "category", "") or "").strip().upper()
            desc = issue.description.strip()
            is_nit = (
                sev == "NIT"
                or cat == "NIT"
                or bool(
                    re.match(r"^(?:\[nit\]|\(nit\)|nit[:\s\-]|\bstyle nit\b)", desc, re.IGNORECASE)
                )
            )

            raw_conf = getattr(issue, "confidence_score", None)
            if raw_conf is None:
                raw_conf = getattr(issue, "confidence", None)
            conf: int | None = None
            if raw_conf is not None:
                try:
                    conf = int(raw_conf)
                except (ValueError, TypeError):
                    conf = None

            if is_nit:
                continue

            if conf is not None and conf < 5:
                continue

            if conf is not None and conf < self.min_confidence:
                logger.info(
                    "🛡️ Critic demoted IssueItem at %s:%s to minor_suggestions: confidence %d < threshold %d",
                    issue.path,
                    issue.line,
                    conf,
                    self.min_confidence,
                )
                candidate_minor.append(issue)
                continue

            surviving_critical.append(issue)

        surviving_minor = self.filter_issues(candidate_minor, min_confidence=5)
        return surviving_critical, surviving_minor
