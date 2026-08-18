import pytest
from unittest.mock import MagicMock, patch
from google.adk.agents.context import Context
from webhook_agent.tools.auto_fix_feedback import (
    auto_fix_pr_feedback,
    parse_review_feedback_items,
)

pytestmark = [pytest.mark.integration, pytest.mark.webhook_agent]


def test_parse_review_feedback_items():
    review_body = """
### 5. Key Issues & Action Items
* `src/auth.py:L42` Missing null check on API response
* `src/webhook_agent/processor.py:120` Non-UTF8 string slice boundary
"""
    items = parse_review_feedback_items(review_body)
    assert len(items) == 2
    assert items[0]["path"] == "src/auth.py"
    assert items[0]["line"] == "42"
    assert items[0]["description"] == "Missing null check on API response"
    assert items[1]["path"] == "src/webhook_agent/processor.py"
    assert items[1]["line"] == "120"


def test_auto_fix_disabled_by_policy(monkeypatch):
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "0")
    ctx = MagicMock(spec=Context)
    res = auto_fix_pr_feedback(ctx, pr_number=123)
    assert "disabled by policy" in res


@patch("github.Github")
def test_auto_fix_no_bot_reviews(mock_github, monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_AUTOMATED_MUTATIONS", "1")

    mock_gh_inst = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()

    mock_github.return_value = mock_gh_inst
    mock_gh_inst.get_repo.return_value = mock_repo
    mock_repo.get_pull.return_value = mock_pr
    mock_pr.get_reviews.return_value = []

    ctx = MagicMock(spec=Context)

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value.returncode = 0
        res = auto_fix_pr_feedback(ctx, pr_number=123, repo_root=tmp_path)
        assert "No prior reviews found" in res
