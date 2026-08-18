"""Standalone Autonomous Feature Engineering Agent package (feature_agent).

Powered by FEATURE_AGENT_FREE_KEY for complete quota isolation and Google Cloud
Firestore for durable task checkpoints and 429 auto-resumption.
"""

from __future__ import annotations

from feature_agent.agent import build_feature_app, build_feature_developer_agent
from feature_agent.firestore_checkpoints import firestore_checkpoint_registry
from feature_agent.runner import FeatureTaskRunner

__all__ = [
    "build_feature_developer_agent",
    "build_feature_app",
    "firestore_checkpoint_registry",
    "FeatureTaskRunner",
]
