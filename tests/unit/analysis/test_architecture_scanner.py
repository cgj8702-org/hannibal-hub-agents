"""Unit tests for ArchitecturalBoundaryScanner and layer isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from webhook_agent.analysis.architecture_scanner import (
    ArchitecturalBoundaryScanner,
    evaluate_architecture_contract,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

PROBE5_VULNERABLE_CODE = """
from __future__ import annotations
import re
from webhook_agent.tools.github_tools import list_dir

def check_pkg(pkg_name: str) -> bool:
    entries = list_dir(None)
    return pkg_name in entries
"""

TYPE_CHECKING_EXEMPT_CODE = """
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webhook_agent.tools.github_tools import list_dir

def check_pkg(pkg_name: str) -> bool:
    return True
"""

VALID_ANALYSIS_CODE = """
from __future__ import annotations
import ast
from webhook_agent.analysis.diff_filter import filter_review_diff

def run_analysis():
    pass
"""


def test_detect_probe5_boundary_inversion(tmp_path: Path) -> None:
    """Probe #5: Detect prohibited runtime import from tools into analysis package."""
    analysis_dir = tmp_path / "src" / "webhook_agent" / "analysis"
    analysis_dir.mkdir(parents=True)
    test_file = analysis_dir / "dependency_tree.py"
    test_file.write_text(PROBE5_VULNERABLE_CODE, encoding="utf-8")

    scanner = ArchitecturalBoundaryScanner(repo_root=tmp_path)
    rel_path = "src/webhook_agent/analysis/dependency_tree.py"
    report = scanner.scan(changed_files=[rel_path])

    assert report.has_critical_findings is True
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.source_package == "analysis"
    assert "webhook_agent.tools" in finding.prohibited_prefix
    assert finding.imported_module == "webhook_agent.tools.github_tools"

    scorecard_item = evaluate_architecture_contract(report)
    assert scorecard_item.dimension == "Contract Integrity"
    assert scorecard_item.status == "FAIL"
    assert "PROVEN architectural boundary violation" in scorecard_item.notes


def test_type_checking_imports_are_exempt(tmp_path: Path) -> None:
    """Imports enclosed inside if TYPE_CHECKING: are exempt from architectural failure."""
    analysis_dir = tmp_path / "src" / "webhook_agent" / "analysis"
    analysis_dir.mkdir(parents=True)
    test_file = analysis_dir / "safe_typing.py"
    test_file.write_text(TYPE_CHECKING_EXEMPT_CODE, encoding="utf-8")

    scanner = ArchitecturalBoundaryScanner(repo_root=tmp_path)
    rel_path = "src/webhook_agent/analysis/safe_typing.py"
    report = scanner.scan(changed_files=[rel_path])

    assert report.has_critical_findings is False
    assert len(report.findings) == 0

    scorecard_item = evaluate_architecture_contract(report)
    assert scorecard_item.status == "PASS"


def test_valid_analysis_imports_pass(tmp_path: Path) -> None:
    """Standard intra-package imports and standard library pass cleanly."""
    analysis_dir = tmp_path / "src" / "webhook_agent" / "analysis"
    analysis_dir.mkdir(parents=True)
    test_file = analysis_dir / "valid_code.py"
    test_file.write_text(VALID_ANALYSIS_CODE, encoding="utf-8")

    scanner = ArchitecturalBoundaryScanner(repo_root=tmp_path)
    rel_path = "src/webhook_agent/analysis/valid_code.py"
    report = scanner.scan(changed_files=[rel_path])

    assert report.has_critical_findings is False
    assert len(report.findings) == 0

    scorecard_item = evaluate_architecture_contract(report)
    assert scorecard_item.status == "PASS"


def test_scan_diff_hunks_detects_boundary_inversion() -> None:
    """Verify architectural scanner detects boundary inversions from unified diffs."""
    diff_text = """diff --git a/src/webhook_agent/models/rate_limiter.py b/src/webhook_agent/models/rate_limiter.py
--- a/src/webhook_agent/models/rate_limiter.py
+++ b/src/webhook_agent/models/rate_limiter.py
@@ -10,6 +10,7 @@ import time
+from webhook_agent.processor import WebhookProcessor
"""
    scanner = ArchitecturalBoundaryScanner()
    report = scanner.scan(diff_text=diff_text)

    assert report.has_critical_findings is True
    assert report.findings[0].source_package == "models"
    assert "webhook_agent.processor" in report.findings[0].imported_module
