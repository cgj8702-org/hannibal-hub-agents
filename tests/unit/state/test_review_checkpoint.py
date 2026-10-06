"""Unit tests for durable review checkpointing with 14-day sliding TTL."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from webhook_agent.state.review_checkpoint import (
    ReviewCheckpointManager,
    _safe_doc_id,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_safe_doc_id():
    """Verify document ID replaces slashes with double underscores."""
    doc_id = _safe_doc_id("cgj8702-org/hannibal-hub-agents", 226, "abc1234")
    assert doc_id == "cgj8702-org__hannibal-hub-agents__226__abc1234"
    assert "/" not in doc_id


def test_checkpoint_save_and_retrieve():
    """Verify saving a checkpoint in local memory and retrieving it."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    success = mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="pull_request.opened",
        precompiled_dossier="### Dossier findings",
        pr_diff="diff --git a/foo.py b/foo.py",
        changed_files=["foo.py"],
        status="pending",
    )
    assert success is True

    checkpoint = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert checkpoint is not None
    assert checkpoint["status"] == "pending"
    assert checkpoint["precompiled_dossier"] == "### Dossier findings"
    assert checkpoint["pr_diff"] == "diff --git a/foo.py b/foo.py"
    assert checkpoint["changed_files"] == ["foo.py"]
    assert checkpoint["attempts"] == 1


def test_checkpoint_mark_rate_limited():
    """Verify marking a checkpoint rate-limited updates status and error."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="pull_request.synchronize",
        precompiled_dossier="dossier",
    )

    mgr.mark_rate_limited("owner/repo", 10, "sha123", "429 Resource Exhausted")
    checkpoint = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert checkpoint is not None
    assert checkpoint["status"] == "rate_limited"
    assert checkpoint["last_error"] == "429 Resource Exhausted"


def test_checkpoint_mark_completed():
    """Verify marking a checkpoint completed."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="pull_request.opened",
        precompiled_dossier="dossier",
    )

    mgr.mark_completed("owner/repo", 10, "sha123")
    checkpoint = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert checkpoint is not None
    assert checkpoint["status"] == "completed"


def test_checkpoint_expiration():
    """Verify expired checkpoints are ignored."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="pull_request.opened",
        precompiled_dossier="dossier",
    )

    # Force expiration in memory cache
    doc_id = _safe_doc_id("owner/repo", 10, "sha123")
    mgr._local_cache[doc_id]["expire_at_ts"] = datetime.datetime.now(datetime.UTC).timestamp() - 100

    checkpoint = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert checkpoint is None


def test_checkpoint_firestore_integration():
    """Verify checkpoint interactions when Firestore client is mocked."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    mock_db = MagicMock()
    mock_doc = MagicMock()
    mock_doc.exists = True
    mock_doc.to_dict.return_value = {
        "repo": "owner/repo",
        "pr_number": 42,
        "head_sha": "abc999",
        "status": "pending",
        "precompiled_dossier": "cached ast",
        "expire_at": datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=14),
    }
    mock_db.collection.return_value.document.return_value.get.return_value = mock_doc
    mgr._db = mock_db
    mgr._initialized = True

    checkpoint = mgr.get_checkpoint("owner/repo", 42, "abc999")
    assert checkpoint is not None
    assert checkpoint["precompiled_dossier"] == "cached ast"
    assert checkpoint["status"] == "pending"

    # Test save with Firestore client
    doc_ref_mock = mock_db.collection.return_value.document.return_value
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=42,
        head_sha="abc999",
        canonical="pull_request.opened",
        precompiled_dossier="updated dossier",
    )
    assert doc_ref_mock.set.called

    # Test mark rate limited with Firestore client
    doc_ref_mock.set.reset_mock()
    mgr.mark_rate_limited("owner/repo", 42, "abc999", "503 Service Unavailable")
    assert doc_ref_mock.set.called

    # Test mark completed with Firestore client
    doc_ref_mock.set.reset_mock()
    mgr.mark_completed("owner/repo", 42, "abc999")
    assert doc_ref_mock.set.called


def test_checkpoint_size_guard_truncation():
    """Verify oversized diff and dossier are truncated to protect Firestore 1 MiB limit."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    huge_diff = "D" * (450 * 1024)
    huge_dossier = "P" * (550 * 1024)

    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="pull_request.opened",
        precompiled_dossier=huge_dossier,
        pr_diff=huge_diff,
    )

    checkpoint = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert checkpoint is not None
    assert len(checkpoint["pr_diff"].encode("utf-8")) < 450 * 1024
    assert "[TRUNCATED: PR diff exceeded 400 KiB document size guard]" in checkpoint["pr_diff"]
    assert len(checkpoint["precompiled_dossier"].encode("utf-8")) < 550 * 1024
    assert (
        "[TRUNCATED: Precompiled dossier exceeded 500 KiB document size guard]"
        in checkpoint["precompiled_dossier"]
    )


def test_checkpoint_never_downgrade_completed_or_rate_limited():
    """Verify save_checkpoint preserves completed or rate_limited status even if pending is passed."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)

    # 1. Start with completed
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=1,
        head_sha="sha1",
        canonical="pull_request.opened",
        status="completed",
    )
    # Subsequent attempt tries to save as pending
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=1,
        head_sha="sha1",
        canonical="pull_request.synchronize",
        status="pending",
    )
    cp1 = mgr.get_checkpoint("owner/repo", 1, "sha1")
    assert cp1 is not None
    assert cp1["status"] == "completed"

    # 2. Start with rate_limited
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=2,
        head_sha="sha2",
        canonical="pull_request.opened",
        status="pending",
    )
    mgr.mark_rate_limited("owner/repo", 2, "sha2", "Quota exceeded", retry_after_seconds=30.0)
    # Subsequent attempt tries to save as pending
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=2,
        head_sha="sha2",
        canonical="pull_request.synchronize",
        status="pending",
    )
    cp2 = mgr.get_checkpoint("owner/repo", 2, "sha2")
    assert cp2 is not None
    assert cp2["status"] == "rate_limited"
    assert "resume_after" in cp2


def test_checkpoint_mark_rate_limited_with_retry_after():
    """Verify mark_rate_limited computes resume_after timestamp based on retry_after_seconds."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="pull_request.opened",
    )

    now = datetime.datetime.now(datetime.UTC)
    mgr.mark_rate_limited(
        "owner/repo",
        10,
        "sha123",
        "ResourceExhausted: 429",
        retry_after_seconds=120.0,
    )
    cp = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert cp is not None
    assert cp["status"] == "rate_limited"
    assert "resume_after" in cp
    assert cp["resume_after_ts"] >= now.timestamp() + 115.0


def test_checkpoint_save_additional_metadata():
    """Verify delivery_id, sender, comment_body, installation_id are saved."""
    mgr = ReviewCheckpointManager(collection_name="test_checkpoints", sliding_ttl_days=14)
    mgr.save_checkpoint(
        repo="owner/repo",
        pr_number=10,
        head_sha="sha123",
        canonical="issue_comment.created",
        delivery_id="del-12345",
        sender="octocat",
        comment_body="/review",
        installation_id=98765,
    )
    cp = mgr.get_checkpoint("owner/repo", 10, "sha123")
    assert cp is not None
    assert cp["delivery_id"] == "del-12345"
    assert cp["sender"] == "octocat"
    assert cp["comment_body"] == "/review"
    assert cp["installation_id"] == 98765
