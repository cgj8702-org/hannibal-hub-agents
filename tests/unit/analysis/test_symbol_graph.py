"""Unit tests for Cross-File Symbol Dependency & Breaking Signature Graph (SymbolImpactAnalyzer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from webhook_agent.analysis.symbol_graph import (
    BrokenCallSite,
    CallSiteSpec,
    SignatureDiff,
    SymbolImpactAnalyzer,
    SymbolImpactReport,
    compare_signatures,
    evaluate_contract_integrity,
    extract_signatures_from_code,
    verify_call_site_compatibility,
)

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
    assert p_sig.min_positional_args == 2
    assert p_sig.max_positional_args == 3


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

    cs1 = CallSiteSpec("test.py", 10, 0, "process", 2, frozenset(), False, False)
    assert verify_call_site_compatibility(cs1, sig) is None

    cs2 = CallSiteSpec("test.py", 11, 0, "process", 1, frozenset({"b"}), False, False)
    assert verify_call_site_compatibility(cs2, sig) is None


def test_verify_call_site_compatibility_missing_positional():
    code = "def process(a, b, c=10): pass"
    sig = extract_signatures_from_code(code)["process"]

    cs = CallSiteSpec("test.py", 10, 0, "process", 1, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "MISSING_REQUIRED_ARG"
    assert "Missing 1 required positional argument" in broken.description


def test_verify_call_site_compatibility_too_many_args():
    code = "def process(a, b): pass"
    sig = extract_signatures_from_code(code)["process"]

    cs = CallSiteSpec("test.py", 10, 0, "process", 3, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "TOO_MANY_ARGS"
    assert "Too many positional arguments" in broken.description


def test_verify_call_site_compatibility_missing_kwonly():
    code = "def process(a, *, strict: bool): pass"
    sig = extract_signatures_from_code(code)["process"]

    cs = CallSiteSpec("test.py", 10, 0, "process", 1, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "MISSING_KEYWORD_ARG"
    assert "Missing required keyword-only argument" in broken.description


def test_verify_call_site_compatibility_unexpected_kw():
    code = "def process(a, b=2): pass"
    sig = extract_signatures_from_code(code)["process"]

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

    cs = CallSiteSpec("test.py", 10, 0, "execute", 1, frozenset(), False, False)
    assert verify_call_site_compatibility(cs, sig) is None

    cs_bad = CallSiteSpec("test.py", 11, 0, "execute", 0, frozenset(), False, False)
    broken = verify_call_site_compatibility(cs_bad, sig)
    assert broken is not None
    assert broken.error_type == "MISSING_REQUIRED_ARG"


def test_symbol_impact_analyzer_real_repo():
    analyzer = SymbolImpactAnalyzer()
    report = analyzer.analyze_symbol(
        file_path="src/webhook_agent/tools/ast_tools.py",
        symbol_name="verify_python_ast",
    )
    assert report.symbol_name == "verify_python_ast"
    assert report.total_call_sites_found > 0
    assert len(report.broken_call_sites) == 0
    md = report.to_markdown()
    assert "verify_python_ast" in md
    assert "All External Call Sites Compatible" in md


def test_symbol_impact_analyzer_with_baseline_loader(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    mod_file = src_dir / "service.py"
    mod_file.write_text("def run(a, b, new_required_arg): pass\n")
    caller_file = src_dir / "caller.py"
    caller_file.write_text("from src.service import run\nrun(1, 2)\n")

    baseline_code = "def run(a, b): pass\n"

    def baseline_loader(rel_path: str) -> str | None:
        if rel_path == "src/service.py":
            return baseline_code
        return None

    analyzer = SymbolImpactAnalyzer(repo_root=tmp_path)
    reports = analyzer.analyze_modified_files(["src/service.py"], baseline_loader=baseline_loader)
    assert len(reports) == 1
    assert reports[0].symbol_name == "run"
    assert reports[0].sig_diff is not None
    assert reports[0].sig_diff.is_breaking is True
    assert len(reports[0].broken_call_sites) == 1


def test_symbol_impact_analyzer_without_baseline_loader(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    mod_file = src_dir / "service.py"
    mod_file.write_text("def run(a, b, c): pass\n")
    caller_file = src_dir / "caller.py"
    caller_file.write_text("run(1, 2)\n")

    analyzer = SymbolImpactAnalyzer(repo_root=tmp_path)
    reports = analyzer.analyze_modified_files(["src/service.py"], baseline_loader=None)
    assert len(reports) == 1
    assert reports[0].symbol_name == "run"
    assert reports[0].sig_diff is None
    assert len(reports[0].broken_call_sites) == 1
    assert reports[0].broken_call_sites[0].confidence == "PROVEN"


def test_analyze_modified_files_skips_dunder_methods(tmp_path: Path) -> None:
    """Dunder methods like __init__ must be skipped to avoid phantom WARNs.

    Regression test for #418: __init__ was not skipped by the private-symbol
    gate, so super().__init__(name=...) call sites were flagged as
    SUSPECTED UNEXPECTED_KEYWORD broken call sites even though __init__ does
    not accept keyword arguments.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)

    mod_file = src_dir / "plugin.py"
    mod_file.write_text(
        "class WebhookPlugin:\n"
        "    def __init__(self):\n"
        "        self.started = False\n"
        "    def _helper(self):\n"
        "        pass\n"
        "    def public_method(self, value):\n"
        "        return value\n"
    )

    caller_file = src_dir / "caller.py"
    caller_file.write_text(
        "from src.plugin import WebhookPlugin\n"
        "\n"
        "class SubPlugin(WebhookPlugin):\n"
        "    def __init__(self):\n"
        '        super().__init__(name="sub")\n'
        "        super().public_method(1)\n"
    )

    analyzer = SymbolImpactAnalyzer(repo_root=tmp_path)
    reports = analyzer.analyze_modified_files(["src/plugin.py"])

    symbol_names = {r.symbol_name for r in reports}
    assert "__init__" not in symbol_names
    assert "_helper" not in symbol_names


def test_verify_call_site_confidence_proven_vs_suspected():
    code = "def compute(x, y): pass"
    sig = extract_signatures_from_code(code)["compute"]

    # Direct call -> PROVEN
    direct_cs = CallSiteSpec(
        "test.py", 10, 0, "compute", 1, frozenset(), False, False, is_attribute_call=False
    )
    direct_broken = verify_call_site_compatibility(direct_cs, sig)
    assert direct_broken is not None
    assert direct_broken.confidence == "PROVEN"

    # Attribute call -> SUSPECTED
    attr_cs = CallSiteSpec(
        "test.py", 12, 0, "compute", 1, frozenset(), False, False, is_attribute_call=True
    )
    attr_broken = verify_call_site_compatibility(attr_cs, sig)
    assert attr_broken is not None
    assert attr_broken.confidence == "SUSPECTED"


def test_verify_call_site_unexpected_keyword_with_starred_args():
    code = "def compute(x, y=10): pass"
    sig = extract_signatures_from_code(code)["compute"]

    # Starred args passed, but explicit unexpected keyword passed without **kwargs
    cs = CallSiteSpec(
        "test.py", 15, 0, "compute", 1, frozenset({"bogus"}), True, False, is_attribute_call=False
    )
    broken = verify_call_site_compatibility(cs, sig)
    assert broken is not None
    assert broken.error_type == "UNEXPECTED_KEYWORD"
    assert broken.confidence == "PROVEN"

    # Starred args with valid keywords -> skips mismatch (None)
    cs_valid = CallSiteSpec(
        "test.py", 16, 0, "compute", 1, frozenset({"y"}), True, False, is_attribute_call=False
    )
    assert verify_call_site_compatibility(cs_valid, sig) is None


def test_symbol_impact_report_to_markdown_confidence():
    cs = CallSiteSpec("test.py", 10, 0, "func", 0, frozenset(), is_attribute_call=False)
    broken = BrokenCallSite(
        cs, "func", "MISSING_REQUIRED_ARG", "Missing 1 arg", confidence="PROVEN"
    )
    report = SymbolImpactReport(
        symbol_name="func",
        file_path="src/lib.py",
        sig_diff=None,
        total_call_sites_found=1,
        broken_call_sites=[broken],
        compatible_call_sites=[],
    )
    md = report.to_markdown()
    assert "[PROVEN]" in md
    assert "[MISSING_REQUIRED_ARG]" in md


def test_extract_signatures_error_collector():
    invalid_code = "def broken(\n"
    errors: list[str] = []
    sigs = extract_signatures_from_code(invalid_code, file_path="bad.py", error_collector=errors)
    assert sigs == {}
    assert len(errors) == 1
    assert "bad.py" in errors[0]
    assert "SyntaxError" in errors[0]


def test_symbol_impact_analyzer_parse_errors(tmp_path: Path):
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    bad_file = src_dir / "broken.py"
    bad_file.write_text("def syntax_error(\n")

    analyzer = SymbolImpactAnalyzer(repo_root=tmp_path)
    reports = analyzer.analyze_modified_files(["src/broken.py"])
    assert reports == []
    assert len(analyzer.parse_errors) == 1
    assert "src/broken.py" in analyzer.parse_errors[0]
    assert "SyntaxError" in analyzer.parse_errors[0]


def test_evaluate_contract_integrity_parse_errors():
    item = evaluate_contract_integrity(
        [], parse_errors=["src/bad.py:1: SyntaxError: unexpected EOF"]
    )
    assert item.dimension == "Contract Integrity"
    assert item.status == "ERROR"
    assert "ERROR: AST parse failure on: src/bad.py:1: SyntaxError: unexpected EOF" in item.notes

    unknown_item = evaluate_contract_integrity([], parse_errors=["UNKNOWN: Timeout during parsing"])
    assert unknown_item.status == "UNKNOWN"
    assert "UNKNOWN: Analysis incomplete" in unknown_item.notes


def test_evaluate_contract_integrity_proven_fail():
    cs = CallSiteSpec("src/caller.py", 20, 0, "run", 0, frozenset(), is_attribute_call=False)
    broken = BrokenCallSite(
        cs, "run", "MISSING_REQUIRED_ARG", "Missing required arg 'x'", confidence="PROVEN"
    )
    report = SymbolImpactReport(
        symbol_name="run",
        file_path="src/service.py",
        sig_diff=None,
        total_call_sites_found=1,
        broken_call_sites=[broken],
        compatible_call_sites=[],
    )
    item = evaluate_contract_integrity([report])
    assert item.dimension == "Contract Integrity"
    assert item.status == "FAIL"
    assert "PROVEN: Broken call site(s) detected for 'run' at src/caller.py:20" in item.notes


def test_evaluate_contract_integrity_suspected_warn():
    cs = CallSiteSpec("src/caller.py", 25, 0, "execute", 0, frozenset(), is_attribute_call=True)
    broken = BrokenCallSite(
        cs, "execute", "MISSING_REQUIRED_ARG", "Missing required arg", confidence="SUSPECTED"
    )
    report = SymbolImpactReport(
        symbol_name="execute",
        file_path="src/service.py",
        sig_diff=None,
        total_call_sites_found=1,
        broken_call_sites=[broken],
        compatible_call_sites=[],
    )
    item = evaluate_contract_integrity([report])
    assert item.dimension == "Contract Integrity"
    assert item.status == "WARN"
    assert (
        "SUSPECTED: Dynamic call site(s) may be affected: 'execute' at src/caller.py:25"
        in item.notes
    )


def test_evaluate_contract_integrity_breaking_signature_without_callers():
    diff = SignatureDiff(
        symbol_name="api_v2",
        file_path="src/api.py",
        old_sig=None,
        new_sig=None,
        is_breaking=True,
        reasons=["Required parameter 'token' was added without default."],
    )
    report = SymbolImpactReport(
        symbol_name="api_v2",
        file_path="src/api.py",
        sig_diff=diff,
        total_call_sites_found=0,
        broken_call_sites=[],
        compatible_call_sites=[],
    )
    item = evaluate_contract_integrity([report])
    assert item.dimension == "Contract Integrity"
    assert item.status == "WARN"
    assert "SUSPECTED: Breaking signature change detected on 'api_v2'" in item.notes


def test_evaluate_contract_integrity_pass():
    report = SymbolImpactReport(
        symbol_name="safe_func",
        file_path="src/safe.py",
        sig_diff=None,
        total_call_sites_found=2,
        broken_call_sites=[],
        compatible_call_sites=[],
    )
    item = evaluate_contract_integrity([report])
    assert item.dimension == "Contract Integrity"
    assert item.status == "PASS"
    assert "PASS: All cross-file signatures and call sites compatible." in item.notes

    empty_item = evaluate_contract_integrity([])
    assert empty_item.dimension == "Contract Integrity"
    assert empty_item.status == "PASS"
    assert "PASS: No breaking contract changes detected." in empty_item.notes
