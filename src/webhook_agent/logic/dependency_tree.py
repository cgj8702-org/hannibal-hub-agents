"""Transitive dependency resolution and grounding for lockfile diff audits.

Extracts direct and transitive dependency trees from lockfiles (e.g. uv.lock)
to ground the auditor and prevent hallucinations regarding valid resolver bumps.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("webhook_agent.dependency_tree")


@dataclass
class LockfilePackage:
    name: str
    version: str = ""
    dependencies: list[str] = field(default_factory=list)


def parse_lockfile_dependency_graph(lockfile_text: str) -> dict[str, list[str]]:
    """Parse a uv.lock text into an adjacency list of package -> list of dependency names.

    Args:
        lockfile_text: Content of uv.lock file.

    Returns:
        Dictionary mapping package_name -> [dependency_names].
    """
    graph: dict[str, list[str]] = {}
    current_pkg: str | None = None
    in_dependencies_block = False

    for line in lockfile_text.splitlines():
        line_strip = line.strip()
        if line_strip.startswith("[[package]]"):
            current_pkg = None
            in_dependencies_block = False
            continue

        name_match = re.match(r'^name\s*=\s*"([^"]+)"', line_strip)
        if name_match and current_pkg is None:
            current_pkg = name_match.group(1).lower().strip()
            graph.setdefault(current_pkg, [])
            continue

        if line_strip.startswith("dependencies = ["):
            in_dependencies_block = True
            continue

        if in_dependencies_block:
            if line_strip == "]":
                in_dependencies_block = False
                continue
            dep_match = re.search(r'name\s*=\s*"([^"]+)"', line_strip)
            if dep_match and current_pkg:
                dep_name = dep_match.group(1).lower().strip()
                graph[current_pkg].append(dep_name)

    return graph


def get_transitive_closure(
    root_packages: list[str],
    graph: dict[str, list[str]],
) -> set[str]:
    """Compute the set of all direct and transitive dependency names for the given root packages.

    Args:
        root_packages: List of primary packages modified/bumped.
        graph: Adjacency list from parse_lockfile_dependency_graph.

    Returns:
        Set of all transitive dependency package names reachable from root_packages.
    """
    visited: set[str] = set()
    queue = [p.lower().strip() for p in root_packages]

    while queue:
        pkg = queue.pop(0)
        for dep in graph.get(pkg, []):
            if dep not in visited:
                visited.add(dep)
                queue.append(dep)

    return visited


def build_dependency_grounding_context(
    bumped_packages: list[str],
    lockfile_text: str,
) -> str:
    """Generate a clinical dependency grounding block for the auditor LLM prompt.

    Args:
        bumped_packages: Names of packages bumped in the PR.
        lockfile_text: Full text or relevant sections of the target uv.lock.

    Returns:
        Markdown-formatted context block explaining primary vs. valid transitive packages.
    """
    if not bumped_packages or not lockfile_text:
        return ""

    graph = parse_lockfile_dependency_graph(lockfile_text)
    norm_bumped = [b.lower().strip() for b in bumped_packages]

    # Find which packages are primary candidates (or if all modified, check reachability)
    closure = get_transitive_closure(norm_bumped, graph)

    if not closure:
        return ""

    verified_transitive = sorted(closure)
    return f"""### 📦 Verified Transitive Dependency Grounding
* **Primary Modified Package(s):** {", ".join(f"`{p}`" for p in bumped_packages)}
* **Verified Transitive Subtree:** {", ".join(f"`{p}`" for p in verified_transitive)}
* **Auditor Policy:** Any lockfile updates to packages listed under the *Verified Transitive Subtree* are standard resolver behavior for `{", ".join(bumped_packages)}`. Do NOT flag updates to these verified transitive dependencies as unauthorized scope creep or critical issues.
"""
