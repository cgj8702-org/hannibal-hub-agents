"""Critic Sub-Agent for adversarial falsification of candidate findings.

Lifting patterns from Google's adk-recipes/long-horizon-harness:
Stage 2 of the Native 'Hunter -> Critic -> Gate' pipeline. Acts as a skeptical Principal Engineer
who actively seeks to disprove each candidate finding against caller semantics and framework invariants.
Drops findings with confidence < 8 or severity == 'NIT'.
"""

from __future__ import annotations

import logging
import re

from webhook_agent.review.schemas import CandidateFinding, IssueItem, VerificationVerdict

logger = logging.getLogger("webhook_agent.review.critic")

MIN_VERIFIED_CONFIDENCE = 8


class CriticEngine:
    """Executes adversarial falsification against candidate review findings."""

    def __init__(self, min_confidence: int = MIN_VERIFIED_CONFIDENCE) -> None:
        self.min_confidence = min_confidence

    def evaluate_candidate(
        self,
        candidate: CandidateFinding,
        verdict: VerificationVerdict | None = None,
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

        # 2. If verdict is provided, check falsification verdict
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

        # 3. Default confidence check if no separate verdict
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
    ) -> list[IssueItem]:
        """Adversarially evaluate all candidates and convert verified survivors to IssueItems."""
        verdicts_map = verdicts or {}
        survivors: list[IssueItem] = []

        for cand in candidates:
            verdict = verdicts_map.get(cand.id)
            if self.evaluate_candidate(cand, verdict):
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

    def filter_issues(self, issues: list[IssueItem]) -> list[IssueItem]:
        """Filter out low-confidence findings (< min_confidence) and NITs from IssueItems."""
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
                    if conf < self.min_confidence:
                        logger.info(
                            "🛡️ Critic dropped IssueItem at %s:%s: confidence %d < threshold %d",
                            issue.path,
                            issue.line,
                            conf,
                            self.min_confidence,
                            extra={
                                "json_fields": {
                                    "event": "finding_falsified",
                                    "category": "critic_drop",
                                    "reason": f"confidence {conf} < threshold {self.min_confidence}",
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
