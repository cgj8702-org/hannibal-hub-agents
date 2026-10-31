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
WEBHOOK_PAID_PROJECT = "cgj8702-webhook-agent"
WEBHOOK_FREE_PROJECT = "gen-lang-client-0615466973"
CHATBOT_PAID_PROJECT = "chatbot-project-hannibal"
CHATBOT_FREE_PROJECT = "gen-lang-client-0035989819"
DEFAULT_WEBHOOK_PAID_PROJECT = WEBHOOK_PAID_PROJECT
DEFAULT_WEBHOOK_FREE_PROJECT = WEBHOOK_FREE_PROJECT
DEFAULT_PUBSUB_PROJECT = WEBHOOK_PAID_PROJECT
DEFAULT_FIRESTORE_PROJECT = WEBHOOK_PAID_PROJECT
DEFAULT_COMPUTE_HOST_PROJECT = CHATBOT_PAID_PROJECT

# --- PubSub Topic & Subscription Paths ---
PUBSUB_PROJECT = WEBHOOK_PAID_PROJECT
PUBSUB_TOPIC = f"projects/{PUBSUB_PROJECT}/topics/webhooks"
PUBSUB_SUBSCRIPTION = f"projects/{PUBSUB_PROJECT}/subscriptions/webhooks-sub"
PUBSUB_DEAD_LETTER_TOPIC = f"projects/{PUBSUB_PROJECT}/topics/webhooks-dead-letter"
DEFAULT_PUBSUB_TOPIC = PUBSUB_TOPIC
DEFAULT_PUBSUB_SUBSCRIPTION = PUBSUB_SUBSCRIPTION
DEFAULT_PUBSUB_DEAD_LETTER_TOPIC = PUBSUB_DEAD_LETTER_TOPIC

# --- Operational Policy Defaults ---
ENABLE_DYNAMIC_MODEL_ROUTING = "1"
ENABLE_PR_WORKSPACE = "1"
STRICT_REVIEW_GROUNDING = "1"
ENABLE_CODERABBIT_AUTO_RESOLVE = "true"
ALLOW_AUTOMATED_MUTATIONS = "1"
# Hold automatic reviews until the PR head commit has fully green CI (silent skip on failure).
# Set REVIEW_WAIT_FOR_CI=0 to restore review-immediately behaviour. See review/ci_gate.py.
DEFAULT_REVIEW_WAIT_FOR_CI = "1"
# Primary autonomous PR reviewer: automatically review opened/synchronized PRs once CI is green.
# Set ENABLE_AUTOMATIC_PR_REVIEWS=0 to require manual /review triggers.
DEFAULT_ENABLE_AUTOMATIC_PR_REVIEWS = "1"
DEFAULT_WEBHOOK_TIER = "free"
# Author associations allowed to trigger agent runs (comma-separated).
DEFAULT_TRIGGER_ALLOWED_ASSOCIATIONS = "OWNER,MEMBER,COLLABORATOR,CONTRIBUTOR"
DEFAULT_TRIGGER_EXEMPT_LOGINS = "cgj8702,cgj8702-agents,dependabot[bot]"

# Repositories with native GitHub branch protection that auto-dismisses stale reviews on push.
# The bot skips manual review dismissal on these repositories (comma-separated).
DEFAULT_NATIVE_STALE_REVIEW_DISMISSAL_REPOS = "cgj8702-org/hannibal-hub-agents"
