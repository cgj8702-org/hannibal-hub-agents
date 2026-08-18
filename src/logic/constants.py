"""Centralized non-sensitive infrastructure constants and static defaults.

These constants serve as non-sensitive defaults across the repository,
allowing the application to run out-of-the-box while still permitting
environment variable overrides via os.getenv("VAR_NAME", DEFAULT_CONSTANT).
"""

from __future__ import annotations

# --- GitHub App Constants ---
DEFAULT_GITHUB_APP_ID = "4133145"
DEFAULT_GITHUB_INSTALLATION_ID = "150411146"
DEFAULT_GITHUB_REPOSITORY = "cgj8702-org/hannibal-hub-agents"

# --- GCP Multi-Project Identifiers ---
DEFAULT_PUBSUB_PROJECT = "cgj8702-webhook-agent"
DEFAULT_WEBHOOK_PAID_PROJECT = "cgj8702-webhook-agent"
DEFAULT_WEBHOOK_FREE_PROJECT = "gen-lang-client-0615466973"
DEFAULT_FEATURE_AGENT_PROJECT = "gen-lang-client-0613181237"
DEFAULT_COMPUTE_HOST_PROJECT = "chatbot-project-hannibal"

# --- PubSub Topic & Subscription Paths ---
DEFAULT_PUBSUB_TOPIC = f"projects/{DEFAULT_PUBSUB_PROJECT}/topics/webhooks"
DEFAULT_PUBSUB_SUBSCRIPTION = (
    f"projects/{DEFAULT_PUBSUB_PROJECT}/subscriptions/webhooks-sub"
)
DEFAULT_PUBSUB_DEAD_LETTER_TOPIC = (
    f"projects/{DEFAULT_PUBSUB_PROJECT}/topics/webhooks-dead-letter"
)

# --- Operational Policy Defaults ---
DEFAULT_ALLOW_AUTOMATED_MUTATIONS = "1"
DEFAULT_WEBHOOK_TIER = "free"
