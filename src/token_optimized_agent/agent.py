"""Agent definitions implementing task mode, history omission, and generation token caps."""

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from src.token_optimized_agent.callbacks import truncate_tool_response_callback
from src.token_optimized_agent.tools import (
    lookup_config,
    save_large_data_artifact,
)


class AnalysisOutput(BaseModel):
    summary: str = Field(description="Brief summary of findings.")
    action_items: list[str] = Field(description="Key action items.")


analyst_agent = Agent(
    name="analyst",
    model="gemini-3.6-flash",
    mode="task",
    output_schema=AnalysisOutput,
    description="Analyzes complex data payloads and extracts key insights.",
    instruction=(
        "Analyze the input data concisely. Extract key points and call"
        " finish_task when completed."
    ),
    generate_content_config=genai_types.GenerateContentConfig(
        max_output_tokens=512,
        temperature=0.2,
    ),
)

lookup_agent = Agent(
    name="lookup_helper",
    model="gemini-3.6-flash",
    description="Performs quick configuration lookups.",
    instruction=(
        "You perform key-value lookups using lookup_config. Keep responses brief."
    ),
    include_contents="none",
    tools=[lookup_config],
    generate_content_config=genai_types.GenerateContentConfig(
        max_output_tokens=256,
    ),
)

root_agent = Agent(
    name="coordinator",
    model="gemini-3.6-flash",
    description="Main coordinator agent optimized for minimal token usage.",
    instruction=(
        "You are the main coordinator. Route tasks to sub-agents or use"
        " save_large_data_artifact for large payloads to minimize prompt"
        " bloat."
    ),
    sub_agents=[analyst_agent, lookup_agent],
    tools=[save_large_data_artifact],
    generate_content_config=genai_types.GenerateContentConfig(
        max_output_tokens=1024,
    ),
    after_tool_callback=truncate_tool_response_callback,
)
