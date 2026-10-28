"""Unit tests for the Jules REST API client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from webhook_agent.github.jules_api import (
    approve_plan,
    extract_session_id,
    get_session,
    list_activities,
    send_message,
    session_state,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


class TestExtractSessionId:
    def test_extracts_from_jules_url(self):
        assert extract_session_id("Check out https://jules.google.com/session/abc123") == "abc123"

    def test_extracts_from_sessions_path(self):
        assert extract_session_id("Session: sessions/31415926535897932384") == (
            "31415926535897932384"
        )

    def test_extracts_bare_id(self):
        assert extract_session_id("Session ID: xyz789") == "xyz789"

    def test_extracts_from_task_url(self):
        assert (
            extract_session_id("Task URL: https://jules.google.com/task/task-abc123")
            == "task-abc123"
        )

    def test_extracts_uuid_session(self):
        assert (
            extract_session_id("UUID session: 12345678-1234-1234-1234-123456789abc")
            == "12345678-1234-1234-1234-123456789abc"
        )

    def test_rejects_repo_and_org_names(self):
        assert extract_session_id("Reviewing PR for cgj8702-org/hannibal-hub-agents...") is None
        assert extract_session_id("https://github.com/cgj8702-org/repo") is None

    def test_returns_none_when_no_match(self):
        assert extract_session_id("No session here") is None
        assert extract_session_id("") is None
        assert extract_session_id(None) is None


class TestJulesApiClient:
    def test_get_session_returns_session_dict(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "name": "sessions/abc123",
            "id": "abc123",
            "state": "AWAITING_PLAN_APPROVAL",
        }
        with patch("webhook_agent.github.jules_api._request", return_value=mock_resp) as mock_req:
            result = get_session("abc123")
            assert result == mock_resp.json.return_value
            mock_req.assert_called_once_with("GET", "/sessions/abc123")

    def test_get_session_returns_none_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        assert get_session("abc123") is None

    def test_list_activities_returns_activity_list(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "activities": [{"id": "act1", "originator": "agent", "description": "Plan generated"}]
        }
        with patch("webhook_agent.github.jules_api._request", return_value=mock_resp):
            result = list_activities("abc123")
            assert len(result) == 1
            assert result[0]["id"] == "act1"

    def test_list_activities_returns_empty_on_failure(self):
        with patch("webhook_agent.github.jules_api._request", return_value=None):
            assert list_activities("abc123") == []

    def test_approve_plan_returns_true_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("webhook_agent.github.jules_api._request", return_value=mock_resp):
            assert approve_plan("abc123") is True

    def test_approve_plan_returns_failure_on_error(self):
        with patch("webhook_agent.github.jules_api._request", return_value=None):
            assert approve_plan("abc123") is False

    def test_send_message_returns_true_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("webhook_agent.github.jules_api._request", return_value=mock_resp) as mock_req:
            assert send_message("abc123", "Please proceed") is True
            mock_req.assert_called_once_with(
                "POST",
                "/sessions/abc123:sendMessage",
                json={"prompt": "Please proceed"},
            )

    def test_session_state_returns_state_string(self):
        with patch(
            "webhook_agent.github.jules_api.get_session",
            return_value={"state": "IN_PROGRESS"},
        ):
            assert session_state("abc123") == "IN_PROGRESS"

    def test_session_state_returns_none_on_failure(self):
        with patch("webhook_agent.github.jules_api.get_session", return_value=None):
            assert session_state("abc123") is None

    def test_request_skips_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        from webhook_agent.github.jules_api import _request

        assert _request("GET", "/sessions/abc123") is None


class TestHandleJulesLabel:
    def test_handle_jules_label_with_existing_jules_comment(self):
        from webhook_agent.processor import WebhookProcessor

        processor = WebhookProcessor.__new__(WebhookProcessor)
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value
        mock_comment = MagicMock()
        mock_comment.user.raw_data = {"login": "google-labs-jules[bot]"}
        mock_comment.body = "Jules is on it https://jules.google.com/task/12345678"
        mock_issue.get_comments.return_value = [mock_comment]
        processor._gh = mock_gh
        processor._gh_token_expires_at = None

        payload = {
            "raw_payload": {
                "label": {"name": "jules"},
                "sender": {"login": "cgj8702"},
                "issue": {"number": 456},
                "repository": {"full_name": "owner/repo"},
            }
        }

        with patch.object(
            processor, "_interact_with_jules_session", return_value=True
        ) as mock_interact:
            handled = processor.handle_jules_label(payload)
            assert handled is True
            mock_interact.assert_called_once_with("12345678")

    def test_handle_jules_label_before_jules_comments_suppresses_triage(self):
        from webhook_agent.processor import WebhookProcessor

        processor = WebhookProcessor.__new__(WebhookProcessor)
        mock_gh = MagicMock()
        mock_repo = mock_gh.get_repo.return_value
        mock_issue = mock_repo.get_issue.return_value
        mock_issue.get_comments.return_value = []
        processor._gh = mock_gh
        processor._gh_token_expires_at = None

        payload = {
            "raw_payload": {
                "label": {"name": "jules"},
                "sender": {"login": "cgj8702"},
                "issue": {"number": 456},
                "repository": {"full_name": "owner/repo"},
            }
        }

        with patch.object(processor, "_interact_with_jules_session") as mock_interact:
            handled = processor.handle_jules_label(payload)
            assert handled is True
            mock_interact.assert_not_called()

    def test_handle_jules_label_ignores_non_owner(self):
        from webhook_agent.processor import WebhookProcessor

        processor = WebhookProcessor.__new__(WebhookProcessor)
        payload = {
            "raw_payload": {
                "label": {"name": "jules"},
                "sender": {"login": "random-user"},
                "issue": {"number": 456},
                "repository": {"full_name": "owner/repo"},
            }
        }
        assert processor.handle_jules_label(payload) is False
