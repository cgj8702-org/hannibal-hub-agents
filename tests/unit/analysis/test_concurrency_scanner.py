"""Unit tests for deterministic concurrency scanner and Anti-GIL race condition analysis."""

from __future__ import annotations

import pytest

from webhook_agent.analysis.concurrency_scanner import (
    ConcurrencyScanner,
    evaluate_concurrency_integrity,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

PROBE2_LOCK_REMOVAL_DIFF = """diff --git a/src/webhook_agent/state/review_idempotency.py b/src/webhook_agent/state/review_idempotency.py
index 5800edc..7111d44 100644
--- a/src/webhook_agent/state/review_idempotency.py
+++ b/src/webhook_agent/state/review_idempotency.py
@@ -86,17 +86,17 @@ class ReviewClaimRegistry:
         github_review_exists: bool,
         force: bool = False,
     ) -> ReviewClaim | None:
-        with self._local_lock:
-            existing = self._local_claims.get(key)
-            if not force and existing and existing[0] == "submitted":
-                return None
-            if not force and github_review_exists:
-                self._local_claims[key] = ("submitted", 0.0, token)
-                return None
-            if existing and existing[0] == "processing" and existing[1] > now.timestamp():
-                return None
-            self._local_claims[key] = ("processing", lease_until.timestamp(), token)
-            return ReviewClaim(key, token)
+        # Fast-path: bypass thread lock overhead on local claim map
+        existing = self._local_claims.get(key)
+        if not force and existing and existing[0] == "submitted":
+            return None
+        if not force and github_review_exists:
+            self._local_claims[key] = ("submitted", 0.0, token)
+            return None
+        if existing and existing[0] == "processing" and existing[1] > now.timestamp():
+            return None
+        self._local_claims[key] = ("processing", lease_until.timestamp(), token)
+        return ReviewClaim(key, token)
"""

CLEAN_CONCURRENCY_DIFF = """diff --git a/src/webhook_agent/utils/string_helpers.py b/src/webhook_agent/utils/string_helpers.py
--- a/src/webhook_agent/utils/string_helpers.py
+++ b/src/webhook_agent/utils/string_helpers.py
@@ -10,3 +10,4 @@ def sanitize(text: str) -> str:
+    text = text.strip()
     return text
"""

TEST_FILE_LOCK_DIFF = """diff --git a/tests/unit/test_concurrency.py b/tests/unit/test_concurrency.py
--- a/tests/unit/test_concurrency.py
+++ b/tests/unit/test_concurrency.py
@@ -10,3 +10,3 @@ def test_mock_lock():
-    with lock:
-        pass
+    pass
"""


def test_concurrency_scanner_detects_lock_removal() -> None:
    scanner = ConcurrencyScanner()
    report = scanner.scan(
        diff_text=PROBE2_LOCK_REMOVAL_DIFF,
        changed_files=["src/webhook_agent/state/review_idempotency.py"],
    )

    proven = report.proven_findings()
    assert len(proven) == 1
    finding = proven[0]
    assert finding.rule_id == "concurrency:lock_removal"
    assert finding.file_path == "src/webhook_agent/state/review_idempotency.py"
    assert finding.confidence == "PROVEN"
    assert finding.severity == "CRITICAL"
    assert "with self._local_lock:" in finding.evidence

    scorecard_item = evaluate_concurrency_integrity(report, has_python_files=True)
    assert scorecard_item.status == "FAIL"
    assert scorecard_item.dimension == "Concurrency & State"
    assert "PROVEN concurrency defect(s) detected" in scorecard_item.notes
    assert "TOCTOU" in scorecard_item.notes

    md = report.to_markdown()
    assert "CPython GIL Anti-Fallacy" in md
    assert "CRITICAL" in md


def test_concurrency_scanner_exempts_tests_directory() -> None:
    scanner = ConcurrencyScanner()
    report = scanner.scan(
        diff_text=TEST_FILE_LOCK_DIFF,
        changed_files=["tests/unit/test_concurrency.py"],
    )
    assert len(report.proven_findings()) == 0
    scorecard_item = evaluate_concurrency_integrity(report, has_python_files=True)
    assert scorecard_item.status == "PASS"


def test_concurrency_scanner_clean_diff_passes() -> None:
    scanner = ConcurrencyScanner()
    report = scanner.scan(
        diff_text=CLEAN_CONCURRENCY_DIFF,
        changed_files=["src/webhook_agent/utils/string_helpers.py"],
    )
    assert len(report.findings) == 0
    scorecard_item = evaluate_concurrency_integrity(report, has_python_files=True)
    assert scorecard_item.status == "PASS"
    assert "0 synchronization lock removals" in scorecard_item.notes


def test_concurrency_scanner_non_python_scope_is_na() -> None:
    scanner = ConcurrencyScanner()
    report = scanner.scan(
        diff_text="diff --git a/docs/README.md b/docs/README.md\n+hello",
        changed_files=["docs/README.md"],
    )
    scorecard_item = evaluate_concurrency_integrity(report, has_python_files=False)
    assert scorecard_item.status == "N/A"
    assert "No Python source code modified" in scorecard_item.notes
