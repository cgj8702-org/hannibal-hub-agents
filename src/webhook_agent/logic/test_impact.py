"""Backward-compatibility shim. Canonical module is webhook_agent.analysis.test_impact."""

from __future__ import annotations

import sys

from webhook_agent.analysis import test_impact as _canonical
from webhook_agent.analysis.test_impact import *  # noqa: F403

sys.modules[__name__] = _canonical
