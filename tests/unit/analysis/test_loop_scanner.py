"""Unit tests for LoopTerminationScanner and loop safety evaluation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from webhook_agent.analysis.loop_scanner import (
    LoopTerminationScanner,
    evaluate_loop_termination,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

PROBE4_VULNERABLE_CODE = """
class CommentRateLimiter:
    def __init__(self):
        self._history = {}

    def purge_stale_records(self, max_age: float = 3600.0) -> int:
        now = 1000.0
        cutoff = now - max_age
        evicted_count = 0
        for key, timestamps in list(self._history.items()):
            idx = 0
            while idx < len(timestamps):
                if timestamps[idx] < cutoff:
                    timestamps.pop(idx)
                    evicted_count += 1
            if not timestamps:
                del self._history[key]
        return evicted_count
"""

SAFE_LOOP_WITH_ELSE = """
def process_items(items):
    idx = 0
    while idx < len(items):
        if items[idx] == "skip":
            idx += 2
        else:
            idx += 1
"""

SAFE_LOOP_WITH_FALLTHROUGH_ADVANCE = """
def process_items(items):
    idx = 0
    while idx < len(items):
        if items[idx] == "skip":
            log_skip()
        idx += 1
"""

SAFE_WHILE_TRUE_WITH_BREAK = """
def worker_loop(queue):
    while True:
        if queue.empty():
            break
        item = queue.get()
        process(item)
"""

UNSAFE_WHILE_TRUE_NO_BREAK = """
def worker_loop():
    while True:
        pass
"""


def test_detect_probe4_non_advancing_loop(tmp_path: Path) -> None:
    """Probe #4: Detect non-advancing while loop where index is only conditionally popped."""
    test_file = tmp_path / "rate_limiter.py"
    test_file.write_text(PROBE4_VULNERABLE_CODE, encoding="utf-8")

    scanner = LoopTerminationScanner(repo_root=tmp_path)
    report = scanner.scan(changed_files=["rate_limiter.py"])

    assert report.has_critical_findings is True
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.finding_type == "NON_ADVANCING_LOOP"
    assert "idx" in finding.variables or "timestamps" in finding.variables
    assert finding.line_number > 0

    scorecard_item = evaluate_loop_termination(report)
    assert scorecard_item.dimension == "Logic & Boundaries"
    assert scorecard_item.status == "FAIL"
    assert "PROVEN loop termination defect" in scorecard_item.notes


def test_detect_unbounded_while_true_loop(tmp_path: Path) -> None:
    """Detect while True loop without exit statements."""
    test_file = tmp_path / "worker.py"
    test_file.write_text(UNSAFE_WHILE_TRUE_NO_BREAK, encoding="utf-8")

    scanner = LoopTerminationScanner(repo_root=tmp_path)
    report = scanner.scan(changed_files=["worker.py"])

    assert report.has_critical_findings is True
    assert len(report.findings) == 1
    assert report.findings[0].finding_type == "UNBOUNDED_TRUE_LOOP"

    scorecard_item = evaluate_loop_termination(report)
    assert scorecard_item.status == "FAIL"


def test_safe_loops_pass_evaluation(tmp_path: Path) -> None:
    """Safe loops with else branch, fallthrough advance, or break pass cleanly."""
    file1 = tmp_path / "safe1.py"
    file1.write_text(SAFE_LOOP_WITH_ELSE, encoding="utf-8")

    file2 = tmp_path / "safe2.py"
    file2.write_text(SAFE_LOOP_WITH_FALLTHROUGH_ADVANCE, encoding="utf-8")

    file3 = tmp_path / "safe3.py"
    file3.write_text(SAFE_WHILE_TRUE_WITH_BREAK, encoding="utf-8")

    scanner = LoopTerminationScanner(repo_root=tmp_path)
    report = scanner.scan(changed_files=["safe1.py", "safe2.py", "safe3.py"])

    assert report.has_critical_findings is False
    assert len(report.findings) == 0

    scorecard_item = evaluate_loop_termination(report)
    assert scorecard_item.status == "PASS"


def test_scan_diff_hunks_detects_non_advancing_loop() -> None:
    """Verify loop scanner detects non-advancing loops from raw diff hunks."""
    diff_text = """diff --git a/src/webhook_agent/review/writeback_policy.py b/src/webhook_agent/review/writeback_policy.py
--- a/src/webhook_agent/review/writeback_policy.py
+++ b/src/webhook_agent/review/writeback_policy.py
@@ -48,6 +48,15 @@ class CommentRateLimiter:
+    def purge_stale_records(self, max_age: float = 3600.0) -> int:
+        now = 1000.0
+        cutoff = now - max_age
+        for key, timestamps in list(self._history.items()):
+            idx = 0
+            while idx < len(timestamps):
+                if timestamps[idx] < cutoff:
+                    timestamps.pop(idx)
+        return 0
"""
    scanner = LoopTerminationScanner()
    report = scanner.scan(diff_text=diff_text)

    assert report.has_critical_findings is True
    assert report.findings[0].finding_type == "NON_ADVANCING_LOOP"
