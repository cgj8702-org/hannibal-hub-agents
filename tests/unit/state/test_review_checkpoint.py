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
