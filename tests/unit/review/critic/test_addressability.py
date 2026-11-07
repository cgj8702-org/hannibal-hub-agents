"""Unit tests for AddressabilityChecker right-side diff bounds."""

from __future__ import annotations

import pytest

from webhook_agent.review.critic.addressability import AddressabilityChecker
from webhook_agent.review.schemas import IssueItem

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

SAMPLE_DIFF = """--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@ def init_app():
     setup_db()
+    register_routes()
     return app
--- a/src/utils.py
+++ b/src/utils.py
@@ -100,2 +100,3 @@ def helper():
+    new_util_helper()
     return True
"""


def test_addressability_checker_identifies_inline_lines() -> None:
    checker = AddressabilityChecker(SAMPLE_DIFF)

    # Added line 11 in src/app.py
    assert checker.is_inline_addressable("src/app.py", 11) is True
    # Strip prefix b/src/app.py
    assert checker.is_inline_addressable("b/src/app.py", 11) is True

    # Added line 100 in src/utils.py
    assert checker.is_inline_addressable("src/utils.py", 100) is True

    # Unmodified context line 10 or 12 in src/app.py is not an inline anchor
    assert checker.is_inline_addressable("src/app.py", 10) is False
    assert checker.is_inline_addressable("src/app.py", 12) is False

    # Line outside diff or file not changed
    assert checker.is_inline_addressable("src/app.py", 999) is False
    assert checker.is_inline_addressable("src/other.py", 11) is False
    assert checker.is_inline_addressable("", 11) is False
    assert checker.is_inline_addressable("src/app.py", None) is False


def test_addressability_checker_partitions_issues() -> None:
    checker = AddressabilityChecker(SAMPLE_DIFF)
    issues = [
        IssueItem(path="src/app.py", line=11, description="Inline issue on route registration"),
        IssueItem(path="src/app.py", line=10, description="Top-level note on setup_db context"),
        IssueItem(path="src/utils.py", line=100, description="Inline issue on new util"),
        IssueItem(path="src/utils.py", line=50, description="Top-level note on distant line"),
    ]

    inline, top_level = checker.partition_addressability(issues)

    assert len(inline) == 2
    assert [i.description for i in inline] == [
        "Inline issue on route registration",
        "Inline issue on new util",
    ]

    assert len(top_level) == 2
    assert [i.description for i in top_level] == [
        "Top-level note on setup_db context",
        "Top-level note on distant line",
    ]


def test_addressability_checker_gets_file_lines() -> None:
    checker = AddressabilityChecker(SAMPLE_DIFF)
    lines_app = checker.get_file_lines("src/app.py")
    assert 10 in lines_app
    assert "setup_db()" in lines_app[10]
    assert 11 in lines_app
    assert "register_routes()" in lines_app[11]
