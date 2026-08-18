"""App configuration with EventsCompactionConfig, ContextCacheConfig, and MessagePruningPlugin."""

from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

try:
    from logic.model_factory import get_adk_model
except ImportError:
    from src.logic.model_factory import get_adk_model

from src.token_optimized_agent.agent import root_agent
from src.token_optimized_agent.callbacks import MessagePruningPlugin

summarizer_llm = get_adk_model(model_name="gemini-3.6-flash")

app = App(
    name="token_optimized_app",
    root_agent=root_agent,
    plugins=[MessagePruningPlugin(max_history_events=20)],
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
