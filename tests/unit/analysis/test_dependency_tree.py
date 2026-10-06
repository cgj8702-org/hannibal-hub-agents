"""Unit tests for dependency_tree transitive closure and grounding."""

from webhook_agent.analysis.dependency_tree import (
    build_dependency_grounding_context,
    get_transitive_closure,
    parse_lockfile_dependency_graph,
)

SAMPLE_UV_LOCK = """
[[package]]
name = "ast-serialize"
version = "0.6.0"

[[package]]
name = "librt"
version = "0.16.0"

[[package]]
name = "mypy-extensions"
version = "1.0.0"

[[package]]
name = "mypy"
version = "2.3.1"
dependencies = [
    { name = "ast-serialize" },
    { name = "librt", marker = "platform_python_implementation != 'PyPy'" },
    { name = "mypy-extensions" },
    { name = "typing-extensions" },
]

[[package]]
name = "typing-extensions"
version = "4.12.2"

[[package]]
name = "unrelated-pkg"
version = "1.0.0"
"""


def test_parse_lockfile_dependency_graph():
    graph = parse_lockfile_dependency_graph(SAMPLE_UV_LOCK)
    assert "mypy" in graph
    assert "librt" in graph["mypy"]
    assert "ast-serialize" in graph["mypy"]
    assert "mypy-extensions" in graph["mypy"]
    assert "typing-extensions" in graph["mypy"]


def test_get_transitive_closure_mypy():
    graph = parse_lockfile_dependency_graph(SAMPLE_UV_LOCK)
    closure = get_transitive_closure(["mypy"], graph)
    assert "librt" in closure
    assert "ast-serialize" in closure
    assert "mypy-extensions" in closure
    assert "typing-extensions" in closure
    assert "unrelated-pkg" not in closure


def test_build_dependency_grounding_context():
    context = build_dependency_grounding_context(["mypy"], SAMPLE_UV_LOCK)
    assert "Verified Transitive Dependency Grounding" in context
    assert "`mypy`" in context
    assert "`librt`" in context
    assert (
        "Do NOT flag updates to these verified transitive dependencies as unauthorized scope creep"
        in context
    )
