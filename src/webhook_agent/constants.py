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
DEFAULT_FIRESTORE_PROJECT = DEFAULT_PUBSUB_PROJECT
DEFAULT_WEBHOOK_PAID_PROJECT = "cgj8702-webhook-agent"
DEFAULT_WEBHOOK_FREE_PROJECT = "gen-lang-client-0615466973"
DEFAULT_COMPUTE_HOST_PROJECT = "chatbot-project-hannibal"

# --- PubSub Topic & Subscription Paths ---
DEFAULT_PUBSUB_TOPIC = f"projects/{DEFAULT_PUBSUB_PROJECT}/topics/webhooks"
DEFAULT_PUBSUB_SUBSCRIPTION = f"projects/{DEFAULT_PUBSUB_PROJECT}/subscriptions/webhooks-sub"
DEFAULT_PUBSUB_DEAD_LETTER_TOPIC = f"projects/{DEFAULT_PUBSUB_PROJECT}/topics/webhooks-dead-letter"

# --- Operational Policy Defaults ---
# Fail open / allowed by default: automated mutations enabled out of the box.
DEFAULT_ALLOW_AUTOMATED_MUTATIONS = "1"
# Fail closed: citation grounding is measured (logged) first and only enforced once its
# false-positive rate is known. See docs/plans/reviewer_hardening_plan.md Phase 2.2.
DEFAULT_STRICT_REVIEW_GROUNDING = "0"
# Hold automatic reviews until the PR head commit has fully green CI (silent skip on failure).
# Set REVIEW_WAIT_FOR_CI=0 to restore review-immediately behaviour. See review/ci_gate.py.
DEFAULT_REVIEW_WAIT_FOR_CI = "1"
DEFAULT_WEBHOOK_TIER = "free"
