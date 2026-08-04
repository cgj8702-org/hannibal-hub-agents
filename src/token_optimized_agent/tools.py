"""Tools for off-context artifact storage and data lookups."""

from google.adk.tools import ToolContext
from google.genai import types


async def save_large_data_artifact(
    filename: str,
    content: str,
    tool_context: ToolContext,
) -> dict[str, str]:
    """Save large text content as an artifact off-context to avoid prompt bloat.

    Args:
        filename: Name of the artifact file to save.
        content: String content of the document or dataset.
        tool_context: The ADK tool context.

    Returns:
        A dictionary with status and version info.
    """
    part = types.Part(
        inline_data=types.Blob(mime_type="text/plain", data=content.encode("utf-8"))
    )
    version = await tool_context.save_artifact(filename, part)
    return {
        "status": "success",
        "message": (
            f"Saved {filename} off-context (version {version}). "
            "Refer to this artifact rather than embedding raw text in context."
        ),
    }


def lookup_config(key: str) -> dict[str, str]:
    """Look up system configuration settings by key.

    Args:
        key: The configuration key to look up.

    Returns:
        A dictionary containing the configuration key and value.
    """
    sample_configs = {
        "environment": "production",
        "max_retries": "3",
        "timeout": "30s",
    }
    value = sample_configs.get(key, "not_found")
    return {"key": key, "value": value}
