"""Unit tests for deterministic line-citation repair."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from webhook_agent.review.citation_repair import (
    find_snippet_line,
    repair_line_citations,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]

PATH = "src/webhook_agent/core/execution.py"


def _execution_like_file() -> str:
    """Rebuild the #415 file layout that produced the drifted citations."""
    lines = [f"# filler {index}" for index in range(1, 401)]
    lines[103] = "    if _has_tool_calls(event):"  # 104
    lines[185] = "                if _is_final_response_event(event):"  # 186
    lines[186] = "                    emitted_texts.extend(text_parts)"  # 187
    lines[364] = "    if candidate == prior_state.get(_CONVERSATIONAL_REPLY_STATE_KEY):"  # 365
    lines[365] = '        return ""'  # 366
    lines[387] = '    full_reply = (state_reply or "").strip()'  # 388
    return "\n".join(lines)


def _loader(content: str | None = None) -> Callable[[str], str | None]:
    payload = _execution_like_file() if content is None else content

    def load(path: str) -> str | None:
        return payload if path == PATH else None

    return load


REVIEW_BODY = (
    f"- [x] {PATH}:187: 'if _is_final_response_event(event): "
    "emitted_texts.extend(text_parts)' verified by test_a\n"
    f"- [x] {PATH}:367: 'if candidate == prior_state.get(_CONVERSATIONAL_REPLY_STATE_KEY): "
    'return ""\' verified by test_b\n'
    f"- [x] {PATH}:387: 'full_reply = (state_reply or \"\").strip()' verified by test_c\n"
    f"- [x] {PATH}:104: 'if _has_tool_calls(event):' verified by test_d\n"
)


def test_repairs_multiline_and_single_line_drift() -> None:
    """The three real drifts from review 5608264918 are each corrected."""
    repaired, repairs = repair_line_citations(REVIEW_BODY, _loader())

    assert [(r.original_line, r.repaired_line) for r in repairs] == [
        (187, 186),
        (367, 365),
        (387, 388),
    ]
    assert ":186:" in repaired
    assert ":365:" in repaired
    assert ":388:" in repaired


def test_leaves_correct_citations_untouched() -> None:
    """A citation that already matches its quote is not rewritten."""
    body = f"- [x] {PATH}:104: 'if _has_tool_calls(event):' verified by test_d"

    repaired, repairs = repair_line_citations(body, _loader())

    assert repairs == []
    assert repaired == body


def test_repair_is_idempotent() -> None:
    """Running the repair twice changes nothing the second time."""
    once, _ = repair_line_citations(REVIEW_BODY, _loader())
    twice, repairs = repair_line_citations(once, _loader())

    assert twice == once
    assert repairs == []


@pytest.mark.parametrize(
    "body",
    [
        f"- [x] {PATH}:12 no quote at all",
        "- [x] src/app.py:12: 'a plain english sentence here'",
        "- [x] missing/file.py:12: 'if x: y()'",
        f"- [x] {PATH}:99999: 'if _has_tool_calls(event):'",
    ],
    ids=["no-snippet", "prose-quote", "unknown-path", "past-eof"],
)
def test_leaves_unrepairable_citations_alone(body: str) -> None:
    """Nothing is guessed at when the signal is missing or out of range."""
    repaired, repairs = repair_line_citations(body, _loader())

    assert repairs == []
    assert repaired == body


def test_ambiguous_occurrences_outside_drift_are_left_alone() -> None:
    """Two distant copies of the same snippet make the target unknowable."""
    lines = [f"# filler {index}" for index in range(1, 201)]
    lines[9] = "    if cache_hit: return None"
    lines[189] = "    if cache_hit: return None"
    body = "- [x] src/app.py:120: 'if cache_hit: return None'"

    repaired, repairs = repair_line_citations(body, _loader("\n".join(lines)))

    assert repairs == []
    assert repaired == body


def test_single_distant_occurrence_is_still_repaired() -> None:
    """One unambiguous copy is evidence, however far the number drifted."""
    lines = [f"# filler {index}" for index in range(1, 201)]
    lines[179] = "    if cache_hit: return None"
    body = "- [x] src/app.py:20: 'if cache_hit: return None'"

    _, repairs = repair_line_citations(body, _loader("\n".join(lines)))

    assert [(r.original_line, r.repaired_line) for r in repairs] == [(20, 180)]


def test_malformed_loader_output_is_tolerated() -> None:
    """A loader returning None (missing file) leaves the citation untouched."""
    body = f"- [x] {PATH}:187: 'if _is_final_response_event(event):'"
    _, repairs = repair_line_citations(body, lambda _path: None)

    assert repairs == []


def test_find_snippet_line_prefers_nearest_anchor() -> None:
    """With several nearby matches, the closest to the anchor wins."""
    lines = ["# a", "target_line()", "# b", "target_line()", "# c"]

    assert find_snippet_line(lines, "target_line()", anchor=4) == 4
    assert find_snippet_line(lines, "target_line()", anchor=2) == 2


def test_find_snippet_line_handles_multiline_quote() -> None:
    """A quote spanning lines resolves to the line where the quote starts."""
    lines = ["# a", "if ready:", "    run()", "# b"]

    assert find_snippet_line(lines, "if ready: run()", anchor=3) == 2
