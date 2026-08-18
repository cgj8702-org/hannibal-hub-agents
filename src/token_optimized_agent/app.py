"""App configuration with EventsCompactionConfig, ContextCacheConfig, and MessagePruningPlugin."""

from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

from src.token_optimized_agent.agent import root_agent
from src.token_optimized_agent.callbacks import MessagePruningPlugin


def build_token_optimized_app() -> App:
    """Construct the token_optimized ADK App instance dynamically."""
    from logic.analytics import CloudLoggingAnalyticsPlugin
    from logic.model_factory import get_adk_model

    summarizer_llm = get_adk_model(model_name="gemini-3.6-flash")

    return App(
        name="token_optimized_app",
        root_agent=root_agent,
        plugins=[
            MessagePruningPlugin(max_history_events=20),
            CloudLoggingAnalyticsPlugin(),
        ],
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=15,
            overlap_size=2,
            summarizer=LlmEventSummarizer(llm=summarizer_llm),
        ),
        context_cache_config=ContextCacheConfig(
            min_tokens=2048,
            ttl_seconds=1800,
            cache_intervals=5,
        ),
    )


# Helper factory for constructing the token_optimized ADK App
__all__ = ["build_token_optimized_app"]
