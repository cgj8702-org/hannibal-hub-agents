"""Pre-audit compiler: concurrent multi-perspective static analysis and dossier assembly.

Orchestrates AST integrity, cross-file symbol impacts, test coverage, and security scanners
concurrently across worker threads, compiling a comprehensive Pre-Audit Dossier.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webhook_agent.analysis.security_scanner import (
    SecurityAnalysisReport,
    SecurityScanner,
    evaluate_security_integrity,
)
from webhook_agent.analysis.symbol_graph import (
    SymbolImpactAnalyzer,
    SymbolImpactReport,
    evaluate_contract_integrity,
)
from webhook_agent.analysis.test_impact import (
    TestImpactAnalyzer,
    TestImpactReport,
    evaluate_test_coverage_integrity,
    run_diff_coverage,
)
from webhook_agent.environment.context import get_active_environment
from webhook_agent.models.rate_limiter import _resolve_tier
from webhook_agent.review.schemas import ScorecardItem
from webhook_agent.tools.ast_tools import verify_python_ast

logger = logging.getLogger("webhook_agent.analysis.pre_audit_compiler")


@dataclass
class PreAuditDossier:
    """Consolidated results of all concurrent pre-audit analysis passes."""

    dossier_markdown: str
    contract_scorecard: ScorecardItem
    test_scorecard: ScorecardItem
    security_scorecard: ScorecardItem
    ast_verified: bool = False
    full_files_included: int = 0


def compile_pre_audit_dossier(
    changed_files: list[str],
    pr_diff_text: str,
    event_data: dict[str, Any],
) -> PreAuditDossier:
    """Concurrently execute static audit pipelines and compile a multi-perspective dossier.

    Runs symbol dependency graphs, test coverage calculation, security scans, and AST integrity
    in parallel across thread workers, formatting results into high-signal markdown blocks.
    """
    tier = _resolve_tier()
    active_env = get_active_environment()
    repo_full_name = (event_data.get("repository") or {}).get("full_name", "")
    raw = event_data.get("raw_payload") or {}
    base_sha = str(
        (raw.get("pull_request") or {}).get("base", {}).get("sha")
        or event_data.get("base_sha")
        or ""
    )
    gh_client = event_data.get("gh_client")

    baseline_loader = None
    if gh_client and repo_full_name and base_sha:

        def _gh_baseline_loader(file_path: str) -> str | None:
            try:
                repo_obj = gh_client.get_repo(repo_full_name)
                content_file = repo_obj.get_contents(file_path, ref=base_sha)
                if hasattr(content_file, "decoded_content"):
                    return content_file.decoded_content.decode("utf-8", errors="replace")
                return None
            except Exception:
                return None

        baseline_loader = _gh_baseline_loader

    py_files = [f for f in changed_files if f.endswith(".py")]
    ast_limit = 50 if tier == "paid" else 5
    py_files_to_ast = py_files[:ast_limit]

    repo_root_path = Path(active_env.root) if active_env and active_env.root else None

    # Task workers for concurrent execution
    def _run_ast_scan() -> list[str]:
        items: list[str] = []
        try:
            for pf in py_files_to_ast:
                res = verify_python_ast(file_path=pf, diff_text=pr_diff_text)
                if not res.startswith("Error: No code snippet provided and file"):
                    items.append(res)
        except Exception as e:
            logger.warning("AST scan encountered error: %s", e)
        return items

    def _run_symbol_analysis() -> tuple[list[SymbolImpactReport] | None, ScorecardItem | None]:
        if repo_root_path is None:
            return None, None
        try:
            sym_analyzer = SymbolImpactAnalyzer(repo_root=repo_root_path)
            reports = sym_analyzer.analyze_modified_files(
                changed_files,
                baseline_loader=baseline_loader,
            )
            item = evaluate_contract_integrity(reports, parse_errors=sym_analyzer.parse_errors)
            return reports, item
        except Exception as e:
            logger.warning("Symbol graph analysis encountered error: %s", e)
            item = ScorecardItem(
                dimension="Contract Integrity",
                status="UNKNOWN",
                notes=f"Symbol graph analysis skipped: {e}",
            )
            return None, item

    def _run_test_pipeline() -> tuple[TestImpactReport | None, float | None, ScorecardItem | None]:
        if repo_root_path is None:
            return None, None, None
        try:
            test_analyzer = TestImpactAnalyzer(repo_root=repo_root_path)
            report = test_analyzer.analyze(changed_files)
            diff_pct, _ = run_diff_coverage(diff_text=pr_diff_text, repo_root=repo_root_path)
            item = evaluate_test_coverage_integrity(report, diff_coverage_pct=diff_pct)
            return report, diff_pct, item
        except Exception as e:
            logger.warning("Test coverage analysis encountered error: %s", e)
            item = ScorecardItem(
                dimension="Test Coverage & Regressions",
                status="UNKNOWN",
                notes=f"Test impact analysis skipped: {e}",
            )
            return None, None, item

    def _run_security_scan() -> tuple[SecurityAnalysisReport | None, ScorecardItem | None]:
        try:
            scanner = SecurityScanner(repo_root=repo_root_path)
            report = scanner.scan(
                diff_text=pr_diff_text,
                changed_files=changed_files,
                repo_root=repo_root_path,
            )
            item = evaluate_security_integrity(report)
            return report, item
        except Exception as e:
            logger.warning("Security scanner encountered error: %s", e)
            item = ScorecardItem(
                dimension="Security & Secrets",
                status="UNKNOWN",
                notes=f"Security scan skipped: {e}",
            )
            return None, item

    # Execute all 4 static pipelines concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        f_ast = executor.submit(_run_ast_scan)
        f_sym = executor.submit(_run_symbol_analysis)
        f_test = executor.submit(_run_test_pipeline)
        f_sec = executor.submit(_run_security_scan)

        ast_items = f_ast.result()
        sym_reports, contract_item = f_sym.result()
        test_report, diff_pct, test_item = f_test.result()
        sec_report, sec_item = f_sec.result()

    dossier_sections: list[str] = []

    # 1. AST Structural Findings
    if ast_items:
        ast_text = "\n\n".join(ast_items)
        dossier_sections.append(
            f"### 🔍 Deterministic Pre-Audit Compiler Findings\n"
            f"The deterministic AST integrity engine pre-audited modified Python files ({len(ast_items)} file(s)):\n\n"
            f"{ast_text}\n\n"
            f"Use these findings to focus your audit on structural defects, risk areas, and verified AST nodes."
        )

    # 2. Full Source File Context (Paid Tier High-Capacity Mode)
    full_files_count = 0
    if tier == "paid" and repo_root_path is not None:
        file_blocks: list[str] = []
        for mf in py_files[:10]:
            file_disk_path = repo_root_path / mf
            if file_disk_path.exists() and file_disk_path.is_file():
                try:
                    content = file_disk_path.read_text(encoding="utf-8", errors="replace")
                    file_blocks.append(f"#### `{mf}`\n```python\n{content}\n```")
                    full_files_count += 1
                except Exception as exc:
                    logger.debug("Failed reading file %s for dossier inclusion: %s", mf, exc)
        if file_blocks:
            dossier_sections.append(
                "### 📂 Full Source File Context (Paid Tier High-Capacity Mode)\n"
                "The complete contents of primary modified files are provided below for end-to-end structural verification:\n\n"
                + "\n\n".join(file_blocks)
            )

    # 3. Cross-File Symbol Contract Analysis
    if contract_item is not None:
        impact_blocks = [
            r.to_markdown()
            for r in (sym_reports or [])
            if r.broken_call_sites or (r.sig_diff and r.sig_diff.is_breaking)
        ]
        if impact_blocks or contract_item.status in ("FAIL", "WARN", "ERROR"):
            impact_text = (
                "\n\n".join(impact_blocks) if impact_blocks else "No broken call sites detected."
            )
            dossier_sections.append(
                f"### 🕸️ Cross-File Contract & Symbol Impact Analysis\n"
                f"The static symbol dependency engine detected contract alterations and cross-file callers:\n\n"
                f"**Dimension 4 (Contract Integrity) Evaluated Status**: `{contract_item.status}`\n"
                f"**Notes**: {contract_item.notes}\n\n"
                f"{impact_text}\n\n"
                f"Verify whether external callers are broken and enforce Dimension 4 (Contract Integrity)."
            )
    else:
        contract_item = ScorecardItem(
            dimension="Contract Integrity",
            status="UNKNOWN",
            notes="Symbol graph analysis skipped (no active workspace).",
        )

    # 4. Test Coverage & Impact
    if test_item is not None:
        if test_report and (
            test_report.coverages or diff_pct is not None or test_item.status != "PASS"
        ):
            test_md = test_report.to_markdown()
            cov_note = (
                f"\n**Diff Test Coverage**: `{diff_pct:.1f}%`\n" if diff_pct is not None else ""
            )
            dossier_sections.append(
                f"{test_md}{cov_note}\n"
                f"**Dimension 5 (Test Coverage & Regressions) Evaluated Status**: `{test_item.status}`\n"
                f"**Notes**: {test_item.notes}"
            )
    else:
        test_item = ScorecardItem(
            dimension="Test Coverage & Regressions",
            status="UNKNOWN",
            notes="Test impact analysis skipped (no active workspace).",
        )

    # 5. Security & Secrets Findings
    if sec_item is not None:
        if sec_report is not None and (
            sec_report.active_findings() or sec_item.status in ("FAIL", "WARN")
        ):
            dossier_sections.append(
                f"### 🛡️ Deterministic Security Pre-Audit Findings\n"
                f"{sec_report.to_markdown()}\n\n"
                f"**Dimension 3 (Security & Secrets) Evaluated Status**: `{sec_item.status}`\n"
                f"**Notes**: {sec_item.notes}\n\n"
                f"Enforce Dimension 3 (Security & Secrets) in the review scorecard. "
                f"Proven defects mechanically prevent approval."
            )
    else:
        sec_item = ScorecardItem(
            dimension="Security & Secrets",
            status="PASS",
            notes="No static security scanner findings.",
        )

    return PreAuditDossier(
        dossier_markdown="\n\n".join(dossier_sections),
        contract_scorecard=contract_item,
        test_scorecard=test_item,
        security_scorecard=sec_item,
        ast_verified=bool(ast_items),
        full_files_included=full_files_count,
    )
