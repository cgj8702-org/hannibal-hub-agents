"""Unit tests for PRClosedRegistry and AbortAgentExecution short-circuiting."""

from __future__ import annotations

import pytest
from webhook_agent.cancellation import AbortAgentExecution, pr_closed_registry
from webhook_agent.callbacks import _check_pr_closed_short_circuit


def test_pr_closed_registry_mark_and_check():
    """Verify marking a PR closed sets status in registry."""
    pr_closed_registry.mark_closed("cgj8702-org/test-repo", 101)
    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 101) is True
    assert pr_closed_registry.is_closed("cgj8702-org/test-repo", 102) is False


def test_check_pr_closed_short_circuit_raises():
    """Verify _check_pr_closed_short_circuit raises AbortAgentExecution when PR is closed."""
    pr_closed_registry.mark_closed("cgj8702-org/test-repo", 202)

    state = {
        "repo_full_name": "cgj8702-org/test-repo",
        "pr_number": 202,
    }

    with pytest.raises(AbortAgentExecution) as exc_info:
        _check_pr_closed_short_circuit(state)

    assert "closed or merged" in str(exc_info.value)
