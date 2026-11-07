"""Event routing, perimeter filtering, and deterministic fast-paths for GitHub webhooks.

Package structure:
- event_filter: Tier 0 pure perimeter drops, bot loop detection, authorization, and canonical routing.
- tier1_router: Tier 1 deterministic programmatic routing, debouncing, CI gating, and dependency fast-paths.
"""

from __future__ import annotations

from webhook_agent.events.event_filter import (
    SUPPRESSED_EVENT_NAMES,
    SUPPRESSED_PR_METADATA_ACTIONS,
    filter_tier_0,
    is_actionable_issue_opened,
    route_event,
    should_process_event,
)
from webhook_agent.events.tier1_router import (
    Tier1Status,
    handle_ci_completed,
    handle_tier_1_programmatic,
    review_if_ci_green,
)

__all__ = [
    "SUPPRESSED_EVENT_NAMES",
    "SUPPRESSED_PR_METADATA_ACTIONS",
    "Tier1Status",
    "filter_tier_0",
    "handle_ci_completed",
    "handle_tier_1_programmatic",
    "is_actionable_issue_opened",
    "review_if_ci_green",
    "route_event",
    "should_process_event",
]
