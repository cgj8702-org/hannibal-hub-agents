"""Backward-compatibility shim. Canonical module is webhook_agent.analysis.diff_filter."""

from __future__ import annotations

import sys

from webhook_agent.analysis import diff_filter as _canonical
from webhook_agent.analysis.diff_filter import *  # noqa: F403

sys.modules[__name__] = _canonical
