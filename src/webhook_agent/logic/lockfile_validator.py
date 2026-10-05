"""Backward-compatibility shim. Canonical module is webhook_agent.analysis.lockfile_validator."""

from __future__ import annotations

import sys

from webhook_agent.analysis import lockfile_validator as _canonical
from webhook_agent.analysis.lockfile_validator import *  # noqa: F403

sys.modules[__name__] = _canonical
