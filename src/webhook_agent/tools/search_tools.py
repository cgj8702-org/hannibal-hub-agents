"""Sandboxed code search tool.

Lifts the sandboxed file search pattern from Google's long-horizon-harness,
executing an isolated directory walk script within the active environment with
binary extension skipping, file size limits, regex pattern matching, and snippet windowing.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from webhook_agent.environment.context import active_environment

_SEARCH_DELIMITER = "__HANNIBAL_SEARCH_RESULT__"
_SEARCH_ERROR_KEY = "__search_error__"
_SEARCH_MAX_FILE_SIZE = 500_000  # 500 KB limit per file
_SEARCH_SNIPPET_MAX_CHARS = 200  # max characters in preview snippet

_BINARY_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".dll",
    ".bin",
    ".exe",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
)

_SEARCH_IGNORED_DIRS = (
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "node_modules",
    ".tox",
    ".idea",
    ".vscode",
)


def _build_search_script(
    root: str,
    pattern: str,
    file_glob: str | None,
    limit: int,
    ignore_case: bool = False,
    no_ignore: bool = False,
) -> str:
    """Build a standalone python script that searches files and prints delimited JSON."""
    return (
        "import fnmatch, json, os, re, sys\n"
        f"_root = {root!r}\n"
        f"_pattern = {pattern!r}\n"
        f"_file_glob = {file_glob!r}\n"
        f"_limit = {limit!r}\n"
        f"_ignore_case = {ignore_case!r}\n"
        f"_no_ignore = {no_ignore!r}\n"
        f"_error_key = {_SEARCH_ERROR_KEY!r}\n"
        f"_delim = {_SEARCH_DELIMITER!r}\n"
        f"_binary = {set(_BINARY_EXTENSIONS)!r}\n"
        f"_ignored = {set(_SEARCH_IGNORED_DIRS)!r}\n"
        f"_max_size = {_SEARCH_MAX_FILE_SIZE!r}\n"
        f"_snip_max = {_SEARCH_SNIPPET_MAX_CHARS!r}\n"
        "try:\n"
        "    _re = re.compile(_pattern, re.IGNORECASE if _ignore_case else 0)\n"
        "except re.error as _exc:\n"
        "    print(_delim + json.dumps({_error_key: str(_exc)}) + _delim)\n"
        "    sys.exit(0)\n"
        "if not os.path.isdir(_root):\n"
        "    sys.stderr.write('search path not a directory: ' + _root + '\\n')\n"
        "    sys.exit(2)\n"
        "_real_root = os.path.realpath(_root) + os.sep\n"
        "_matches = []\n"
        "for _dp, _dns, _fns in os.walk(_root):\n"
        "    _dns[:] = [d for d in _dns if not d.startswith('.') and (_no_ignore or d not in _ignored)]\n"
        "    for _fn in _fns:\n"
        "        if _fn.startswith('.'):\n"
        "            continue\n"
        "        if _file_glob and not fnmatch.fnmatch(_fn, _file_glob):\n"
        "            continue\n"
        "        _dot = _fn.rfind('.')\n"
        "        if _dot != -1 and _fn[_dot:].lower() in _binary:\n"
        "            continue\n"
        "        _full = os.path.join(_dp, _fn)\n"
        "        if os.path.islink(_full) or not os.path.realpath(_full).startswith(_real_root):\n"
        "            continue\n"
        "        try:\n"
        "            _st = os.stat(_full)\n"
        "            if _st.st_size > _max_size:\n"
        "                continue\n"
        "            _mtime = _st.st_mtime\n"
        "        except OSError:\n"
        "            continue\n"
        "        try:\n"
        "            with open(_full, 'r', encoding='utf-8', errors='replace') as _fh:\n"
        "                for _ln, _line in enumerate(_fh, start=1):\n"
        "                    _line = _line.rstrip('\\n')\n"
        "                    _m = _re.search(_line)\n"
        "                    if _m:\n"
        "                        _left = max(0, _m.start() - 100)\n"
        "                        _snip = _line[_left:_left + _snip_max]\n"
        "                        if _left > 0:\n"
        "                            _snip = '...' + _snip\n"
        "                        if _left + _snip_max < len(_line):\n"
        "                            _snip = _snip + '...'\n"
        "                        _rel = os.path.relpath(_full, _root)\n"
        "                        _matches.append({'path': _rel, 'line': _ln, 'text': _snip, 'mtime': _mtime})\n"
        "                        if len(_matches) >= _limit:\n"
        "                            break\n"
        "        except OSError:\n"
        "            continue\n"
        "        if len(_matches) >= _limit:\n"
        "            break\n"
        "    if len(_matches) >= _limit:\n"
        "        break\n"
        "print(_delim + json.dumps(_matches) + _delim)\n"
    )


def _extract_delimited(text: str, delimiter: str) -> str | None:
    """Extract content between delimiters."""
    start = text.find(delimiter)
    if start == -1:
        return None
    end = text.find(delimiter, start + len(delimiter))
    if end == -1:
        return None
    return text[start + len(delimiter) : end]


async def search_files(
    pattern: str,
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
    ignore_case: bool = False,
    no_ignore: bool = False,
) -> dict[str, Any]:
    """Search text files within the environment for regex `pattern`.

    Args:
        pattern: Regex pattern to search for.
        path: Relative path within the environment to search from (default: ".").
        file_glob: Optional glob pattern to filter filenames (e.g. "*.py").
        limit: Maximum number of matches to return (default: 50).
        ignore_case: Whether to perform case-insensitive regex matching.
        no_ignore: If True, do not skip ignored directories (e.g. .venv).

    Returns:
        Dict with "matches" (list of match dicts) and "total" (int count), or
        "error" string if search failed.
    """
    if not pattern:
        return {"error": "Search pattern cannot be empty", "matches": [], "total": 0}

    if len(pattern) > 500:
        return {
            "error": "Search pattern exceeds maximum allowed length of 500 characters",
            "matches": [],
            "total": 0,
        }

    limit = max(1, min(limit, 200))

    target_env = active_environment()
    root_resolved = target_env.resolve_path(path)
    if root_resolved is None:
        return {
            "error": f"Access denied: search path '{path}' escapes environment boundary",
            "matches": [],
            "total": 0,
        }

    script = _build_search_script(
        root=str(root_resolved),
        pattern=pattern,
        file_glob=file_glob,
        limit=limit,
        ignore_case=ignore_case,
        no_ignore=no_ignore,
    )

    command = f"python3 -c {shlex.quote(script)}"
    result = await target_env.execute(command, timeout=30.0)

    if not result.ok:
        msg = result.stderr.strip() or "search execution failed"
        return {"error": f"Search failed: {msg}", "matches": [], "total": 0}

    payload = _extract_delimited(result.stdout, _SEARCH_DELIMITER)
    if payload is None:
        return {
            "error": f"Search produced no valid result delimiter; stderr={result.stderr!r}",
            "matches": [],
            "total": 0,
        }

    try:
        data = json.loads(payload)
        if isinstance(data, dict) and _SEARCH_ERROR_KEY in data:
            return {
                "error": f"Invalid search regex '{pattern}': {data[_SEARCH_ERROR_KEY]}",
                "matches": [],
                "total": 0,
            }
        if isinstance(data, list):
            return {"matches": data, "total": len(data)}
        return {"matches": [], "total": 0}
    except json.JSONDecodeError as exc:
        return {"error": f"Failed parsing search JSON output: {exc}", "matches": [], "total": 0}
