"""webhook_agent package (root)

This mirrors the starter placed under `webhook_agent_starter/` but lives at the repository root
so this package can be imported/installed from the top-level project.
"""

from .app import app

__all__ = ["app"]
