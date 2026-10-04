"""Unit tests for Smart Test Impact Analyzer (TestImpactAnalyzer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from webhook_agent.logic.test_impact import (
    TestCoverageStatus,
    TestImpactAnalyzer,
    TestImpactReport,
)
from webhook_agent.tools.test_impact_tools import check_test_coverage

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


class TestTestImpactAnalyzer:
    def test_covered_symbol_detection(self):
        analyzer = TestImpactAnalyzer()
        report = analyzer.analyze(["src/webhook_agent/proactive_service.py"])

        claim_cov = next(
            (c for c in report.coverages if c.symbol_name == "try_claim_reconciliation"),
            None,
        )
        assert claim_cov is not None
        assert claim_cov.status == TestCoverageStatus.COVERED
        assert len(claim_cov.covering_tests) > 0
        assert any("test_proactive_service.py" in t for t in claim_cov.covering_tests)

    def test_uncovered_symbol_detection_and_stub_generation(self, tmp_path: Path):
        src_dir = tmp_path / "src" / "sample_pkg"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "calculator.py"
        src_file.write_text(
            "def calculate_total_tax(amount: int, state_code: str) -> int:\n"
            "    return amount * 2\n",
            encoding="utf-8",
        )

        tests_dir = tmp_path / "tests" / "unit" / "sample_pkg"
        tests_dir.mkdir(parents=True)
        test_file = tests_dir / "test_calculator.py"
        test_file.write_text(
            "def test_unrelated():\n    assert True\n",
            encoding="utf-8",
        )

        analyzer = TestImpactAnalyzer(repo_root=tmp_path)
        report = analyzer.analyze(["src/sample_pkg/calculator.py"])

        assert len(report.coverages) == 1
        cov = report.coverages[0]
        assert cov.symbol_name == "calculate_total_tax"
        assert cov.status == TestCoverageStatus.UNCOVERED
        assert cov.suggested_stub is not None
        assert "def test_calculate_total_tax_basic():" in cov.suggested_stub
        assert "mock_amount = 1" in cov.suggested_stub
        assert 'mock_state_code = "state_code_sample"' in cov.suggested_stub

    def test_class_method_stub_generation(self, tmp_path: Path):
        src_dir = tmp_path / "src" / "sample_pkg"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "service.py"
        src_file.write_text(
            "class PaymentService:\n"
            "    def process_charge(self, token: str, is_active: bool = True) -> bool:\n"
            "        return True\n",
            encoding="utf-8",
        )

        analyzer = TestImpactAnalyzer(repo_root=tmp_path)
        report = analyzer.analyze(["src/sample_pkg/service.py"])

        cov = next((c for c in report.coverages if c.symbol_name == "process_charge"), None)
        assert cov is not None
        assert cov.status == TestCoverageStatus.UNCOVERED
        assert cov.suggested_stub is not None
        assert "instance = PaymentService()" in cov.suggested_stub
        assert "instance.process_charge(" in cov.suggested_stub

    def test_dunder_methods_skipped(self, tmp_path: Path):
        src_dir = tmp_path / "src" / "sample_pkg"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "models.py"
        src_file.write_text(
            "class MyModel:\n"
            "    def __init__(self, val: int) -> None:\n"
            "        self.val = val\n"
            "    def __repr__(self) -> str:\n"
            "        return str(self.val)\n",
            encoding="utf-8",
        )

        analyzer = TestImpactAnalyzer(repo_root=tmp_path)
        report = analyzer.analyze(["src/sample_pkg/models.py"])

        init_cov = next((c for c in report.coverages if c.symbol_name == "__init__"), None)
        assert init_cov is not None
        assert init_cov.status == TestCoverageStatus.SKIPPED

    def test_test_modified_classification(self, tmp_path: Path):
        src_dir = tmp_path / "src" / "sample_pkg"
        src_dir.mkdir(parents=True)
        src_file = src_dir / "utils.py"
        src_file.write_text(
            "def helper_fn() -> int:\n    return 42\n",
            encoding="utf-8",
        )

        tests_dir = tmp_path / "tests" / "unit" / "sample_pkg"
        tests_dir.mkdir(parents=True)
        test_file = tests_dir / "test_utils.py"
        test_file.write_text("# will be updated in PR\n", encoding="utf-8")

        analyzer = TestImpactAnalyzer(repo_root=tmp_path)
        report = analyzer.analyze(
            [
                "src/sample_pkg/utils.py",
                "tests/unit/sample_pkg/test_utils.py",
            ]
        )

        assert "tests/unit/sample_pkg/test_utils.py" in report.modified_test_files
        cov = next((c for c in report.coverages if c.symbol_name == "helper_fn"), None)
        assert cov is not None
        assert cov.status == TestCoverageStatus.TEST_MODIFIED

    def test_report_to_markdown_formatting(self):
        report = TestImpactReport(
            modified_source_files=["src/sample.py"],
            modified_test_files=["tests/test_sample.py"],
            coverages=[],
        )
        md = report.to_markdown()
        assert "Deterministic Test Coverage & Regression Impact" in md
        assert "PR Modified Tests" in md
        assert "No callable Python functions" in md

    def test_check_test_coverage_tool_execution(self):
        res = check_test_coverage(
            "src/webhook_agent/proactive_service.py", symbol_name="try_claim_reconciliation"
        )
        assert "Deterministic Test Coverage" in res
        assert "try_claim_reconciliation" in res
        assert "✅ COVERED" in res
