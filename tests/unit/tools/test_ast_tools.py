"""Unit tests for AST & Syntax Integrity Verifier Tool and Gemini 3+ Context Caching."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents.context_cache_config import ContextCacheConfig

from webhook_agent.core.callbacks import after_model_callback, before_model_callback
from webhook_agent.models.model_chain import is_gemini_3_plus
from webhook_agent.tools.ast_tools import (
    StructuralDefectVisitor,
    _strip_diff_prefix,
    verify_python_ast,
)

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_is_gemini_3_plus_model_filter():
    """Verify Gemini 3+ detection and strict exclusion of Gemma and legacy models."""
    assert is_gemini_3_plus("gemini-3.5-flash-lite") is True
    assert is_gemini_3_plus("gemini-3.1-flash-lite") is True
    assert is_gemini_3_plus("gemini-3.8-flash") is True
    assert is_gemini_3_plus("gemini-3.7-flash") is True
    assert is_gemini_3_plus("gemini-3.0-pro") is True
    assert is_gemini_3_plus("models/gemini-3.5-flash-lite") is True
    assert is_gemini_3_plus("gemini-4.0-flash") is True

    assert is_gemini_3_plus("gemma-4-31b-it") is False
    assert is_gemini_3_plus("gemma-4-26b-a4b-it") is False
    assert is_gemini_3_plus("models/gemma-4-31b-it") is False
    assert is_gemini_3_plus("gemma-3-12b-it") is False

    assert is_gemini_3_plus("gemini-2.5-flash") is False
    assert is_gemini_3_plus("gemini-2.5-flash-lite") is False
    assert is_gemini_3_plus("gemini-2.0-flash") is False
    assert is_gemini_3_plus("gemini-1.5-pro") is False

    assert is_gemini_3_plus(None) is False
    assert is_gemini_3_plus("") is False
    assert is_gemini_3_plus("unknown-model") is False


@pytest.mark.anyio
async def test_before_model_callback_disables_cache_for_gemma():
    """Verify that before_model_callback nullifies cache_config and thinking_config on Gemma models."""
    ctx = MagicMock()
    ctx.state = {"active_tier": "free"}

    req = MagicMock()
    req.model = "gemma-4-31b-it"
    req.contents = ["Hello Gemma"]
    req.cache_config = ContextCacheConfig(min_tokens=4096)
    req.cache_metadata = MagicMock()
    req.cacheable_contents_token_count = 5000
    req.config = MagicMock()
    req.config.thinking_config = MagicMock()

    with patch("webhook_agent.core.callbacks.rpm_waiter.check_and_wait", new_callable=AsyncMock):
        await before_model_callback(ctx, req)

    assert req.cache_config is None
    assert req.cache_metadata is None
    assert req.cacheable_contents_token_count is None
    assert req.config.thinking_config is None


@pytest.mark.anyio
async def test_before_model_callback_enables_cache_for_gemini_3():
    """Verify that before_model_callback configures cache_config for Gemini 3+ models."""
    ctx = MagicMock()
    ctx.state = {"active_tier": "free"}

    req = MagicMock()
    req.model = "gemini-3.5-flash-lite"
    req.contents = ["Hello Gemini 3.5"]
    req.cache_config = None

    with patch("webhook_agent.core.callbacks.rpm_waiter.check_and_wait", new_callable=AsyncMock):
        await before_model_callback(ctx, req)

    assert req.cache_config is not None
    assert req.cache_config.min_tokens == 4096
    assert req.cache_config.ttl_seconds == 1800


@pytest.mark.anyio
async def test_after_model_callback_records_cached_content_tokens():
    """Verify that after_model_callback records cached_content_tokens in session state."""
    ctx = MagicMock()
    ctx.state = {"active_model": "gemini-3.5-flash-lite"}

    resp = MagicMock()
    resp.usage_metadata.total_token_count = 5000
    resp.usage_metadata.cached_content_token_count = 4200
    resp.content = None

    with patch(
        "webhook_agent.core.callbacks.rpm_waiter.record_actual_tokens", new_callable=AsyncMock
    ):
        await after_model_callback(ctx, resp)

    assert ctx.state["total_tokens"] == 5000
    assert ctx.state["cached_content_tokens"] == 4200


def test_verify_python_ast_valid_snippet():
    """Verify clean Python code passes AST verification with zero defects."""
    valid_code = """
def add(a: int, b: int) -> int:
    return a + b
"""
    result = verify_python_ast("src/math_ops.py", code_snippet=valid_code)
    assert "SYNTAX VALID" in result
    assert "Structural Defects: None detected." in result
    assert "Risk Score:" in result


def test_verify_python_ast_syntax_error():
    """Verify AST verifier catches syntax errors with exact line and column numbers."""
    broken_code = """
def broken_fn():
    if True
        print('missing colon')
"""
    result = verify_python_ast("src/broken.py", code_snippet=broken_code)
    assert "❌ SyntaxError in 'src/broken.py' at line 3" in result
    assert "Action: Review and repair the syntax error at line 3." in result


def test_verify_python_ast_structural_defects():
    """Verify AST verifier catches bare excepts, mutable defaults, and unreachable code."""
    defect_code = """
def risky_fn(items=[], config={}):
    try:
        return 1
        print("unreachable")
    except:
        pass
"""
    result = verify_python_ast("src/risky.py", code_snippet=defect_code)
    assert "SYNTAX VALID" in result
    assert "Bare 'except:' clause catches BaseException" in result
    assert "Mutable default argument (list) in function 'risky_fn'" in result
    assert "Mutable default argument (dict) in function 'risky_fn'" in result
    assert "Unreachable statement after return on line 4." in result


def test_verify_python_ast_file_on_disk(tmp_path):
    """Verify reading a Python file directly from disk including diff prefix stripping."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("x = 10\ny = 20\n", encoding="utf-8")

    res1 = verify_python_ast(str(test_file))
    assert "SYNTAX VALID" in res1

    res2 = verify_python_ast(f"b/{test_file}")
    assert "SYNTAX VALID" in res2


def test_verify_python_ast_diff_text_priority_over_disk(tmp_path):
    """Verify diff_text takes priority over file on disk (fixing P0-2 disk bleed)."""
    test_file = tmp_path / "target.py"
    test_file.write_text("def valid_disk():\n    return 42\n", encoding="utf-8")

    diff_text = (
        f"diff --git a/{test_file} b/{test_file}\n"
        f"--- a/{test_file}\n"
        f"+++ b/{test_file}\n"
        "@@ -1,2 +1,3 @@\n"
        " def valid_disk():\n"
        "+    if True\n"
        "     return 42\n"
    )

    result = verify_python_ast(str(test_file), diff_text=diff_text)
    assert "SyntaxError" in result


def test_verify_python_ast_two_hunk_diff_no_unreachable_warning(tmp_path):
    """Verify non-contiguous hunks do not report unreachable statements and use partial status."""
    test_file = "src/example.py"
    # Hunk 1 has 'return 42'. Hunk 2 has 'print("second hunk")'.
    # If stitched into one function body, 'print' follows 'return', which normally triggers an unreachable defect.
    diff_text = (
        f"diff --git a/{test_file} b/{test_file}\n"
        f"--- a/{test_file}\n"
        f"+++ b/{test_file}\n"
        "@@ -10,2 +10,3 @@\n"
        " def my_func():\n"
        "+    return 42\n"
        "@@ -50,2 +51,3 @@\n"
        "+    print('second hunk')\n"
        "     return 100\n"
    )
    result = verify_python_ast(test_file, diff_text=diff_text)
    assert "PARSE OK (partial diff view)" in result
    assert "SYNTAX VALID" not in result
    assert "Unreachable statement" not in result


def test_verify_python_ast_partial_diff_unparseable_neutral_note():
    """Verify unparseable non-contiguous diff returns a neutral note, not a SyntaxError alarm."""
    test_file = "src/partial_broken.py"
    diff_text = (
        f"diff --git a/{test_file} b/{test_file}\n"
        f"--- a/{test_file}\n"
        f"+++ b/{test_file}\n"
        "@@ -10,2 +10,3 @@\n"
        "+    if x >\n"
        "@@ -80,2 +81,3 @@\n"
        "+    return 42\n"
    )
    result = verify_python_ast(test_file, diff_text=diff_text)
    assert "Could not parse partial diff fragment; this is expected for hunks" in result
    assert "❌ SyntaxError" not in result
