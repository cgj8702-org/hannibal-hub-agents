"""ADK FunctionTools for cross-file symbol impact and breaking signature analysis."""

from __future__ import annotations

import logging

from google.adk.tools import FunctionTool

from webhook_agent.analysis.symbol_graph import SymbolImpactAnalyzer

logger = logging.getLogger("webhook_agent.tools.symbol_tools")


def check_symbol_impact(
    file_path: str,
    symbol_name: str,
) -> str:
    """Audit cross-file callers and contract compatibility for a Python symbol.

    Scans the repository to locate all invocation sites calling the specified function
    or method, checks argument compatibility against its signature, and reports any
    broken call sites that would crash at runtime.

    Args:
        file_path: Path to the Python file defining the symbol (e.g. 'src/webhook_agent/formatter.py').
        symbol_name: Name of the function, method, or class (e.g. 'calculate_strict_verdict').

    Returns:
        A clinical Markdown report summarizing call sites, breaking contract changes,
        and broken invocation sites across other files.
    """
    cleaned_path = file_path[2:] if file_path.startswith(("a/", "b/")) else file_path
    analyzer = SymbolImpactAnalyzer()
    report = analyzer.analyze_symbol(file_path=cleaned_path, symbol_name=symbol_name)
    return report.to_markdown()


check_symbol_impact_tool = FunctionTool(check_symbol_impact)
