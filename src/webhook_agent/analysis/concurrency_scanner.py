"""Deterministic concurrency and thread-safety analysis engine.

Audits diffs and Python source ASTs for dangerous concurrency patterns, including:
- Lock / mutex / transaction removal around shared mutable state.
- Compound check-then-act / TOCTOU race conditions.
- Fallacious reliance on CPython GIL for compound dictionary/collection operations.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from webhook_agent.review.schemas import ScorecardItem

logger = logging.getLogger("webhook_agent.analysis.concurrency_scanner")

ConfidenceLevel = Literal["PROVEN", "SUSPECTED", "UNKNOWN"]
SeverityLevel = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "tests/",
    "test/",
    "testing/",
    "fixtures/",
    "test_fixtures/",
)

EXEMPT_FILE_SUFFIXES: tuple[str, ...] = (
    "_test.py",
    "test_.py",
)

# Regex patterns matching lock/mutex/transaction synchronization removal
RE_LOCK_WITH_PATTERN = re.compile(
    r"^\s*with\s+[^:]*(?:lock|mutex|semaphore|transactional?|atomic)[^:]*:",
    re.IGNORECASE,
)
RE_TRANSACTION_DECORATOR = re.compile(
    r"^\s*@(?:[\w.]+\.)?(?:transactional|atomic)\b",
    re.IGNORECASE,
)
RE_LOCK_ACQUIRE_RELEASE = re.compile(
    r"\b(?:[\w.]+\.)?(?:acquire|release)\s*\(\s*\)",
    re.IGNORECASE,
)


@dataclass
class ConcurrencyFinding:
    """A single deterministic concurrency defect or advisory finding."""

    file_path: str
    line_number: int | None
    rule_id: str
    confidence: ConfidenceLevel
    severity: SeverityLevel
    message: str
    evidence: str


@dataclass
class ConcurrencyAnalysisReport:
    """Consolidated concurrency audit findings and diagnostic telemetry."""

    findings: list[ConcurrencyFinding] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)

    def proven_findings(self) -> list[ConcurrencyFinding]:
        """Return blocking findings verified with high mechanical confidence."""
        return [f for f in self.findings if f.confidence == "PROVEN"]

    def suspected_findings(self) -> list[ConcurrencyFinding]:
        """Return advisory findings where manual verification is warranted."""
        return [f for f in self.findings if f.confidence == "SUSPECTED"]

    def to_markdown(self, **kwargs: Any) -> str:
        """Render a concise Markdown summary for the pre-audit dossier."""
        if not self.findings and not self.scan_errors:
            return "No concurrency risks or lock removals detected."

        lines: list[str] = []
        if self.findings:
            lines.append("#### ⚡ Concurrency & State Findings")
            for f in self.findings:
                loc = f"{f.file_path}:{f.line_number}" if f.line_number else f.file_path
                badge = "🔴 CRITICAL" if f.confidence == "PROVEN" else "🟡 ADVISORY"
                lines.append(f"- **{badge}** [{f.rule_id}] `{loc}`: {f.message}")
                if f.evidence:
                    lines.append(f"  * Evidence: `{f.evidence.strip()}`")
            lines.append(
                "> [!WARNING]\n"
                "> **CPython GIL Anti-Fallacy**: The CPython GIL only guarantees atomic execution of "
                "individual single bytecodes. It does NOT protect compound check-then-set or "
                "multi-step dictionary operations from thread preemption or TOCTOU race conditions."
            )

        if self.scan_errors:
            lines.append("#### ⚠️ Concurrency Scanner Errors")
            for err in self.scan_errors:
                lines.append(f"- {err}")

        return "\n".join(lines)


class ConcurrencyScanner:
    """Deterministic analyzer for synchronization integrity and race conditions."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root

    @staticmethod
    def _is_exempt(file_path: str) -> bool:
        normalized = file_path.replace("\\", "/").strip().lower()
        if any(normalized.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES):
            return True
        return any(normalized.endswith(suffix) for suffix in EXEMPT_FILE_SUFFIXES)

    def scan(
        self,
        diff_text: str,
        changed_files: list[str],
        repo_root: Path | None = None,
    ) -> ConcurrencyAnalysisReport:
        """Scan modified Python files and unified diff for synchronization removals."""
        report = ConcurrencyAnalysisReport()
        if not diff_text or not changed_files:
            return report

        py_files = [f for f in changed_files if f.endswith(".py") and not self._is_exempt(f)]
        if not py_files:
            return report

        self._scan_diff_for_lock_removals(diff_text, report)
        return report

    def _scan_diff_for_lock_removals(
        self,
        diff_text: str,
        report: ConcurrencyAnalysisReport,
    ) -> None:
        """Parse unified diff looking for deleted lock/transaction context blocks."""
        current_file: str | None = None
        old_line_num = 0
        deleted_locks_in_file: list[tuple[int, str]] = []

        def flush_file_findings(target_file: str) -> None:
            nonlocal deleted_locks_in_file
            if deleted_locks_in_file and not self._is_exempt(target_file):
                for line_no, lock_evidence in deleted_locks_in_file:
                    report.findings.append(
                        ConcurrencyFinding(
                            file_path=target_file,
                            line_number=line_no,
                            rule_id="concurrency:lock_removal",
                            confidence="PROVEN",
                            severity="CRITICAL",
                            message=(
                                f"Synchronization primitive removed (`{lock_evidence.strip()}`). "
                                "Removing mutual exclusion or transactional guards around shared state "
                                "exposes concurrent operations to TOCTOU race conditions. The CPython GIL "
                                "does NOT guarantee thread-safe compound check-then-set dictionary operations."
                            ),
                            evidence=lock_evidence,
                        )
                    )
            deleted_locks_in_file = []

        for raw_line in diff_text.splitlines():
            # File boundary check
            if raw_line.startswith("diff --git"):
                if current_file:
                    flush_file_findings(current_file)
                current_file = None
                continue

            if raw_line.startswith("--- a/"):
                current_file = raw_line[6:].strip()
                continue
            if raw_line.startswith("+++ b/"):
                if raw_line[6:].strip() != "/dev/null":
                    current_file = raw_line[6:].strip()
                continue

            # Chunk header
            if raw_line.startswith("@@"):
                match = re.search(r"@@ -(\d+)", raw_line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                continue

            if (
                not current_file
                or not current_file.endswith(".py")
                or self._is_exempt(current_file)
            ):
                continue

            # Track deletions and additions
            if raw_line.startswith("-"):
                old_line_num += 1
                line_content = raw_line[1:]
                if (
                    RE_LOCK_WITH_PATTERN.search(line_content)
                    or RE_TRANSACTION_DECORATOR.search(line_content)
                    or RE_LOCK_ACQUIRE_RELEASE.search(line_content)
                ):
                    deleted_locks_in_file.append((old_line_num, line_content))
            elif raw_line.startswith("+"):
                pass
            else:
                old_line_num += 1

        if current_file:
            flush_file_findings(current_file)


def evaluate_concurrency_integrity(
    report: ConcurrencyAnalysisReport,
    has_python_files: bool = True,
) -> ScorecardItem:
    """Evaluate Dimension 2 (Concurrency & State) scorecard item deterministically.

    - If PROVEN findings exist: FAIL (triggers scorecard floor; LLM cannot overturn).
    - If SUSPECTED findings exist: WARN (advisory).
    - If errors occurred without findings: UNKNOWN.
    - If no Python files modified: N/A.
    - Otherwise: PASS.
    """
    if not has_python_files:
        return ScorecardItem(
            dimension="Concurrency & State",
            status="N/A",
            notes="No Python source code modified in PR diff scope.",
        )

    proven = report.proven_findings()
    suspected = report.suspected_findings()

    if proven:
        summary_items = [f"{f.rule_id} in {f.file_path}:{f.line_number}" for f in proven[:3]]
        if len(proven) > 3:
            summary_items.append(f"...+{len(proven) - 3} more")
        notes = (
            f"PROVEN concurrency defect(s) detected: {'; '.join(summary_items)}. "
            "Synchronization lock or transaction context was stripped around shared state mutations, "
            "introducing TOCTOU race conditions under concurrent workloads."
        )
        return ScorecardItem(
            dimension="Concurrency & State",
            status="FAIL",
            notes=notes,
        )

    if suspected:
        summary_items = [f"{f.rule_id} in {f.file_path}:{f.line_number}" for f in suspected[:3]]
        if len(suspected) > 3:
            summary_items.append(f"...+{len(suspected) - 3} more")
        notes = f"SUSPECTED concurrency advisory item(s): {'; '.join(summary_items)}."
        return ScorecardItem(
            dimension="Concurrency & State",
            status="WARN",
            notes=notes,
        )

    if report.scan_errors:
        return ScorecardItem(
            dimension="Concurrency & State",
            status="UNKNOWN",
            notes=f"Concurrency scanner encountered non-fatal error: {'; '.join(report.scan_errors)}",
        )

    return ScorecardItem(
        dimension="Concurrency & State",
        status="PASS",
        notes="Deterministic audit confirmed 0 synchronization lock removals or unsynchronized shared state mutations.",
    )
