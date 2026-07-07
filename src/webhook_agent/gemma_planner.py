"""DEPRECATED: Replaced by webhook_agent.py (ADK-powered agent).

This file is kept as a reference only. All functionality has been migrated
to the ADK-based WebhookAgent in webhook_agent.py.

Key changes:
- Old: google-genai Interactions API with manual tool schemas
- New: google-adk Agent with automatic tool declaration from Python functions
- Old: One-shot planning (stateless, no memory)
- New: ADK Runner with InMemorySessionService + ChromaDBMemoryService
"""

from __future__ import annotations
