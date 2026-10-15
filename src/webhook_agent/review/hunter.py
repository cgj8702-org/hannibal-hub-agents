"""Hunter Sub-Agent for high-recall candidate issue discovery.

Lifting patterns from Google's adk-recipes/long-horizon-harness:
Stage 1 of the Native 'Hunter -> Critic -> Gate' pipeline. Discovers candidate issues
(broken callers, unhandled edge cases, linked issue gaps) and articulates falsification checks.
"""

from __future__ import annotations

import logging
from typing import Any

from webhook_agent.review.schemas import CandidateFinding

logger = logging.getLogger("webhook_agent.review.hunter")

HUNTER_INSTRUCTION = """You are Hunter, a high-recall code defect and regression discovery engine.
Your mission is to find genuine regressions, broken call sites, signature contract mismatches,
resource leaks, and acceptance criteria gaps introduced by PR changes.

For each potential defect you identify, formulate a rigorous `CandidateFinding`:
1. file_path and line_number
2. severity: CRITICAL, HIGH, MEDIUM, LOW, or NIT
3. confidence_score: from 1 to 10
4. title: concise one-line summary
5. description: clinical explanation of why the code is defective
6. suggested_fix: actionable replacement code snippet
7. falsification_check: an explicit instruction telling a skeptical adversarial critic how
   to attempt to disprove or falsify this finding against callers or framework guarantees.

Search broadly across the repository using `search_files` to locate callers and impacted interfaces.
"""


class HunterEngine:
    """Orchestrates candidate finding extraction and normalization for the Hunter stage."""

    def __init__(self, agent_runner: Any = None) -> None:
        self.agent_runner = agent_runner

    def parse_candidate_findings(
        self, raw_findings: list[dict[str, Any]] | list[Any]
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
                    )
                )
            except Exception as parse_err:
                logger.debug("Could not parse CandidateFinding item %s: %s", item, parse_err)
        return parsed
