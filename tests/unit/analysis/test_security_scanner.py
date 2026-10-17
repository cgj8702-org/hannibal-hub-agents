"""Unit tests for deterministic security scanner engine wrapping detect-secrets and bandit."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.analysis.security_scanner import (
    SecurityAnalysisReport,
    SecurityFinding,
    SecurityScanner,
    evaluate_security_integrity,
    is_exempt_file_path,
    is_exempt_secret_value,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_exempt_file_path() -> None:
    # Test directories and files
    assert is_exempt_file_path("tests/unit/test_app.py") is True
    assert is_exempt_file_path("test/integration/test_api.py") is True
    assert is_exempt_file_path("fixtures/dummy_keys.json") is True
    assert is_exempt_file_path("docs/security.md") is True
    assert is_exempt_file_path("conftest.py") is True
    assert is_exempt_file_path("uv.lock") is True
    assert is_exempt_file_path(".agents/skills/test.py") is True
    assert is_exempt_file_path("package-lock.json") is True

    # Production code paths (not exempt)
    assert is_exempt_file_path("src/webhook_agent/core/auth.py") is False
    assert is_exempt_file_path("src/webhook_agent/app.py") is False
    assert is_exempt_file_path("main.py") is False


def test_is_exempt_secret_value() -> None:
    # Empty or non-dummy string is not exempt
    assert is_exempt_secret_value("") is False
    assert is_exempt_secret_value("abc") is False

    # Common dummy / mock patterns
    assert is_exempt_secret_value("dummy_token_value_xyz") is True
    assert is_exempt_secret_value("my_fake_api_secret_key") is True
    assert is_exempt_secret_value("sample_private_key_value") is True
    assert is_exempt_secret_value("placeholder_token_abc") is True
    assert is_exempt_secret_value("mock_client_secret_999") is True
    assert is_exempt_secret_value("sk-xxx") is True

    # Real secret pattern (not exempt)
    assert is_exempt_secret_value("ghp_abzdefghijklnmopqrstuvwxyz9876543210") is False


def test_scan_diff_secrets_empty_diff() -> None:
    scanner = SecurityScanner()
    assert scanner.scan_diff_secrets("") == []
    assert scanner.scan_diff_secrets("   \n") == []


def test_scan_diff_secrets_detects_secret() -> None:
    scanner = SecurityScanner()
    diff_text = """diff --git a/src/client.py b/src/client.py
--- a/src/client.py
+++ b/src/client.py
@@ -10,3 +10,4 @@
 def get_client():
+    token = "ghp_abzdefghijklnmopqrstuvwxyz9876543210"
     return Client(token=token)
"""
    findings = scanner.scan_diff_secrets(diff_text)
    # detect-secrets flags GitHub Personal Access Tokens or High Entropy Strings
    assert len(findings) >= 1
    f = findings[0]
    assert f.file_path == "src/client.py"
    assert f.line_number == 11
    assert f.tool == "detect-secrets"
    assert f.confidence == "PROVEN"
    assert f.is_exempt is False


def test_scan_diff_secrets_respects_exempt_path() -> None:
    scanner = SecurityScanner()
    diff_text = """diff --git a/tests/test_client.py b/tests/test_client.py
--- a/tests/test_client.py
+++ b/tests/test_client.py
@@ -1,3 +1,4 @@
+TEST_TOKEN = "ghp_abzdefghijklnmopqrstuvwxyz9876543210"
"""
    findings = scanner.scan_diff_secrets(diff_text)
    assert len(findings) >= 1
    f = findings[0]
    assert f.file_path == "tests/test_client.py"
    assert f.is_exempt is True
    assert f.confidence == "SUSPECTED"
    assert "Exempt file path" in f.exemption_reason


def test_scan_python_bandit_subprocess(tmp_path: Path) -> None:
    scanner = SecurityScanner(repo_root=tmp_path)

    # Mock bandit JSON output
    mock_bandit_output = {
        "results": [
            {
                "code": "eval(user_input)",
                "filename": str(tmp_path / "src" / "insecure.py"),
                "issue_confidence": "HIGH",
                "issue_severity": "HIGH",
                "issue_text": "Use of possibly insecure function - consider using safer ast.literal_eval.",
                "line_number": 42,
                "test_id": "B307",
            }
        ]
    }

    test_file = tmp_path / "src" / "insecure.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("eval(user_input)\n")

    mock_res = MagicMock()
    mock_res.stdout = json.dumps(mock_bandit_output)

    with patch("subprocess.run", return_value=mock_res) as mock_run:
        findings, errors = scanner.scan_python_bandit(["src/insecure.py"])
        assert mock_run.called
        assert len(errors) == 0
        assert len(findings) == 1

        finding = findings[0]
        assert finding.tool == "bandit"
        assert finding.rule_id == "B307"
        assert finding.file_path == "src/insecure.py"
        assert finding.line_number == 42
        assert finding.severity == "CRITICAL"
        assert finding.confidence == "PROVEN"
        assert finding.is_exempt is False


def test_scan_python_bandit_skips_exempt_paths() -> None:
    scanner = SecurityScanner()
    with patch("subprocess.run") as mock_run:
        findings, errors = scanner.scan_python_bandit(["tests/unit/test_app.py", "docs/index.py"])
        assert not mock_run.called
        assert findings == []
        assert errors == []


def test_evaluate_security_integrity_proven_fail() -> None:
    finding = SecurityFinding(
        tool="detect-secrets",
        rule_id="AWS Key",
        file_path="src/config.py",
        line_number=15,
        severity="CRITICAL",
        confidence="PROVEN",
        description="Hardcoded AWS secret key detected",
        code_snippet="AWS_SECRET = ...",
        is_exempt=False,
    )
    report = SecurityAnalysisReport(findings=[finding])
    item = evaluate_security_integrity(report)

    assert item.dimension == "Security & Secrets"
    assert item.status == "FAIL"
    assert "PROVEN security defect(s) detected" in item.notes
    assert "detect-secrets:AWS Key in src/config.py:15" in item.notes


def test_evaluate_security_integrity_suspected_warn() -> None:
    finding = SecurityFinding(
        tool="bandit",
        rule_id="B404",
        file_path="src/utils.py",
        line_number=20,
        severity="LOW",
        confidence="SUSPECTED",
        description="Consider possible security implications associated with the subprocess module.",
        code_snippet="import subprocess",
        is_exempt=False,
    )
    report = SecurityAnalysisReport(findings=[finding])
    item = evaluate_security_integrity(report)

    assert item.dimension == "Security & Secrets"
    assert item.status == "WARN"
    assert "SUSPECTED security advisory item(s)" in item.notes


def test_evaluate_security_integrity_scan_errors_unknown() -> None:
    report = SecurityAnalysisReport(findings=[], scan_errors=["bandit execution timed out"])
    item = evaluate_security_integrity(report)

    assert item.dimension == "Security & Secrets"
    assert item.status == "UNKNOWN"
    assert "bandit execution timed out" in item.notes


def test_evaluate_security_integrity_clean_pass() -> None:
    report = SecurityAnalysisReport(findings=[], scan_errors=[])
    item = evaluate_security_integrity(report)

    assert item.dimension == "Security & Secrets"
    assert item.status == "PASS"
    assert "Deterministic audit confirmed 0 hardcoded secrets" in item.notes


def test_report_to_markdown() -> None:
    clean_report = SecurityAnalysisReport()
    assert "✅ No hardcoded secrets" in clean_report.to_markdown()

    finding = SecurityFinding(
        tool="detect-secrets",
        rule_id="Secret Keyword",
        file_path="src/auth.py",
        line_number=10,
        severity="CRITICAL",
        confidence="PROVEN",
        description="API token detected",
        code_snippet="TOKEN = 'abc'",
    )
    report = SecurityAnalysisReport(findings=[finding])
    md = report.to_markdown()
    assert "#### 🛡️ Active Security Findings" in md
    assert (
        "**[PROVEN CRITICAL]** `src/auth.py:10` (detect-secrets `Secret Keyword`): API token detected"
        in md
    )
