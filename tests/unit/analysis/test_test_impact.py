"""Unit tests for Smart Test Impact Analyzer (TestImpactAnalyzer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from webhook_agent.analysis.test_impact import (
    SymbolTestCoverage,
    TestCoverageStatus,
    TestImpactAnalyzer,
    TestImpactReport,
    evaluate_test_coverage_integrity,
    run_diff_coverage,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


class TestTestImpactAnalyzer:
    def test_covered_symbol_detection(self):
        analyzer = TestImpactAnalyzer()
        report = analyzer.analyze(["src/webhook_agent/state/reconciliation.py"])

        claim_cov = next(
            (c for c in report.coverages if c.symbol_name == "try_claim_reconciliation"),
            None,
        )
        assert claim_cov is not None
        assert claim_cov.status == TestCoverageStatus.COVERED
        assert len(claim_cov.covering_tests) > 0
        assert any("test_reconciliation.py" in t for t in claim_cov.covering_tests)

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


def test_run_diff_coverage_missing_or_empty_inputs(tmp_path: Path):
    pct, lines = run_diff_coverage("", repo_root=tmp_path)
    assert pct is None
    assert lines == 0

    pct, lines = run_diff_coverage("--- a/foo.py\n+++ b/foo.py\n", repo_root=tmp_path)
    assert pct is None
    assert lines == 0


def test_run_diff_coverage_with_valid_cobertura_report(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    sample_file = src_dir / "calc.py"
    sample_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    cov_xml = tmp_path / "coverage.xml"
    cov_xml.write_text(
        '<?xml version="1.0" ?>\n'
        '<coverage version="7.0">\n'
        "  <packages>\n"
        '    <package name="src">\n'
        "      <classes>\n"
        '        <class name="calc.py" filename="src/calc.py" line-rate="1.0">\n'
        "          <lines>\n"
        '            <line number="1" hits="1"/>\n'
        '            <line number="2" hits="1"/>\n'
        "          </lines>\n"
        "        </class>\n"
        "      </classes>\n"
        "    </package>\n"
        "  </packages>\n"
        "</coverage>\n",
        encoding="utf-8",
    )

    patch_text = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a\n"
        "+    return a + b\n"
    )

    pct, num_lines = run_diff_coverage(
        patch_text,
        coverage_file=cov_xml,
        repo_root=tmp_path,
    )
    assert pct is not None
    assert pct == 100.0
    assert num_lines == 1


def test_evaluate_test_coverage_integrity_non_code():
    report = TestImpactReport(
        modified_source_files=[],
        modified_test_files=[],
        coverages=[],
    )
    item = evaluate_test_coverage_integrity(report, is_docs_or_config_only=True)
    assert item.status == "PASS"
    assert "No executable production code modified" in item.notes

    item_dep = evaluate_test_coverage_integrity(report, is_pure_dep_pr=True)
    assert item_dep.status == "PASS"


def test_evaluate_test_coverage_integrity_diff_coverage():
    report = TestImpactReport(
        modified_source_files=["src/foo.py"],
        modified_test_files=[],
        coverages=[],
    )
    pass_item = evaluate_test_coverage_integrity(report, diff_coverage_pct=92.5)
    assert pass_item.status == "PASS"
    assert "92.5%" in pass_item.notes

    warn_item = evaluate_test_coverage_integrity(report, diff_coverage_pct=65.0)
    assert warn_item.status == "WARN"
    assert "65.0%" in warn_item.notes
    assert warn_item.status != "FAIL"


def test_evaluate_test_coverage_integrity_with_modified_test_files():
    report = TestImpactReport(
        modified_source_files=["src/foo.py"],
        modified_test_files=["tests/test_foo.py"],
        coverages=[],
    )
    item = evaluate_test_coverage_integrity(report)
    assert item.status == "PASS"
    assert "Test suite updated with 1 test file(s)" in item.notes


def test_evaluate_test_coverage_integrity_uncovered_symbols():
    cov = SymbolTestCoverage(
        symbol_name="untested_func",
        qualname="src.foo.untested_func",
        source_file="src/foo.py",
        line_number=10,
        status=TestCoverageStatus.UNCOVERED,
        suggested_stub="def test_untested_func(): pass",
    )
    report = TestImpactReport(
        modified_source_files=["src/foo.py"],
        modified_test_files=[],
        coverages=[cov],
    )
    item = evaluate_test_coverage_integrity(report)
    assert item.status == "WARN"
    assert "1 symbol(s)" in item.notes
    assert "untested_func" in item.notes
    # Crucial invariant: missing test coverage never yields a mechanical FAIL
    assert item.status != "FAIL"


def test_evaluate_test_coverage_integrity_scan_errors():
    report = TestImpactReport(
        modified_source_files=["src/foo.py"],
        modified_test_files=[],
        coverages=[],
    )
    item = evaluate_test_coverage_integrity(report, scan_errors=["AST parser error"])
    assert item.status == "UNKNOWN"
    assert "AST parser error" in item.notes
    assert item.status != "FAIL"
