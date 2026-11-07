"""Hunter Engine orchestrating candidate finding extraction, budgeting, and 3+ grouping."""

from __future__ import annotations

import logging
from typing import Any

from webhook_agent.review.hunter.budgeter import ReviewBudgeter
from webhook_agent.review.hunter.grouping import DefectGrouper
from webhook_agent.review.schemas import CandidateFinding

logger = logging.getLogger("webhook_agent.review.hunter")


class HunterEngine:
    """Orchestrates candidate finding extraction and normalization for the Hunter stage."""

    def __init__(
        self,
        agent_runner: Any = None,
        budgeter: ReviewBudgeter | None = None,
        grouper: DefectGrouper | None = None,
    ) -> None:
        self.agent_runner = agent_runner
        self.budgeter = budgeter or ReviewBudgeter()
        self.grouper = grouper or DefectGrouper()

    def parse_candidate_findings(
        self, raw_findings: list[dict[str, Any]] | list[Any], apply_grouping: bool = True
    ) -> list[CandidateFinding]:
        """Convert raw LLM finding dictionaries into validated CandidateFinding instances."""
        parsed: list[CandidateFinding] = []
        for idx, item in enumerate(raw_findings):
            if isinstance(item, CandidateFinding):
                parsed.append(item)
                continue
            if not isinstance(item, dict):
                continue
            try:
                finding_id = str(item.get("id") or f"candidate-{idx + 1}")
                raw_severity = str(item.get("severity") or "MEDIUM").upper()
                valid_severities = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "NIT")
                severity = raw_severity if raw_severity in valid_severities else "MEDIUM"

                raw_score = item.get("confidence_score")
                confidence_score = max(1, min(10, int(raw_score))) if raw_score is not None else 5

                parsed.append(
                    CandidateFinding(
                        id=finding_id,
                        file_path=str(item.get("file_path") or item.get("path") or ""),
                        line_number=int(item.get("line_number") or item.get("line") or 1),
                        start_line=item.get("start_line"),
                        severity=severity,  # type: ignore[arg-type]
                        confidence_score=confidence_score,
                        title=str(item.get("title") or str(item.get("description", ""))[:60]),
                        description=str(item.get("description") or ""),
                        suggested_fix=item.get("suggested_fix"),
                        falsification_check=str(item.get("falsification_check") or ""),
                        window=item.get("window"),
                        verify_steps=item.get("verify_steps"),
                    )
                )
            except Exception as parse_err:
                logger.debug("Could not parse CandidateFinding item %s: %s", item, parse_err)

        if apply_grouping and len(parsed) >= 3:
            parsed = self.grouper.group_candidates(parsed)

        return parsed
