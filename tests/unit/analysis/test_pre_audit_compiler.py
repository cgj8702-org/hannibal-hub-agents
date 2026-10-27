"""Unit tests for Pre-Audit Compiler (concurrent static analysis and dossier assembly)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from webhook_agent.analysis.pre_audit_compiler import (
    PreAuditDossier,
    compile_pre_audit_dossier,
)
from webhook_agent.environment.context import use_environment
from webhook_agent.environment.local import LocalEnvironment

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_compile_pre_audit_dossier_no_workspace() -> None:
    """Verify dossier assembly falls back gracefully when no active workspace exists."""
    event_data = {
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "action": "opened",
    }
    dossier = compile_pre_audit_dossier(
        changed_files=["src/worker.py"],
        pr_diff_text="diff --git a/src/worker.py b/src/worker.py\n+ # comment",
        event_data=event_data,
    )

    assert isinstance(dossier, PreAuditDossier)
    assert dossier.full_files_included == 0
    assert dossier.contract_scorecard.dimension == "Contract Integrity"
    assert dossier.contract_scorecard.status == "UNKNOWN"
    assert dossier.test_scorecard.dimension == "Test Coverage & Regressions"
    assert dossier.test_scorecard.status == "UNKNOWN"
    assert dossier.security_scorecard.dimension == "Security & Secrets"
    assert dossier.security_scorecard.status == "PASS"


def test_compile_pre_audit_dossier_ast_scan_tier_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify AST scan caps at 5 on free tier and 50 on paid tier."""
    py_files = [f"src/module_{i}.py" for i in range(60)]

    with patch("webhook_agent.analysis.pre_audit_compiler.verify_python_ast") as mock_ast:
        mock_ast.return_value = "AST verified successfully."

        # 1. Free Tier
        monkeypatch.setattr(
            "webhook_agent.analysis.pre_audit_compiler._resolve_tier", lambda: "free"
        )
        dossier_free = compile_pre_audit_dossier(
            changed_files=py_files,
            pr_diff_text="",
            event_data={},
        )
        assert mock_ast.call_count == 5
        assert dossier_free.ast_verified is True
        assert "Deterministic Pre-Audit Compiler Findings" in dossier_free.dossier_markdown

        # 2. Paid Tier
        mock_ast.reset_mock()
        monkeypatch.setattr(
            "webhook_agent.analysis.pre_audit_compiler._resolve_tier", lambda: "paid"
        )
        dossier_paid = compile_pre_audit_dossier(
            changed_files=py_files,
            pr_diff_text="",
            event_data={},
        )
        assert mock_ast.call_count == 50
        assert dossier_paid.ast_verified is True


def test_compile_pre_audit_dossier_paid_tier_full_files_inclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify paid tier embeds full source code of up to 10 modified Python files."""
    monkeypatch.setattr("webhook_agent.analysis.pre_audit_compiler._resolve_tier", lambda: "paid")

    # Create dummy source files
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    created_files = []
    for i in range(12):
        f = src_dir / f"file_{i}.py"
        f.write_text(f"# Content of file {i}\ndef func_{i}(): pass\n", encoding="utf-8")
        created_files.append(f"src/file_{i}.py")

    env = LocalEnvironment(tmp_path)
    with use_environment(env):
        dossier = compile_pre_audit_dossier(
            changed_files=created_files,
            pr_diff_text="",
            event_data={},
        )

    assert dossier.full_files_included == 10
    assert (
        "### 📂 Full Source File Context (Paid Tier High-Capacity Mode)" in dossier.dossier_markdown
    )
    assert "#### `src/file_0.py`" in dossier.dossier_markdown
    assert "def func_0(): pass" in dossier.dossier_markdown
    assert "#### `src/file_9.py`" in dossier.dossier_markdown
    # 11th and 12th should be omitted due to cap of 10
    assert "#### `src/file_10.py`" not in dossier.dossier_markdown


def test_compile_pre_audit_dossier_free_tier_skips_full_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify free tier does not embed full source file contents."""
    monkeypatch.setattr("webhook_agent.analysis.pre_audit_compiler._resolve_tier", lambda: "free")

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    f = src_dir / "worker.py"
    f.write_text("print('hello')", encoding="utf-8")

    env = LocalEnvironment(tmp_path)
    with use_environment(env):
        dossier = compile_pre_audit_dossier(
            changed_files=["src/worker.py"],
            pr_diff_text="",
            event_data={},
        )

    assert dossier.full_files_included == 0
    assert "Full Source File Context" not in dossier.dossier_markdown


def test_compile_pre_audit_dossier_baseline_loader() -> None:
    """Verify baseline loader closure handles GitHub client extraction and errors gracefully."""
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    mock_content = MagicMock()
    mock_content.decoded_content = b"def old_func(): return True\n"
    mock_repo.get_contents.side_effect = [
        mock_content,
        Exception("404 not found"),
    ]

    event_data = {
        "repository": {"full_name": "hannibal/hannibal-hub-agents"},
        "base_sha": "base_commit_sha_123",
        "gh_client": mock_gh,
    }

    # Pass symbol analyzer check with active workspace
    with patch("webhook_agent.analysis.pre_audit_compiler.SymbolImpactAnalyzer") as mock_sym_cls:
        mock_instance = MagicMock()
        mock_instance.analyze_modified_files.return_value = []
        mock_instance.parse_errors = []
        mock_sym_cls.return_value = mock_instance

        with patch("webhook_agent.analysis.pre_audit_compiler.get_active_environment") as mock_env:
            mock_env_obj = MagicMock()
            mock_env_obj.root = Path("/fake/root")
            mock_env.return_value = mock_env_obj

            dossier = compile_pre_audit_dossier(
                changed_files=["src/worker.py"],
                pr_diff_text="",
                event_data=event_data,
            )

        assert mock_instance.analyze_modified_files.called
        call_kwargs = mock_instance.analyze_modified_files.call_args[1]
        loader = call_kwargs["baseline_loader"]
        assert loader is not None

        # Call loader directly to verify success and error paths
        res1 = loader("src/worker.py")
        assert res1 == "def old_func(): return True\n"

        res2 = loader("src/missing.py")
        assert res2 is None
