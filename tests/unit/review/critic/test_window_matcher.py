"""Unit tests for WindowMatcher ASCII skeleton comparator and drift snapping."""

from __future__ import annotations

import pytest

from webhook_agent.review.critic.window_matcher import WindowMatcher, norm

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_norm_ascii_skeleton() -> None:
    # 1. Strips unicode escape sequences
    assert norm(r"def test_foo():  \u26a0") == "deftest_foo():"

    # 2. Strips unicode emojis and accents via NFKC + isascii
    assert norm("def process_event(event):  # ⚠️ alert") == "defprocess_event(event):#alert"

    # 3. Discards whitespace completely
    assert norm("  return    True\n\t") == "returnTrue"

    # 4. None / empty handling
    assert norm("") == ""


def test_window_matcher_exact_match() -> None:
    matcher = WindowMatcher()
    diff_lines = {
        10: "def calculate_total(items):",
        11: "    total = 0",
        12: "    for item in items:",
        13: "        total += item.price",
        14: "    return total",
    }
    window = """
    10: def calculate_total(items):
    11:     total = 0
    12:     for item in items:
    """
    verified, line, reason = matcher.check_window(window, 11, diff_lines)
    assert verified is True
    assert line == 11
    assert "window matches the diff" in reason


def test_window_matcher_snaps_line_outside_own_window() -> None:
    matcher = WindowMatcher()
    diff_lines = {
        10: "def calculate_total(items):",
        11: "    total = 0",
        12: "    for item in items:",
    }
    window = """
    10: def calculate_total(items):
    11:     total = 0
    """
    # Finding cited line 20, but window only covers lines 10-11
    verified, line, reason = matcher.check_window(window, 20, diff_lines)
    assert verified is True
    assert line == 11  # Closest line in window
    assert "moved to 11" in reason


def test_window_matcher_snaps_line_drift() -> None:
    matcher = WindowMatcher(max_drift=5)
    # The diff actually has the code at lines 15-17
    diff_lines = {
        15: "def calculate_total(items):",
        16: "    total = 0",
        17: "    for item in items:",
    }
    # Model's hand-counted arithmetic thought it was at lines 12-14 (+3 offset)
    window = """
    12: def calculate_total(items):
    13:     total = 0
    14:     for item in items:
    """
    verified, line, reason = matcher.check_window(window, 13, diff_lines)
    assert verified is True
    assert line == 16  # Snapped by +3
    assert "window matched +3 lines away" in reason


def test_window_matcher_drops_fabricated_code() -> None:
    matcher = WindowMatcher()
    diff_lines = {
        10: "def calculate_total(items):",
        11: "    total = 0",
    }
    # Fabricated code never present in diff
    window = """
    10: def connect_to_remote_cluster(secret_token):
    11:     return cluster.authenticate(secret_token)
    """
    verified, line, reason = matcher.check_window(window, 10, diff_lines)
    assert verified is False
    assert line == 10
    assert "window says" in reason


def test_window_matcher_handles_trailing_ellipsis() -> None:
    matcher = WindowMatcher()
    diff_lines = {
        10: "def calculate_total(items, discount=0.0, tax=0.0):",
    }
    # Model abbreviated long line with '...' or unicode ellipsis
    window_ascii_dots = "10: def calculate_total(items, discount=0.0..."
    window_unicode_ellipsis = "10: def calculate_total(items, discount=0.0…"

    verified1, _, _ = matcher.check_window(window_ascii_dots, 10, diff_lines)
    assert verified1 is True

    verified2, _, _ = matcher.check_window(window_unicode_ellipsis, 10, diff_lines)
    assert verified2 is True
