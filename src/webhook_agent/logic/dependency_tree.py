"""Backward-compatibility shim. Canonical module is webhook_agent.analysis.dependency_tree."""

from __future__ import annotations

import sys

from webhook_agent.analysis import dependency_tree as _canonical
from webhook_agent.analysis.dependency_tree import *  # noqa: F403

sys.modules[__name__] = _canonical
