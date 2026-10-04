"""Unit tests for Cross-File Symbol Dependency & Breaking Signature Graph (SymbolImpactAnalyzer)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from webhook_agent.logic.symbol_graph import (
    CallSiteSpec,
    CallSiteVisitor,
    SignatureDiff,
    SignatureSpec,
    SymbolDefinitionVisitor,
    SymbolImpactAnalyzer,
    compare_signatures,
    extract_signatures_from_code,
    verify_call_site_compatibility,
)
from webhook_agent.tools.symbol_tools import check_symbol_impact

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_extract_signatures_standard_and_async():
    code = """
def calculate_metrics(data, factor=1.5):
    return data * factor

async def fetch_user(user_id: str, timeout: int = 10) -> dict:
    return {"id": user_id}
"""
    sigs = extract_signatures_from_code(code, file_path="src/metrics.py")
    assert "calculate_metrics" in sigs
    assert "fetch_user" in sigs

    m_sig = sigs["calculate_metrics"]
    assert m_sig.symbol_name == "calculate_metrics"
    assert m_sig.min_positional_args == 1
    assert m_sig.max_positional_args == 2
    assert len(m_sig.parameters) == 2
    assert m_sig.parameters[0].name == "data"
    assert not m_sig.parameters[0].has_default
    assert m_sig.parameters[1].name == "factor"
    assert m_sig.parameters[1].has_default

    u_sig = sigs["fetch_user"]
    assert u_sig.symbol_name == "fetch_user"
    assert u_sig.min_positional_args == 1
    assert u_sig.max_positional_args == 2


def test_extract_signatures_class_methods():
    code = """
class MetricsEngine:
    def process(self, value, multiplier=2):
        return value * multiplier

    @classmethod
    def create(cls, config):
        return cls()
"""
    sigs = extract_signatures_from_code(code, file_path="src/engine.py")
    assert "process" in sigs
    assert "MetricsEngine.process" in sigs

    p_sig = sigs["process"]
    assert p_sig.is_method
    assert p_sig.first_arg_is_self
    assert p_sig.min_positional_args == 2  # self + value
    assert p_sig.max_positional_args == 3  # self + value + multiplier


def test_extract_signatures_keyword_only_and_varargs():
    code = """
def audit_event(event_type, *args, strict: bool, max_depth: int = 5, **kwargs):
    pass
"""
    sigs = extract_signatures_from_code(code, file_path="src/audit.py")
    sig = sigs["audit_event"]
    assert sig.has_var_positional
    assert sig.has_var_keyword
    assert sig.min_positional_args == 1
    assert sig.max_positional_args is None
    assert sig.required_kwonly_args == frozenset({"strict"})


def test_compare_signatures_non_breaking_addition():
    old_code = "def send_email(to, subject): pass"
    new_code = "def send_email(to, subject, cc=None): pass"

    old_sig = extract_signatures_from_code(old_code)["send_email"]
    new_sig = extract_signatures_from_code(new_code)["send_email"]

    diff = compare_signatures(old_sig, new_sig)
    assert not diff.is_breaking
    assert not diff.reasons


def test_compare_signatures_breaking_required_param_added():
    old_code = "def send_email(to, subject): pass"
    new_code = "def send_email(to, subject, body): pass"

    old_sig = extract_signatures_from_code(old_code)["send_email"]
    new_sig = extract_signatures_from_code(new_code)["send_email"]

    diff = compare_signatures(old_sig, new_sig)
    assert diff.is_breaking
    assert any("Required parameter 'body' was added" in r for r in diff.reasons)


def test_compare_signatures_breaking_param_removed():
    old_code = "def send_email(to, subject, body): pass"
    new_code = "def send_email(to, subject): pass"

    old_sig = extract_signatures_from_code(old_code)["send_email"]
    new_sig = extract_signatures_from_code(new_code)["send_email"]

    diff = compare_signatures(old_sig, new_sig)
    assert diff.is_breaking
    assert any("Parameter 'body' was removed" in r for r in diff.reasons)


def test_compare_signatures_breaking_default_removed():
    old_code = "def send_email(to, subject='Hello'): pass"
    new_code = "def send_email(to, subject): pass"

    old_sig = extract_signatures_from_code(old_code)["send_email"]
    new_sig = extract_signatures_from_code(new_code)["send_email"]

    diff = compare_signatures(old_sig, new_sig)
    assert diff.is_breaking
    assert any("Default value for parameter 'subject' was removed" in r for r in diff.reasons)


def test_compare_signatures_breaking_symbol_deleted():
    old_code = "def send_email(to): pass"
    old_sig = extract_signatures_from_code(old_code)["send_email"]

    diff = compare_signatures(old_sig, None)
    assert diff.is_breaking
    assert any("Symbol 'send_email' was removed or renamed" in r for r in diff.reasons)


def test_verify_call_site_compatibility_valid():
    code = "def process(a, b, c=10): pass"
    sig = extract_signatures_from_code(code)["process"]

    # Valid positional call: process(1, 2)
    cs1 = CallSiteSpec("test.py", 10, 0, "process", 2, frozenset(), False, False)
    assert verify_call_site_compatibility(cs1, sig) is None

    # Valid call with kwarg: process(1, b=2)
    cs2 = CallSiteSpec("test.py", 11, 0, "process", 1, frozenset({"b"}), False, False)
    assert verify_call_site_compatibility(cs2, sig) is None


def test_verify_call_site_compatibility_missing_positional():
    code = "def process(a, b, c=10): pass"
    sig = extract_signatures_from_code(code)["process"]

    # Call missing b: process(1)
    cs = CallSiteSpec("test.py", 10, 0, "process", 1, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "MISSING_REQUIRED_ARG"
    assert "Missing 1 required positional argument" in broken.description


def test_verify_call_site_compatibility_too_many_args():
    code = "def process(a, b): pass"
    sig = extract_signatures_from_code(code)["process"]

    # Call with 3 args: process(1, 2, 3)
    cs = CallSiteSpec("test.py", 10, 0, "process", 3, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "TOO_MANY_ARGS"
    assert "Too many positional arguments" in broken.description


def test_verify_call_site_compatibility_missing_kwonly():
    code = "def process(a, *, strict: bool): pass"
    sig = extract_signatures_from_code(code)["process"]

    # Call missing strict: process(1)
    cs = CallSiteSpec("test.py", 10, 0, "process", 1, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "MISSING_KEYWORD_ARG"
    assert "Missing required keyword-only argument" in broken.description


def test_verify_call_site_compatibility_unexpected_kw():
    code = "def process(a, b=2): pass"
    sig = extract_signatures_from_code(code)["process"]

    # Call with unexpected unknown_arg=True
    cs = CallSiteSpec("test.py", 10, 0, "process", 1, frozenset({"unknown_arg"}), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "UNEXPECTED_KEYWORD"
    assert "Unexpected keyword argument(s)" in broken.description


def test_verify_call_site_compatibility_method_with_self():
    code = """
class Service:
    def execute(self, payload, retries=3): pass
"""
    sig = extract_signatures_from_code(code)["execute"]

    # Method call: s.execute(data) -> 1 arg supplied on instance, satisfies (self + payload)
    cs = CallSiteSpec("test.py", 10, 0, "execute", 1, frozenset(), False, False)
    assert verify_call_site_compatibility(cs, sig) is None

    # Method call with 0 args: s.execute() -> missing payload!
    cs_bad = CallSiteSpec("test.py", 11, 0, "execute", 0, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs_bad, sig)
    assert broken is not None
    assert broken.error_type == "MISSING_REQUIRED_ARG"


def test_symbol_impact_analyzer_real_repo():
    analyzer = SymbolImpactAnalyzer()
    # Check a symbol defined in ast_tools.py
    report = analyzer.analyze_symbol(
        file_path="src/webhook_agent/tools/ast_tools.py",
        symbol_name="verify_python_ast",
    )
    assert report.symbol_name == "verify_python_ast"
    # verify_python_ast is called in agent_definition.py and test files
    assert report.total_call_sites_found > 0
    # No broken call sites exist in repository
    assert len(report.broken_call_sites) == 0
    md = report.to_markdown()
    assert "verify_python_ast" in md
    assert "All External Call Sites Compatible" in md


def test_check_symbol_impact_tool():
    res = check_symbol_impact(
        file_path="src/webhook_agent/tools/ast_tools.py",
        symbol_name="verify_python_ast",
    )
    assert "Symbol: `verify_python_ast`" in res
    assert "External Call Sites Checked" in res
