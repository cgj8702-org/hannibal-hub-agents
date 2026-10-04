"""ADK FunctionTools for smart test impact analysis and test coverage inspection."""

from __future__ import annotations

import logging

from google.adk.tools import FunctionTool

from webhook_agent.logic.test_impact import TestImpactAnalyzer

logger = logging.getLogger("webhook_agent.tools.test_impact_tools")


def check_test_coverage(
    file_path: str,
    symbol_name: str = "",
) -> str:
    """Inspect unit test coverage and locate test cases for modified Python files or symbols.

    Scans the test suite under tests/ to verify whether functions or methods in the
    target file have corresponding test coverage, cites exact test functions, and
    generates runnable test stubs for uncovered functions.

    Args:
        file_path: Path to the Python source file (e.g. 'src/webhook_agent/worker.py').
        symbol_name: Optional specific function or method name to inspect. If omitted,
            audits all symbols defined in the file.

    Returns:
        A clinical Markdown report listing coverage status, test function citations,
        and recommended test stubs.
    """
    cleaned_path = file_path[2:] if file_path.startswith(("a/", "b/")) else file_path
    analyzer = TestImpactAnalyzer()
    report = analyzer.analyze([cleaned_path])

    if symbol_name:
        filtered = [
            c for c in report.coverages if c.symbol_name == symbol_name or c.qualname == symbol_name
        ]
        from webhook_agent.logic.test_impact import TestImpactReport

        report = TestImpactReport(
            modified_source_files=report.modified_source_files,
            modified_test_files=report.modified_test_files,
            coverages=filtered,
        )

    return report.to_markdown()


check_test_coverage_tool = FunctionTool(check_test_coverage)
