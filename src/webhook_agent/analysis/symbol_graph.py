"""Cross-File Symbol Dependency & Breaking Signature Graph (SymbolImpactAnalyzer).

Statically analyzes Python AST definitions in modified files, identifies breaking
signature changes, scans repository call sites, and detects broken invocation sites
across downstream files.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from webhook_agent.review.schemas import ScorecardItem

logger = logging.getLogger("webhook_agent.analysis.symbol_graph")

ConfidenceLevel = Literal["PROVEN", "SUSPECTED", "UNKNOWN"]


@dataclass(frozen=True)
class ParameterSpec:
    """Specification of a single parameter in a callable definition."""

    name: str
    kind: str  # POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY, VAR_POSITIONAL, VAR_KEYWORD
    has_default: bool
    default_repr: str | None = None
    annotation: str | None = None


@dataclass(frozen=True)
class SignatureSpec:
    """Specification of a callable (function/method) signature."""

    symbol_name: str
    qualname: str
    file_path: str
    line_number: int
    parameters: list[ParameterSpec]
    has_var_positional: bool = False  # *args
    has_var_keyword: bool = False  # **kwargs
    min_positional_args: int = 0
    max_positional_args: int | None = None  # None if has_var_positional
    required_kwonly_args: frozenset[str] = field(default_factory=frozenset)
    is_method: bool = False
    first_arg_is_self: bool = False


@dataclass(frozen=True)
class SignatureDiff:
    """Differences between a baseline signature and an updated signature."""

    symbol_name: str
    file_path: str
    old_sig: SignatureSpec | None
    new_sig: SignatureSpec | None
    is_breaking: bool
    reasons: list[str]


@dataclass(frozen=True)
class CallSiteSpec:
    """An invocation site of a symbol in Python source code."""

    file_path: str
    line_number: int
    col_offset: int
    callee_name: str
    positional_arg_count: int
    keyword_names: frozenset[str]
    has_starred_arg: bool = False  # *args
    has_starred_kwarg: bool = False  # **kwargs
    raw_code: str = ""
    is_attribute_call: bool = False  # True if obj.method(), False if direct func()
    receiver_name: str | None = None


@dataclass(frozen=True)
class BrokenCallSite:
    """A call site that is incompatible with the symbol's signature."""

    call_site: CallSiteSpec
    target_symbol: str
    error_type: (
        str  # "MISSING_REQUIRED_ARG", "TOO_MANY_ARGS", "MISSING_KEYWORD_ARG", "UNEXPECTED_KEYWORD"
    )
    description: str
    confidence: Literal["PROVEN", "SUSPECTED"] = "PROVEN"


@dataclass(frozen=True)
class SymbolImpactReport:
    """Clinical summary of cross-file symbol impact and call-site compatibility."""

    symbol_name: str
    file_path: str
    sig_diff: SignatureDiff | None
    total_call_sites_found: int
    broken_call_sites: list[BrokenCallSite]
    compatible_call_sites: list[CallSiteSpec]

    def to_markdown(self) -> str:
        """Render clinical Markdown report for the symbol impact."""
        lines = [f"#### 🔗 Symbol: `{self.symbol_name}` (`{self.file_path}`)"]
        if self.sig_diff and self.sig_diff.reasons:
            status = (
                "🔴 BREAKING SIGNATURE CHANGE"
                if self.sig_diff.is_breaking
                else "[MODIFIED] Signature Modified"
            )
            lines.append(f"- **Contract Status**: {status}")
            for r in self.sig_diff.reasons:
                lines.append(f"  * ⚠️ {r}")

        lines.append(f"- **External Call Sites Checked**: {self.total_call_sites_found}")

        if self.broken_call_sites:
            lines.append("- **❌ Broken Call Sites Detected**:")
            for b in self.broken_call_sites:
                cs = b.call_site
                loc = f"`{cs.file_path}:{cs.line_number}`"
                lines.append(f"  * {loc}: [{b.confidence}] [{b.error_type}] {b.description}")
        else:
            lines.append("- **✅ All External Call Sites Compatible**.")

        return "\n".join(lines)


class SymbolDefinitionVisitor(ast.NodeVisitor):
    """AST visitor to extract callable signatures from a Python module."""

    def __init__(self, file_path: str = "") -> None:
        self.file_path = file_path
        self.signatures: dict[str, SignatureSpec] = {}
        self._class_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_func(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_func(node)
        self.generic_visit(node)

    def _record_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = ".".join([*self._class_stack, node.name]) if self._class_stack else node.name
        is_method = bool(self._class_stack)

        args = node.args
        all_pos = args.posonlyargs + args.args
        num_defaults = len(args.defaults)
        num_no_defaults = len(all_pos) - num_defaults

        parameters: list[ParameterSpec] = []

        # Positional-only args
        for i, a in enumerate(args.posonlyargs):
            has_def = i >= num_no_defaults
            def_node = args.defaults[i - num_no_defaults] if has_def else None
            parameters.append(
                ParameterSpec(
                    name=a.arg,
                    kind="POSITIONAL_ONLY",
                    has_default=has_def,
                    default_repr=ast.unparse(def_node) if def_node else None,
                    annotation=ast.unparse(a.annotation) if a.annotation else None,
                )
            )

        # Standard positional or keyword args
        offset = len(args.posonlyargs)
        for j, a in enumerate(args.args):
            global_idx = offset + j
            has_def = global_idx >= num_no_defaults
            def_node = args.defaults[global_idx - num_no_defaults] if has_def else None
            parameters.append(
                ParameterSpec(
                    name=a.arg,
                    kind="POSITIONAL_OR_KEYWORD",
                    has_default=has_def,
                    default_repr=ast.unparse(def_node) if def_node else None,
                    annotation=ast.unparse(a.annotation) if a.annotation else None,
                )
            )

        # Varargs (*args)
        has_var_pos = args.vararg is not None
        if args.vararg:
            parameters.append(
                ParameterSpec(
                    name=args.vararg.arg,
                    kind="VAR_POSITIONAL",
                    has_default=False,
                    annotation=ast.unparse(args.vararg.annotation)
                    if args.vararg.annotation
                    else None,
                )
            )

        # Keyword-only args
        req_kwonly: set[str] = set()
        for k_arg, k_def in zip(args.kwonlyargs, args.kw_defaults, strict=False):
            has_def = k_def is not None
            if not has_def:
                req_kwonly.add(k_arg.arg)
            parameters.append(
                ParameterSpec(
                    name=k_arg.arg,
                    kind="KEYWORD_ONLY",
                    has_default=has_def,
                    default_repr=ast.unparse(k_def) if k_def else None,
                    annotation=ast.unparse(k_arg.annotation) if k_arg.annotation else None,
                )
            )

        # Varkw (**kwargs)
        has_var_kw = args.kwarg is not None
        if args.kwarg:
            parameters.append(
                ParameterSpec(
                    name=args.kwarg.arg,
                    kind="VAR_KEYWORD",
                    has_default=False,
                    annotation=ast.unparse(args.kwarg.annotation)
                    if args.kwarg.annotation
                    else None,
                )
            )

        first_arg_is_self = is_method and bool(all_pos and all_pos[0].arg in ("self", "cls"))
        min_pos = num_no_defaults
        max_pos = None if has_var_pos else len(all_pos)

        sig = SignatureSpec(
            symbol_name=node.name,
            qualname=qualname,
            file_path=self.file_path,
            line_number=node.lineno,
            parameters=parameters,
            has_var_positional=has_var_pos,
            has_var_keyword=has_var_kw,
            min_positional_args=min_pos,
            max_positional_args=max_pos,
            required_kwonly_args=frozenset(req_kwonly),
            is_method=is_method,
            first_arg_is_self=first_arg_is_self,
        )
        self.signatures[node.name] = sig
        self.signatures[qualname] = sig


class CallSiteVisitor(ast.NodeVisitor):
    """AST visitor to find all call sites of a target symbol in a Python module."""

    def __init__(self, target_symbol: str, file_path: str = "") -> None:
        self.target_symbol = target_symbol
        self.file_path = file_path
        self.call_sites: list[CallSiteSpec] = []

    def visit_Call(self, node: ast.Call) -> None:
        is_match = False
        callee_name = ""
        is_attribute_call = False
        receiver_name: str | None = None

        # Direct function call: func(...)
        if isinstance(node.func, ast.Name) and node.func.id == self.target_symbol:
            is_match = True
            callee_name = node.func.id
            is_attribute_call = False
        # Attribute call: obj.method(...)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == self.target_symbol:
            is_match = True
            callee_name = node.func.attr
            is_attribute_call = True
            if isinstance(node.func.value, ast.Name):
                receiver_name = node.func.value.id

        if is_match:
            has_star = any(isinstance(a, ast.Starred) for a in node.args)
            has_star_kw = any(kw.arg is None for kw in node.keywords)
            kw_names = frozenset(kw.arg for kw in node.keywords if kw.arg is not None)

            call_spec = CallSiteSpec(
                file_path=self.file_path,
                line_number=node.lineno,
                col_offset=node.col_offset,
                callee_name=callee_name,
                positional_arg_count=len(node.args),
                keyword_names=kw_names,
                has_starred_arg=has_star,
                has_starred_kwarg=has_star_kw,
                raw_code=ast.unparse(node),
                is_attribute_call=is_attribute_call,
                receiver_name=receiver_name,
            )
            self.call_sites.append(call_spec)

        self.generic_visit(node)


def extract_signatures_from_code(
    code: str,
    file_path: str = "",
    error_collector: list[str] | None = None,
) -> dict[str, SignatureSpec]:
    """Parse Python code and extract all callable signatures."""
    try:
        tree = ast.parse(code, filename=file_path)
    except SyntaxError as err:
        if error_collector is not None:
            lineno = getattr(err, "lineno", "?")
            msg = getattr(err, "msg", str(err))
            error_collector.append(f"{file_path}:{lineno}: SyntaxError: {msg}")
        return {}
    except Exception as exc:
        if error_collector is not None:
            error_collector.append(f"{file_path}: Exception: {exc}")
        return {}
    visitor = SymbolDefinitionVisitor(file_path=file_path)
    visitor.visit(tree)
    return visitor.signatures


def compare_signatures(
    old_sig: SignatureSpec | None,
    new_sig: SignatureSpec | None,
) -> SignatureDiff:
    """Compare baseline and new signatures to detect breaking changes."""
    if old_sig is None and new_sig is None:
        return SignatureDiff("", "", None, None, False, [])

    symbol_name = new_sig.symbol_name if new_sig else old_sig.symbol_name  # type: ignore[union-attr]
    file_path = new_sig.file_path if new_sig else old_sig.file_path  # type: ignore[union-attr]

    reasons: list[str] = []
    is_breaking = False

    # Deleted symbol
    if old_sig is not None and new_sig is None:
        return SignatureDiff(
            symbol_name=symbol_name,
            file_path=file_path,
            old_sig=old_sig,
            new_sig=new_sig,
            is_breaking=True,
            reasons=[f"Symbol '{symbol_name}' was removed or renamed."],
        )

    # New symbol
    if old_sig is None and new_sig is not None:
        return SignatureDiff(
            symbol_name=symbol_name,
            file_path=file_path,
            old_sig=old_sig,
            new_sig=new_sig,
            is_breaking=False,
            reasons=[],
        )
    if old_sig is None or new_sig is None:
        return SignatureDiff("", "", None, None, False, [])

    # Check for parameters added without default values
    old_params_by_name = {p.name: p for p in old_sig.parameters}
    new_pos_names = [
        p.name for p in new_sig.parameters if p.kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
    ]
    old_pos_names = [
        p.name for p in old_sig.parameters if p.kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
    ]

    for p in new_sig.parameters:
        if p.name not in old_params_by_name:
            if not p.has_default and p.kind not in ("VAR_POSITIONAL", "VAR_KEYWORD"):
                is_breaking = True
                reasons.append(f"Required parameter '{p.name}' was added without a default value.")
        else:
            old_p = old_params_by_name[p.name]
            # Parameter had default before, but default was removed
            if old_p.has_default and not p.has_default:
                is_breaking = True
                reasons.append(
                    f"Default value for parameter '{p.name}' was removed (now required)."
                )

    # Check for removed parameters
    for old_name, old_p in old_params_by_name.items():
        if old_name not in {p.name: p for p in new_sig.parameters}:
            if old_p.kind not in ("VAR_POSITIONAL", "VAR_KEYWORD"):
                is_breaking = True
                reasons.append(f"Parameter '{old_name}' was removed.")

    # Check for positional reordering
    common_pos = [name for name in new_pos_names if name in old_pos_names]
    old_common_pos = [name for name in old_pos_names if name in new_pos_names]
    if common_pos != old_common_pos:
        is_breaking = True
        reasons.append("Positional parameters were reordered.")

    # Check for new required keyword-only parameters
    new_req_kw = new_sig.required_kwonly_args - old_sig.required_kwonly_args
    for rkw in new_req_kw:
        is_breaking = True
        reasons.append(f"Required keyword-only parameter '{rkw}' was added.")

    return SignatureDiff(
        symbol_name=symbol_name,
        file_path=file_path,
        old_sig=old_sig,
        new_sig=new_sig,
        is_breaking=is_breaking,
        reasons=reasons,
    )


def verify_call_site_compatibility(
    call_site: CallSiteSpec,
    sig: SignatureSpec,
) -> BrokenCallSite | None:
    """Verify whether a call site satisfies the callable's signature constraints."""
    confidence: Literal["PROVEN", "SUSPECTED"] = (
        "SUSPECTED" if call_site.is_attribute_call else "PROVEN"
    )

    # If dynamic forwarding is used (*args or **kwargs), skip static argument count mismatch
    # but still catch explicit unexpected keywords if **kwargs is not accepted
    if call_site.has_starred_arg or call_site.has_starred_kwarg:
        if not sig.has_var_keyword:
            valid_kw_names = {
                p.name
                for p in sig.parameters
                if p.kind in ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")
            }
            unexpected_kw = call_site.keyword_names - valid_kw_names
            if unexpected_kw:
                return BrokenCallSite(
                    call_site=call_site,
                    target_symbol=sig.symbol_name,
                    error_type="UNEXPECTED_KEYWORD",
                    description=(
                        f"Unexpected keyword argument(s): {sorted(unexpected_kw)}. "
                        f"Valid keyword arguments are: {sorted(valid_kw_names)}."
                    ),
                    confidence=confidence,
                )
        return None

    pos_count = call_site.positional_arg_count
    kw_names = call_site.keyword_names

    # If calling a method with self on an instance, the caller supplies 1 fewer positional arg
    effective_min_pos = sig.min_positional_args
    effective_max_pos = sig.max_positional_args
    if sig.is_method and sig.first_arg_is_self:
        effective_min_pos = max(0, effective_min_pos - 1)
        if effective_max_pos is not None:
            effective_max_pos = max(0, effective_max_pos - 1)

    # Check for missing required positional args (accounting for args passed via keywords)
    pos_param_names = [
        p.name for p in sig.parameters if p.kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
    ]
    if sig.is_method and sig.first_arg_is_self and pos_param_names:
        pos_param_names = pos_param_names[1:]

    # Required parameters that must be supplied either positionally or by keyword
    req_pos_names = pos_param_names[:effective_min_pos]
    supplied_via_kw = set(req_pos_names).intersection(kw_names)
    total_effective_pos_supplied = pos_count + len(supplied_via_kw)

    if total_effective_pos_supplied < effective_min_pos:
        missing_count = effective_min_pos - total_effective_pos_supplied
        return BrokenCallSite(
            call_site=call_site,
            target_symbol=sig.symbol_name,
            error_type="MISSING_REQUIRED_ARG",
            description=(
                f"Missing {missing_count} required positional argument(s). "
                f"Signature requires at least {effective_min_pos}, but call supplies {pos_count} "
                f"(keywords supplied: {sorted(kw_names)})."
            ),
            confidence=confidence,
        )

    # Check for too many positional arguments
    if effective_max_pos is not None and pos_count > effective_max_pos:
        return BrokenCallSite(
            call_site=call_site,
            target_symbol=sig.symbol_name,
            error_type="TOO_MANY_ARGS",
            description=(
                f"Too many positional arguments. Function accepts at most {effective_max_pos}, "
                f"but call supplies {pos_count}."
            ),
            confidence=confidence,
        )

    # Check for missing required keyword-only arguments
    missing_req_kw = sig.required_kwonly_args - kw_names
    if missing_req_kw:
        return BrokenCallSite(
            call_site=call_site,
            target_symbol=sig.symbol_name,
            error_type="MISSING_KEYWORD_ARG",
            description=(f"Missing required keyword-only argument(s): {sorted(missing_req_kw)}."),
            confidence=confidence,
        )

    # Check for unexpected keyword arguments
    if not sig.has_var_keyword:
        valid_kw_names = {
            p.name for p in sig.parameters if p.kind in ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")
        }
        unexpected_kw = kw_names - valid_kw_names
        if unexpected_kw:
            return BrokenCallSite(
                call_site=call_site,
                target_symbol=sig.symbol_name,
                error_type="UNEXPECTED_KEYWORD",
                description=(
                    f"Unexpected keyword argument(s): {sorted(unexpected_kw)}. "
                    f"Valid keyword arguments are: {sorted(valid_kw_names)}."
                ),
                confidence=confidence,
            )

    return None


class SymbolImpactAnalyzer:
    """High-speed cross-file symbol impact and breaking signature analyzer."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root or Path.cwd()
        self.parse_errors: list[str] = []

    def find_call_sites_in_repo(
        self,
        symbol_name: str,
        exclude_paths: set[str] | None = None,
    ) -> list[CallSiteSpec]:
        """Scan repository for all call sites referencing the target symbol.

        Uses fast substring pre-filtering to parse AST only on candidate files.
        """
        exclude = exclude_paths or set()
        call_sites: list[CallSiteSpec] = []

        # Candidate scan targets: src/ and tests/
        target_dirs = [self.repo_root / "src", self.repo_root / "tests"]
        py_files: list[Path] = []
        for d in target_dirs:
            if d.is_dir():
                py_files.extend(d.rglob("*.py"))

        for file_path in py_files:
            rel_path = str(file_path.relative_to(self.repo_root))
            if rel_path in exclude or any(part.startswith((".", "__")) for part in file_path.parts):
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as err:
                logger.debug("Failed reading file %s during symbol search: %s", file_path, err)
                continue

            # Fast substring filter (<5ms total runtime across repo)
            if symbol_name not in content:
                continue

            try:
                tree = ast.parse(content, filename=rel_path)
            except SyntaxError:
                continue

            visitor = CallSiteVisitor(target_symbol=symbol_name, file_path=rel_path)
            visitor.visit(tree)
            call_sites.extend(visitor.call_sites)

        return call_sites

    def analyze_symbol(
        self,
        file_path: str,
        symbol_name: str,
        old_code: str | None = None,
    ) -> SymbolImpactReport:
        """Analyze cross-file impact of a specific symbol in a file."""
        abs_file = self.repo_root / file_path
        if not abs_file.is_file():
            return SymbolImpactReport(
                symbol_name=symbol_name,
                file_path=file_path,
                sig_diff=None,
                total_call_sites_found=0,
                broken_call_sites=[],
                compatible_call_sites=[],
            )

        new_code = abs_file.read_text(encoding="utf-8", errors="replace")
        new_sigs = extract_signatures_from_code(new_code, file_path=file_path)
        new_sig = new_sigs.get(symbol_name)

        sig_diff: SignatureDiff | None = None
        if old_code is not None:
            old_sigs = extract_signatures_from_code(old_code, file_path=file_path)
            old_sig = old_sigs.get(symbol_name)
            sig_diff = compare_signatures(old_sig, new_sig)

        if not new_sig:
            return SymbolImpactReport(
                symbol_name=symbol_name,
                file_path=file_path,
                sig_diff=sig_diff,
                total_call_sites_found=0,
                broken_call_sites=[],
                compatible_call_sites=[],
            )

        call_sites = self.find_call_sites_in_repo(
            symbol_name=symbol_name,
            exclude_paths={file_path},
        )

        broken_list: list[BrokenCallSite] = []
        compatible_list: list[CallSiteSpec] = []

        for cs in call_sites:
            err = verify_call_site_compatibility(cs, new_sig)
            if err:
                broken_list.append(err)
            else:
                compatible_list.append(cs)

        return SymbolImpactReport(
            symbol_name=symbol_name,
            file_path=file_path,
            sig_diff=sig_diff,
            total_call_sites_found=len(call_sites),
            broken_call_sites=broken_list,
            compatible_call_sites=compatible_list,
        )

    def analyze_modified_files(
        self,
        changed_files: list[str],
        baseline_loader: Callable[[str], str | None] | None = None,
    ) -> list[SymbolImpactReport]:
        """Audit all modified Python callables across repository callers."""
        py_files = [f for f in changed_files if f.endswith(".py")]
        reports: list[SymbolImpactReport] = []

        for rel_path in py_files:
            abs_file = self.repo_root / rel_path
            if not abs_file.is_file():
                continue

            try:
                new_code = abs_file.read_text(encoding="utf-8", errors="replace")
            except OSError as read_err:
                logger.debug("Failed reading %s: %s", abs_file, read_err)
                self.parse_errors.append(f"{rel_path}: OSError: {read_err}")
                continue

            # Fetch baseline code via baseline_loader if provided
            old_code: str | None = None
            if baseline_loader is not None:
                try:
                    old_code = baseline_loader(rel_path)
                except Exception as load_err:
                    logger.debug("Could not fetch baseline for %s: %s", rel_path, load_err)

            new_sigs = extract_signatures_from_code(
                new_code, file_path=rel_path, error_collector=self.parse_errors
            )
            old_sigs = (
                extract_signatures_from_code(old_code or "", file_path=rel_path) if old_code else {}
            )

            for sym_name, new_sig in new_sigs.items():
                # Skip private symbols or dunder methods
                if sym_name.startswith("_") and not sym_name.startswith("__"):
                    continue

                old_sig = old_sigs.get(sym_name)
                # If symbol signature changed, or if baseline is unavailable, check call sites
                if old_sig is not None:
                    diff = compare_signatures(old_sig, new_sig)
                    if not diff.is_breaking and not diff.reasons:
                        continue
                else:
                    diff = None

                rep = self.analyze_symbol(
                    file_path=rel_path,
                    symbol_name=sym_name,
                    old_code=old_code,
                )
                if (
                    rep.broken_call_sites
                    or (rep.sig_diff and rep.sig_diff.is_breaking)
                    or rep.total_call_sites_found > 0
                ):
                    reports.append(rep)

        return reports


def evaluate_contract_integrity(
    reports: list[SymbolImpactReport],
    parse_errors: list[str] | None = None,
) -> ScorecardItem:
    """Evaluate Dimension 4 (Contract Integrity) from cross-file symbol impact and AST parse status.

    Confidence tiering:
    - ERROR: AST parse errors or syntax failures in analyzed files.
    - FAIL (PROVEN): Incompatible call sites directly invoking modified symbols.
    - WARN (SUSPECTED): Incompatible dynamic/attribute call sites, or breaking signature changes
      without identified internal call sites.
    - PASS: All cross-file signatures and call sites compatible, or no contract changes.
    """
    if parse_errors:
        err_msg = "; ".join(parse_errors[:3])
        if len(parse_errors) > 3:
            err_msg += f" (+{len(parse_errors) - 3} more)"
        is_unknown = any("UNKNOWN" in e.upper() for e in parse_errors)
        status: Literal["UNKNOWN", "ERROR"] = "UNKNOWN" if is_unknown else "ERROR"
        prefix = "UNKNOWN: Analysis incomplete" if is_unknown else "ERROR: AST parse failure on"
        return ScorecardItem(
            dimension="Contract Integrity",
            status=status,
            notes=f"{prefix}: {err_msg}",
        )

    proven_broken: list[BrokenCallSite] = []
    suspected_broken: list[BrokenCallSite] = []
    for rep in reports:
        for b in rep.broken_call_sites:
            if b.confidence == "PROVEN":
                proven_broken.append(b)
            else:
                suspected_broken.append(b)

    if proven_broken:
        citations: list[str] = []
        for b in proven_broken[:3]:
            cs = b.call_site
            citations.append(
                f"'{b.target_symbol}' at {cs.file_path}:{cs.line_number} ({b.error_type})"
            )
        citation_str = "; ".join(citations)
        if len(proven_broken) > 3:
            citation_str += f" (+{len(proven_broken) - 3} more)"
        return ScorecardItem(
            dimension="Contract Integrity",
            status="FAIL",
            notes=f"PROVEN: Broken call site(s) detected for {citation_str}.",
        )

    if suspected_broken:
        citations = []
        for b in suspected_broken[:3]:
            cs = b.call_site
            citations.append(
                f"'{b.target_symbol}' at {cs.file_path}:{cs.line_number} ({b.error_type})"
            )
        citation_str = "; ".join(citations)
        if len(suspected_broken) > 3:
            citation_str += f" (+{len(suspected_broken) - 3} more)"
        return ScorecardItem(
            dimension="Contract Integrity",
            status="WARN",
            notes=f"SUSPECTED: Dynamic call site(s) may be affected: {citation_str}.",
        )

    breaking_diffs = [rep for rep in reports if rep.sig_diff and rep.sig_diff.is_breaking]
    if breaking_diffs:
        sym_names = [f"'{r.symbol_name}'" for r in breaking_diffs[:3]]
        sym_str = ", ".join(sym_names)
        if len(breaking_diffs) > 3:
            sym_str += f" (+{len(breaking_diffs) - 3} more)"
        return ScorecardItem(
            dimension="Contract Integrity",
            status="WARN",
            notes=f"SUSPECTED: Breaking signature change detected on {sym_str}, but no internal callers were broken.",
        )

    if reports:
        return ScorecardItem(
            dimension="Contract Integrity",
            status="PASS",
            notes="PASS: All cross-file signatures and call sites compatible.",
        )

    return ScorecardItem(
        dimension="Contract Integrity",
        status="PASS",
        notes="PASS: No breaking contract changes detected.",
    )
