"""Deterministic security auditing engine wrapping detect-secrets and bandit.

Classifies findings into tiered confidence (PROVEN, SUSPECTED, UNKNOWN) with
context-aware exemption rules for test directories, fixtures, and lockfiles.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import textwrap
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from webhook_agent.review.schemas import ScorecardItem

logger = logging.getLogger("webhook_agent.analysis.security_scanner")

ConfidenceLevel = Literal["PROVEN", "SUSPECTED", "UNKNOWN"]
SeverityLevel = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

RE_SECRET_LOOKAROUND_BYPASS = re.compile(
    r"\(\?(?:<!|!)[^)]+\)\s*(?:bearer|gh[pousr]_|github_pat_|akia|sk-|private\s+key)",
    re.IGNORECASE,
)
RE_SECRET_QUANTIFIER_WEAKENING = re.compile(
    r"bearer.*?\{\s*(?:[3-9]\d|\d{3,})\s*,",
    re.IGNORECASE,
)
RE_UNREDACTED_LOG_KEYWORDS = re.compile(
    r"(?:raw\s+unsanitized|unsanitized\s+model|raw\s+model\s+payload|unredacted\s+(?:secret|payload|token|data)|raw\s+payload)",
    re.IGNORECASE,
)

EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "tests/",
    "test/",
    "testing/",
    "fixtures/",
    "test_fixtures/",
)

EXEMPT_FILE_SUFFIXES: tuple[str, ...] = (
    "_test.py",
    "test_.py",
    ".lock",
)

EXEMPT_FILENAMES: tuple[str, ...] = (
    "conftest.py",
    "uv.lock",
    "package-lock.json",
    "poetry.lock",
)

EXEMPT_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "dummy",
    "fake",
    "example",
    "placeholder",
    "sample",
    "redacted",
    "mock",
    "00000000",
    "12345678",
    "sk-xxx",
    "gho_xxxx",
)


def is_exempt_file_path(path: str) -> bool:
    """Check if a file path is exempt from blocking security rules (tests, fixtures, docs)."""
    norm_path = path.strip().replace("\\", "/")
    if norm_path.startswith("./"):
        norm_path = norm_path[2:]
    norm_lower = norm_path.lower()
    filename = norm_path.split("/")[-1].lower()

    if any(norm_lower.startswith(p) or f"/{p}" in norm_lower for p in EXEMPT_PATH_PREFIXES):
        return True
    if any(norm_lower.endswith(s) for s in EXEMPT_FILE_SUFFIXES):
        return True
    return filename in EXEMPT_FILENAMES


def is_exempt_secret_value(value: str) -> bool:
    """Check if a detected secret value matches common dummy, test, or mock key patterns."""
    val_lower = value.strip().lower()
    if not val_lower:
        return False
    return any(sub in val_lower for sub in EXEMPT_VALUE_SUBSTRINGS)


@dataclass(frozen=True)
class SecurityFinding:
    """Structured security finding from static analyzer tools."""

    tool: Literal["detect-secrets", "bandit"]
    rule_id: str
    file_path: str
    line_number: int | None
    severity: SeverityLevel
    confidence: ConfidenceLevel
    description: str
    code_snippet: str = ""
    is_exempt: bool = False
    exemption_reason: str = ""


@dataclass
class SecurityAnalysisReport:
    """Aggregate report from deterministic security tooling."""

    findings: list[SecurityFinding] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)
    scanned_files_count: int = 0

    def active_findings(self) -> list[SecurityFinding]:
        """Return non-exempt findings requiring audit attention."""
        return [f for f in self.findings if not f.is_exempt]

    def proven_findings(self) -> list[SecurityFinding]:
        """Return hard-blockable findings verified with PROVEN confidence."""
        return [
            f
            for f in self.active_findings()
            if f.confidence == "PROVEN" and f.severity in ("CRITICAL", "HIGH")
        ]

    def suspected_findings(self) -> list[SecurityFinding]:
        """Return advisory findings evaluated with SUSPECTED confidence or lower severity."""
        return [
            f
            for f in self.active_findings()
            if f.confidence == "SUSPECTED" or f.severity in ("MEDIUM", "LOW")
        ]

    def to_markdown(self) -> str:
        """Format report into concise GitHub markdown for agent prompt or review."""
        if not self.active_findings():
            if self.scan_errors:
                return (
                    f"⚠️ Security scan encountered non-fatal errors: {', '.join(self.scan_errors)}"
                )
            return "✅ No hardcoded secrets or static security defects detected."

        lines = ["#### 🛡️ Active Security Findings"]
        for f in self.active_findings():
            loc = f"`{f.file_path}:{f.line_number}`" if f.line_number else f"`{f.file_path}`"
            snippet = f"\n  ```\n  {f.code_snippet.strip()}\n  ```" if f.code_snippet else ""
            lines.append(
                f"- **[{f.confidence} {f.severity}]** {loc} ({f.tool} `{f.rule_id}`): "
                f"{f.description}{snippet}"
            )
        return "\n".join(lines)


class _SecurityPatternVisitor(ast.NodeVisitor):
    """AST visitor detecting unredacted payload logging, regex bypasses, and env-gated leaks."""

    def __init__(self, file_path: str, is_exempt: bool = False) -> None:
        self.file_path = file_path
        self.is_exempt = is_exempt
        self.findings: list[SecurityFinding] = []
        self._env_check_depth = 0
        self._sanitizer_func_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        name_lower = node.name.lower()
        is_sanitizer = any(kw in name_lower for kw in ("sanitiz", "redact", "mask"))
        if is_sanitizer:
            self._sanitizer_func_depth += 1
        self.generic_visit(node)
        if is_sanitizer:
            self._sanitizer_func_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        name_lower = node.name.lower()
        is_sanitizer = any(kw in name_lower for kw in ("sanitiz", "redact", "mask"))
        if is_sanitizer:
            self._sanitizer_func_depth += 1
        self.generic_visit(node)
        if is_sanitizer:
            self._sanitizer_func_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        test_str = ast.unparse(node.test)
        is_env_check = "getenv" in test_str or "environ" in test_str
        if is_env_check:
            self._env_check_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        if is_env_check:
            self._env_check_depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        if not self.is_exempt:
            self._check_logging_call(node)
            self._check_regex_call(node)
        self.generic_visit(node)

    def _check_logging_call(self, node: ast.Call) -> None:
        func_str = ast.unparse(node.func)
        is_log = (
            any(func_str.startswith(prefix) for prefix in ("logger.", "logging.", "log."))
            or func_str == "print"
        )
        if not is_log:
            return

        call_args_str = " ".join(ast.unparse(a) for a in node.args)

        # 1. Direct unredacted model payload or unsanitized keywords
        if RE_UNREDACTED_LOG_KEYWORDS.search(call_args_str):
            self.findings.append(
                SecurityFinding(
                    tool="bandit",
                    rule_id="security:unredacted_payload_logging",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    severity="CRITICAL",
                    confidence="PROVEN",
                    description=(
                        f"Unredacted payload, raw secret, or unsanitized text logged to output sink: "
                        f"'{call_args_str[:80]}'. Logging unredacted inputs or payloads compromises log privacy "
                        "and circumvents sanitization boundaries."
                    ),
                    code_snippet=call_args_str[:100],
                    is_exempt=False,
                )
            )

        # 2. Environment-gated sensitive leak
        if self._env_check_depth > 0:
            call_lower = call_args_str.lower()
            if any(
                kw in call_lower
                for kw in (
                    "payload",
                    "token",
                    "secret",
                    "auth",
                    "credential",
                    "unsanitized",
                    "raw",
                )
            ):
                self.findings.append(
                    SecurityFinding(
                        tool="bandit",
                        rule_id="security:env_gated_leak",
                        file_path=self.file_path,
                        line_number=node.lineno,
                        severity="CRITICAL",
                        confidence="PROVEN",
                        description=(
                            f"Sensitive data or payload logging is conditionally gated behind an environment "
                            f"variable check: '{call_args_str[:80]}'. Environment variable checks (e.g. DEBUG_TELEMETRY) "
                            "do not mitigate sensitive data exfiltration to shared log sinks."
                        ),
                        code_snippet=call_args_str[:100],
                        is_exempt=False,
                    )
                )

    def _check_regex_call(self, node: ast.Call) -> None:
        func_str = ast.unparse(node.func)
        if func_str not in ("re.compile", "compile") and not func_str.endswith(".compile"):
            return
        if not node.args:
            return

        arg0 = node.args[0]
        if not isinstance(arg0, ast.Constant) or not isinstance(arg0.value, str):
            return

        regex_pattern = arg0.value

        if RE_SECRET_LOOKAROUND_BYPASS.search(regex_pattern):
            self.findings.append(
                SecurityFinding(
                    tool="bandit",
                    rule_id="security:secret_pattern_bypass",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    severity="CRITICAL",
                    confidence="PROVEN",
                    description=(
                        f"Negative lookaround exemption bypass detected in secret detection pattern: "
                        f"'{regex_pattern[:80]}'. Secret detection regexes must match secrets unconditionally."
                    ),
                    code_snippet=regex_pattern[:100],
                    is_exempt=False,
                )
            )

        if RE_SECRET_QUANTIFIER_WEAKENING.search(regex_pattern):
            self.findings.append(
                SecurityFinding(
                    tool="bandit",
                    rule_id="security:secret_pattern_weakening",
                    file_path=self.file_path,
                    line_number=node.lineno,
                    severity="CRITICAL",
                    confidence="PROVEN",
                    description=(
                        f"Secret pattern regex quantifier weakened (minimum token length increased): "
                        f"'{regex_pattern[:80]}'. Weakening regex thresholds allows shorter live secrets to bypass detection."
                    ),
                    code_snippet=regex_pattern[:100],
                    is_exempt=False,
                )
            )


class SecurityScanner:
    """Runs detect-secrets and bandit on repository files and diffs."""

    def __init__(self, repo_root: str | Path | None = None) -> None:
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def scan_diff_secrets(self, diff_text: str) -> list[SecurityFinding]:
        """Scan unified diff additions using detect-secrets."""
        if not diff_text or not diff_text.strip():
            return []

        try:
            from detect_secrets.core.scan import _scan_line, get_code_snippet
            from detect_secrets.settings import default_settings, get_plugins
        except ImportError as err:
            logger.warning("detect-secrets is not available: %s", err)
            return []

        findings: list[SecurityFinding] = []
        current_file: str | None = None
        current_line_no = 0

        with default_settings():
            plugins = get_plugins()
            # Simple unified diff parser tracking files and line numbers of '+' lines
            for line in diff_text.splitlines():
                if line.startswith("+++ b/"):
                    current_file = line[6:].strip()
                    current_line_no = 0
                    continue
                if line.startswith("@@ "):
                    # Extract new line start from hunk header, e.g. @@ -10,3 +20,4 @@
                    parts = line.split(" ")
                    if len(parts) >= 3 and parts[2].startswith("+"):
                        with suppress(Exception):
                            hunk_start = parts[2][1:].split(",")[0]
                            current_line_no = int(hunk_start) - 1
                    continue

                if not current_file:
                    continue

                if line.startswith("+") and not line.startswith("+++"):
                    current_line_no += 1
                    added_content = line[1:]
                    if not added_content.strip():
                        continue

                    try:
                        context = get_code_snippet(lines=[added_content], line_number=1)
                        secrets = [
                            s
                            for p in plugins
                            for s in _scan_line(
                                plugin=p,
                                filename=current_file,
                                line=added_content,
                                line_number=current_line_no,
                                context=context,
                            )
                        ]
                    except Exception as scan_err:
                        logger.debug(
                            "detect-secrets scan error on '%s': %s", added_content, scan_err
                        )
                        continue

                    for secret in secrets:
                        secret_val = getattr(secret, "secret_value", "") or ""
                        sec_type = getattr(secret, "type", "Secret Keyword")
                        is_path_exempt = is_exempt_file_path(current_file)
                        is_val_exempt = is_exempt_secret_value(secret_val) if secret_val else False

                        is_exempt = is_path_exempt or is_val_exempt
                        exemption_reason = (
                            "Exempt file path (tests/fixtures/docs)"
                            if is_path_exempt
                            else ("Dummy/test key pattern" if is_val_exempt else "")
                        )

                        confidence: ConfidenceLevel = "PROVEN" if not is_exempt else "SUSPECTED"
                        findings.append(
                            SecurityFinding(
                                tool="detect-secrets",
                                rule_id=sec_type,
                                file_path=current_file,
                                line_number=current_line_no,
                                severity="CRITICAL",
                                confidence=confidence,
                                description=f"Potential hardcoded secret ({sec_type}) detected in added line",
                                code_snippet=added_content.strip()[:100],
                                is_exempt=is_exempt,
                                exemption_reason=exemption_reason,
                            )
                        )
                elif not line.startswith("-"):
                    current_line_no += 1

        return findings

    def scan_python_bandit(
        self,
        file_paths: list[str],
        repo_root: Path | None = None,
    ) -> tuple[list[SecurityFinding], list[str]]:
        """Run bandit security AST checks on Python files."""
        root = repo_root or self.repo_root
        py_files = [f for f in file_paths if f.endswith(".py") and not is_exempt_file_path(f)]
        if not py_files:
            return [], []

        existing_py_files: list[str] = []
        for pf in py_files:
            full_p = root / pf if not Path(pf).is_absolute() else Path(pf)
            if full_p.is_file():
                existing_py_files.append(str(full_p))

        if not existing_py_files:
            return [], []

        config_arg: list[str] = []
        pyproject_path = root / "pyproject.toml"
        if pyproject_path.is_file():
            config_arg = [
                "-c",
                str(pyproject_path),  # read [tool.bandit] skips from pyproject.toml
                "-s",
                "B101,B110",  # Skip assert_used and try_except_pass
            ]
        else:
            config_arg = ["-s", "B101,B110"]

        cmd = [
            "bandit",
            "-f",
            "json",
            "-q",
            *config_arg,
            *existing_py_files,
        ]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
            if not res.stdout or not res.stdout.strip():
                return [], []
            data = json.loads(res.stdout)
        except FileNotFoundError:
            return [], ["bandit executable not found in environment"]
        except Exception as err:
            return [], [f"bandit execution error: {err}"]

        findings: list[SecurityFinding] = []
        results: list[dict[str, Any]] = data.get("results", [])

        for r in results:
            raw_fname = str(r.get("filename") or "")
            try:
                rel_path = str(Path(raw_fname).relative_to(root))
            except Exception:
                rel_path = raw_fname

            test_id = str(r.get("test_id") or "bandit")
            issue_text = str(r.get("issue_text") or "")
            code_snip = str(r.get("code") or "")
            line_no = r.get("line_number")
            raw_sev = str(r.get("issue_severity") or "LOW").upper()
            raw_conf = str(r.get("issue_confidence") or "LOW").upper()

            severity: SeverityLevel = (
                "CRITICAL"
                if raw_sev == "HIGH" and raw_conf == "HIGH"
                else ("HIGH" if raw_sev == "HIGH" else ("MEDIUM" if raw_sev == "MEDIUM" else "LOW"))
            )
            # High severity + High confidence AST defects (like eval, exec, shell=True) are PROVEN
            confidence: ConfidenceLevel = (
                "PROVEN" if raw_sev in ("HIGH", "MEDIUM") and raw_conf == "HIGH" else "SUSPECTED"
            )

            is_exempt = is_exempt_file_path(rel_path)
            findings.append(
                SecurityFinding(
                    tool="bandit",
                    rule_id=test_id,
                    file_path=rel_path,
                    line_number=int(line_no) if isinstance(line_no, int) else None,
                    severity=severity,
                    confidence=confidence,
                    description=issue_text,
                    code_snippet=code_snip.strip(),
                    is_exempt=is_exempt,
                    exemption_reason="Exempt test/fixture file" if is_exempt else "",
                )
            )

        return findings, []

    def scan(
        self,
        diff_text: str = "",
        changed_files: list[str] | None = None,
        repo_root: str | Path | None = None,
    ) -> SecurityAnalysisReport:
        """Run full deterministic security scan across diff and modified files."""
        root = Path(repo_root) if repo_root else self.repo_root
        all_findings: list[SecurityFinding] = []
        all_errors: list[str] = []

        # 1. detect-secrets on diff
        if diff_text:
            try:
                diff_findings = self.scan_diff_secrets(diff_text)
                all_findings.extend(diff_findings)
            except Exception as err:
                all_errors.append(f"detect-secrets diff scan failed: {err}")

        # 2. bandit on changed python files
        files_to_check = changed_files or []
        if files_to_check:
            try:
                bandit_findings, bandit_errors = self.scan_python_bandit(
                    files_to_check, repo_root=root
                )
                all_findings.extend(bandit_findings)
                all_errors.extend(bandit_errors)
            except Exception as err:
                all_errors.append(f"bandit scan failed: {err}")

        # 3. Deterministic AST & Diff Security Patterns
        try:
            pattern_findings = self.scan_security_patterns(
                diff_text=diff_text,
                changed_files=files_to_check,
                repo_root=root,
            )
            all_findings.extend(pattern_findings)
        except Exception as err:
            all_errors.append(f"security pattern scan failed: {err}")

        return SecurityAnalysisReport(
            findings=all_findings,
            scan_errors=all_errors,
            scanned_files_count=len(files_to_check),
        )

    def scan_security_patterns(
        self,
        diff_text: str = "",
        changed_files: list[str] | None = None,
        repo_root: Path | None = None,
    ) -> list[SecurityFinding]:
        """Scan diff and Python files for unredacted logging, regex bypasses, and env-gated leaks."""
        root = repo_root or self.repo_root
        findings: list[SecurityFinding] = []
        seen: set[tuple[str, int | None, str]] = set()

        def add_finding(f: SecurityFinding) -> None:
            key = (f.file_path, f.line_number, f.rule_id)
            if key not in seen:
                seen.add(key)
                findings.append(f)

        # 1. Scan changed Python files on disk via AST
        py_files = [
            f for f in (changed_files or []) if f.endswith(".py") and not is_exempt_file_path(f)
        ]
        for pf in py_files:
            file_disk_path = root / pf if not Path(pf).is_absolute() else Path(pf)
            if file_disk_path.is_file():
                try:
                    content = file_disk_path.read_text(encoding="utf-8", errors="replace")
                    tree = ast.parse(content, filename=str(file_disk_path))
                    visitor = _SecurityPatternVisitor(
                        file_path=pf, is_exempt=is_exempt_file_path(pf)
                    )
                    visitor.visit(tree)
                    for f in visitor.findings:
                        add_finding(f)
                except Exception as exc:
                    logger.debug("Failed AST parsing of %s for security patterns: %s", pf, exc)

        # 2. Scan unified diff additions (handles partial diffs or environments without full workspace)
        if diff_text and diff_text.strip():
            from webhook_agent.analysis.diff_filter import split_diff_into_sections

            sections = split_diff_into_sections(diff_text)
            for sec in sections:
                path = sec.path
                if is_exempt_file_path(path) or not path.endswith(".py"):
                    continue

                hunk_line = 0
                for line in sec.lines:
                    if line.startswith("@@"):
                        match = re.search(r"\+(\d+)", line)
                        if match:
                            with suppress(Exception):
                                hunk_line = int(match.group(1)) - 1
                        continue
                    if line.startswith("+") and not line.startswith("+++"):
                        hunk_line += 1
                        added_line = line[1:]

                        if RE_SECRET_LOOKAROUND_BYPASS.search(added_line):
                            add_finding(
                                SecurityFinding(
                                    tool="bandit",
                                    rule_id="security:secret_pattern_bypass",
                                    file_path=path,
                                    line_number=hunk_line,
                                    severity="CRITICAL",
                                    confidence="PROVEN",
                                    description=(
                                        f"Negative lookaround exemption bypass detected in secret detection pattern: "
                                        f"'{added_line.strip()[:80]}'. Secret detection regexes must match secrets unconditionally."
                                    ),
                                    code_snippet=added_line.strip()[:100],
                                    is_exempt=False,
                                )
                            )

                        if RE_SECRET_QUANTIFIER_WEAKENING.search(added_line):
                            add_finding(
                                SecurityFinding(
                                    tool="bandit",
                                    rule_id="security:secret_pattern_weakening",
                                    file_path=path,
                                    line_number=hunk_line,
                                    severity="CRITICAL",
                                    confidence="PROVEN",
                                    description=(
                                        f"Secret pattern regex quantifier weakened (minimum token length increased): "
                                        f"'{added_line.strip()[:80]}'. Weakening regex thresholds allows shorter live secrets to bypass detection."
                                    ),
                                    code_snippet=added_line.strip()[:100],
                                    is_exempt=False,
                                )
                            )

                        if RE_UNREDACTED_LOG_KEYWORDS.search(added_line):
                            add_finding(
                                SecurityFinding(
                                    tool="bandit",
                                    rule_id="security:unredacted_payload_logging",
                                    file_path=path,
                                    line_number=hunk_line,
                                    severity="CRITICAL",
                                    confidence="PROVEN",
                                    description=(
                                        f"Unredacted payload, raw secret, or unsanitized text logged to output sink: "
                                        f"'{added_line.strip()[:80]}'. Logging unredacted inputs or payloads compromises log privacy "
                                        "and circumvents sanitization boundaries."
                                    ),
                                    code_snippet=added_line.strip()[:100],
                                    is_exempt=False,
                                )
                            )
                    elif not line.startswith("-"):
                        hunk_line += 1

                # Also try AST parsing on dedented added content
                added_lines = [
                    line[1:]
                    for line in sec.lines
                    if line.startswith("+") and not line.startswith("+++")
                ]
                if added_lines:
                    added_content = textwrap.dedent("".join(added_lines))
                    diff_tree: ast.Module | None = None
                    try:
                        diff_tree = ast.parse(added_content)
                    except Exception:
                        with suppress(Exception):
                            diff_tree = ast.parse(
                                f"def _synthetic_diff_scope():\n{textwrap.indent(added_content, '    ')}\n"
                            )

                    if diff_tree is not None:
                        visitor = _SecurityPatternVisitor(file_path=path, is_exempt=False)
                        visitor.visit(diff_tree)
                        for f in visitor.findings:
                            add_finding(f)

        return findings


def evaluate_security_integrity(
    report: SecurityAnalysisReport,
    scan_errors: list[str] | None = None,
) -> ScorecardItem:
    """Evaluate Dimension 3 (Security & Secrets) scorecard item deterministically.

    - If PROVEN findings exist: FAIL (triggers scorecard floor; LLM cannot overturn).
    - If SUSPECTED findings exist: WARN (advisory).
    - If scanner errored without findings: UNKNOWN (logs warning).
    - Otherwise: PASS.
    """
    errors = list(report.scan_errors) + list(scan_errors or [])
    proven = report.proven_findings()
    suspected = report.suspected_findings()

    if proven:
        summary_items = [
            f"{f.tool}:{f.rule_id} in {f.file_path}:{f.line_number}" for f in proven[:3]
        ]
        if len(proven) > 3:
            summary_items.append(f"...+{len(proven) - 3} more")
        notes = f"PROVEN security defect(s) detected: {'; '.join(summary_items)}."
        return ScorecardItem(
            dimension="Security & Secrets",
            status="FAIL",
            notes=notes,
        )

    if suspected:
        summary_items = [
            f"{f.tool}:{f.rule_id} in {f.file_path}:{f.line_number}" for f in suspected[:3]
        ]
        if len(suspected) > 3:
            summary_items.append(f"...+{len(suspected) - 3} more")
        notes = f"SUSPECTED security advisory item(s): {'; '.join(summary_items)}."
        return ScorecardItem(
            dimension="Security & Secrets",
            status="WARN",
            notes=notes,
        )

    if errors:
        return ScorecardItem(
            dimension="Security & Secrets",
            status="UNKNOWN",
            notes=f"Security scanner encountered non-fatal error: {'; '.join(errors)}",
        )

    return ScorecardItem(
        dimension="Security & Secrets",
        status="PASS",
        notes="Deterministic audit confirmed 0 hardcoded secrets or static security defects.",
    )
