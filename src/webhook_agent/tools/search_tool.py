"""Google Search grounding tool with strict programmatic rules for Webhook Agent.

Enforces:
- Supported strictly on Gemini models (gemini-3.5-flash-lite).
- Programmatically capped at max 3 searches per request session.
- Mandatory URL citation formatting.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents.context import Context

logger = logging.getLogger("webhook_agent.search")

MAX_SEARCH_CALLS_PER_SESSION = 3


def google_search_grounding_tool(ctx: Context, query: str) -> str:
    """Perform a Google Search to retrieve current web information with citations.

    Strict programmatic rules:
    - Supported only on Gemini models (gemini-3.5-flash-lite).
    - Capped at max 3 search calls per request session.
    - Grounded URL citations required.

    Args:
        query: Clear search query string.

    Returns:
        Structured search summary with URL citations.
    """
    if not query or not query.strip():
        return "Error: Empty search query provided."

    # Enforce session call limits
    current_count = int(ctx.state.get("search_count") or 0)
    if current_count >= MAX_SEARCH_CALLS_PER_SESSION:
        logger.warning(
            "Search call quota exceeded (%d/%d) for session",
            current_count,
            MAX_SEARCH_CALLS_PER_SESSION,
        )
        return (
            f"Error: Google Search tool limit reached ({MAX_SEARCH_CALLS_PER_SESSION} "
            f"calls per request session)."
        )

    # Increment session search counter
    ctx.state["search_count"] = current_count + 1

    try:
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("CHATBOT_FREE_KEY")
        if not api_key:
            return "Error: No Gemini API key available for Google Search tool."

        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=f"Search query: {query.strip()}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )

        text = response.text or "No text output returned."

        # Extract grounding metadata / URL citations if available
        citations: list[str] = []
        if hasattr(response, "candidates") and response.candidates:
            cand = response.candidates[0]
            g_meta = getattr(cand, "grounding_metadata", None)
            if g_meta and hasattr(g_meta, "grounding_chunks"):
                for chunk in g_meta.grounding_chunks or []:
                    web = getattr(chunk, "web", None)
                    if web and getattr(web, "uri", None):
                        title = getattr(web, "title", "Source")
                        citations.append(f"- [{title}]({web.uri})")

        out_parts = [f"### 🔍 Google Search Results for: '{query.strip()}'", "", text]
        if citations:
            out_parts.extend(["", "#### 🔗 Sources & Citations", "\n".join(citations)])

        logger.info(
            "Google search executed successfully (%d/%d) for query '%s'",
            current_count + 1,
            MAX_SEARCH_CALLS_PER_SESSION,
            query[:30],
        )
        return "\n".join(out_parts)

    except Exception as exc:
        logger.warning("Google Search tool execution failed: %s", exc)
        return f"Error executing Google Search: {exc}"
