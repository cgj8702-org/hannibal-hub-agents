"""Unit tests for deterministic AST tautology scanner (TautologyScanner)."""

from __future__ import annotations

import pytest

from webhook_agent.analysis.tautology_scanner import (
    TautologyScanner,
    evaluate_tautology_integrity,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

PROBE5_TAUTOLOGY_DIFF = """diff --git a/tests/unit/review/test_writeback_policy.py b/tests/unit/review/test_writeback_policy.py
index a1b2c3d..e4f5g6h 100644
--- a/tests/unit/review/test_writeback_policy.py
+++ b/tests/unit/review/test_writeback_policy.py
@@ -50,6 +50,11 @@ class TestCommentRateLimiter:
+    def test_comment_rate_limiter_boundary(self):
+        limiter = CommentRateLimiter(max_comments=1)
+        with patch.object(limiter, "is_allowed", return_value=True):
+            assert limiter.is_allowed("cgj8702", "pr_review") is True
"""

CLEAN_TEST_DIFF = """diff --git a/tests/unit/review/test_writeback_policy.py b/tests/unit/review/test_writeback_policy.py
index a1b2c3d..e4f5g6h 100644
--- a/tests/unit/review/test_writeback_policy.py
+++ b/tests/unit/review/test_writeback_policy.py
@@ -50,6 +50,10 @@ class TestCommentRateLimiter:
+    def test_comment_rate_limiter_boundary(self):
+        limiter = CommentRateLimiter(max_comments=1)
+        assert limiter.is_allowed("cgj8702", "pr_review") is True
+        assert limiter.is_allowed("cgj8702", "pr_review") is False
"""


def test_tautology_scanner_detects_probe5_self_mocking() -> None:
    scanner = TautologyScanner()
    code = """
def test_comment_rate_limiter_boundary():
    limiter = CommentRateLimiter(max_comments=1)
    with patch.object(limiter, "is_allowed", return_value=True):
        assert limiter.is_allowed("cgj8702", "pr_review") is True
"""
    findings = scanner.scan_code(code, modified_symbols={"is_allowed"})
    assert len(findings) == 1
    f = findings[0]
    assert f.finding_type == "SELF_MOCKING"
    assert f.severity == "CRITICAL"
    assert "is_allowed" in f.description
    assert "zero genuine coverage" in f.description


def test_tautology_scanner_detects_self_mocking_with_variable_assignment() -> None:
    scanner = TautologyScanner()
    code = """
def test_mock_assigned():
    limiter = CommentRateLimiter(max_comments=1)
    with patch.object(limiter, "is_allowed", return_value=True):
        allowed = limiter.is_allowed("cgj8702", "pr_review")
        assert allowed is True
"""
    findings = scanner.scan_code(code, modified_symbols={"is_allowed"})
    assert len(findings) == 1
    assert findings[0].finding_type == "SELF_MOCKING"
    assert findings[0].severity == "CRITICAL"


def test_tautology_scanner_detects_tautological_mock_assert_even_without_modified_symbols() -> None:
    scanner = TautologyScanner()
    code = """
def test_tautological_mock():
    with patch.object(service, "execute", return_value=42):
        assert service.execute() == 42
"""
    findings = scanner.scan_code(code)
    assert len(findings) == 1
    assert findings[0].finding_type == "SELF_MOCKING"
    assert findings[0].severity == "CRITICAL"
    assert "tautologically asserts its own mocked return value" in findings[0].description


def test_tautology_scanner_detects_decorator_patch_self_mocking() -> None:
    scanner = TautologyScanner()
    code = """
@patch.object(CommentRateLimiter, "is_allowed", return_value=True)
def test_decorator_mock(mock_allowed):
    limiter = CommentRateLimiter()
    assert limiter.is_allowed("cgj8702", "pr_review") is True
"""
    findings = scanner.scan_code(code, modified_symbols={"is_allowed"})
    assert len(findings) == 1
    assert findings[0].finding_type == "SELF_MOCKING"
    assert findings[0].severity == "CRITICAL"


def test_tautology_scanner_detects_vacuous_assert_constants() -> None:
    scanner = TautologyScanner()
    code = """
def test_vacuous():
    x = 10
    assert True
    assert 1
    assert "constant"
"""
    findings = scanner.scan_code(code)
    vacuous = [f for f in findings if f.finding_type == "VACUOUS_ASSERTION"]
    assert len(vacuous) == 3
    assert all(f.severity == "CRITICAL" for f in vacuous)


def test_tautology_scanner_detects_vacuous_identity_comparisons() -> None:
    scanner = TautologyScanner()
    code = """
def test_identity_asserts():
    x = 10
    assert x == x
    assert obj.property == obj.property
    assert y is y
"""
    findings = scanner.scan_code(code)
    vacuous = [f for f in findings if f.finding_type == "VACUOUS_ASSERTION"]
    assert len(vacuous) == 3
    assert all(f.severity == "CRITICAL" for f in vacuous)


def test_tautology_scanner_detects_unittest_vacuous_asserts() -> None:
    scanner = TautologyScanner()
    code = """
class MyTestCase(unittest.TestCase):
    def test_unittest_vacuous(self):
        self.assertTrue(True)
        self.assertEqual(1, 1)
        self.assertEqual(self.val, self.val)
"""
    findings = scanner.scan_code(code)
    vacuous = [f for f in findings if f.finding_type == "VACUOUS_ASSERTION"]
    assert len(vacuous) == 3
    assert all(f.severity == "CRITICAL" for f in vacuous)


def test_tautology_scanner_detects_empty_test_bodies() -> None:
    scanner = TautologyScanner()
    code = """
def test_empty_pass():
    pass

def test_empty_ellipsis():
    ...

def test_empty_docstring_only():
    \"\"\"This test has only a docstring.\"\"\"
"""
    findings = scanner.scan_code(code)
    empty = [f for f in findings if f.finding_type == "EMPTY_TEST"]
    assert len(empty) == 3
    assert all(f.severity == "CRITICAL" for f in empty)


def test_tautology_scanner_warns_on_test_with_no_assertions() -> None:
    scanner = TautologyScanner()
    code = """
def test_smoke():
    x = 10
    y = x * 2
    logger.info("Computed: %d", y)
"""
    findings = scanner.scan_code(code)
    assert len(findings) == 1
    assert findings[0].finding_type == "EMPTY_TEST"
    assert findings[0].severity == "WARN"
    assert "contains no assert statements" in findings[0].description


def test_tautology_scanner_allows_legitimate_dependency_mocking() -> None:
    scanner = TautologyScanner()
    code = """
def test_service_with_mocked_db():
    with patch.object(database, "query", return_value=[{"id": 1, "name": "alice"}]):
        result = user_service.get_active_users()
        assert result == ["alice"]
"""
    findings = scanner.scan_code(code, modified_symbols={"get_active_users"})
    # Mocking database.query while testing get_active_users is valid dependency mocking
    assert len(findings) == 0


def test_tautology_scanner_scan_diff_detects_probe5() -> None:
    scanner = TautologyScanner()
    report = scanner.scan_diff(PROBE5_TAUTOLOGY_DIFF, modified_symbols={"is_allowed"})
    assert report.has_critical_tautologies
    assert len(report.findings) >= 1
    assert report.findings[0].finding_type == "SELF_MOCKING"

    item = evaluate_tautology_integrity(report)
    assert item.status == "FAIL"
    assert item.dimension == "Test Coverage & Regressions"
    assert "CRITICAL: Tautological test defect(s) detected" in item.notes


def test_tautology_scanner_scan_diff_clean() -> None:
    scanner = TautologyScanner()
    report = scanner.scan_diff(CLEAN_TEST_DIFF, modified_symbols={"is_allowed"})
    assert not report.has_critical_tautologies
    assert len(report.findings) == 0

    item = evaluate_tautology_integrity(report)
    assert item.status == "PASS"
    assert item.dimension == "Test Coverage & Regressions"
    assert "No tautological tests" in item.notes
