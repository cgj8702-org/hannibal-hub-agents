"""
Agent repo Model Sync: Gemini Registry Maintenance (ported from hannibal-hub/dev/model_sync.py)
Fetches the Gemini model registry from the Google Generative AI API and writes a
local JSON registry at `src/assets/registries/gemini_models.json`.

This is a lightweight, dependency-minimal port suitable for the agents repo.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

try:
    from google import genai
except ImportError:
    genai: Any = None  # type: ignore[no-redef]

LOGGER = logging.getLogger("agents.model_sync")
LOGGER.setLevel(logging.INFO)
ch = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
ch.setFormatter(formatter)
LOGGER.addHandler(ch)


TARGET_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "assets",
    "registries",
    "gemini_models.json",
)


DEFAULT_EXCLUSION_KEYWORDS = [
    "image",
    "imagen",
    "lyria",
    "veo",
    "live",
    "computer",
    "robot",
    "tts",
    "tools",
    "embedding",
    "audio",
]


def _get_exclusion_keywords() -> list[str]:
    raw = os.environ.get("MODEL_EXCLUSION_KEYWORDS", "")
    if not raw:
        return DEFAULT_EXCLUSION_KEYWORDS
    custom = [k.strip().lower() for k in raw.split(",") if k.strip()]
    return list(set(DEFAULT_EXCLUSION_KEYWORDS + custom))


async def fetch_model_registry() -> bool:
    """Fetches the latest model list from the Google Generative AI API across free and paid project keys.

    Returns True on success, False on failure.
    """
    if not genai:
        LOGGER.error("Google Generative AI library (genai) not installed.")
        return False

    keys_to_poll: dict[str, str] = {}
    free_key = (os.getenv("WEBHOOK_FREE_KEY") or "").strip()
    paid_key = (os.getenv("WEBHOOK_PAID_KEY") or "").strip()

    if free_key and free_key.lower() not in ("dummy", "dummy-key-for-dev", "none"):
        keys_to_poll["free"] = free_key
    if paid_key and paid_key.lower() not in ("dummy", "dummy-key-for-dev", "none"):
        keys_to_poll["paid"] = paid_key

    if not keys_to_poll:
        LOGGER.error(
            "No valid WEBHOOK_FREE_KEY or WEBHOOK_PAID_KEY found in environment."
        )
        return False

    models_by_name: dict[str, dict[str, Any]] = {}
    excluded_keywords = _get_exclusion_keywords()

    for tier, api_key in keys_to_poll.items():
        LOGGER.info(f"Polling Gemini models using WEBHOOK_{tier.upper()}_KEY...")
        try:
            client = genai.Client(api_key=api_key)
            for model in client.models.list():
                actions = getattr(model, "supported_actions", None) or []
                is_text_gen = any(
                    a in actions for a in ("generateContent", "bidiGenerateContent")
                )

                search_corpus = f"{getattr(model, 'name', '')} {getattr(model, 'display_name', '')} {getattr(model, 'description', '')}".lower()
                is_excluded = any(kw in search_corpus for kw in excluded_keywords)

                if is_text_gen and not is_excluded:
                    name = getattr(model, "name", None)
                    if not name:
                        continue
                    if name not in models_by_name:
                        models_by_name[name] = {
                            "name": name,
                            "version": getattr(model, "version", None),
                            "display_name": getattr(model, "display_name", None),
                            "description": getattr(model, "description", None),
                            "input_token_limit": getattr(
                                model, "input_token_limit", None
                            ),
                            "output_token_limit": getattr(
                                model, "output_token_limit", None
                            ),
                            "supported_actions": actions,
                            "accessible_tiers": [],
                        }
                    models_by_name[name]["accessible_tiers"].append(tier)
        except Exception as e:  # noqa: BLE001
            LOGGER.error(f"Error listing models for tier '{tier}': {e}")

    models = sorted(list(models_by_name.values()), key=lambda m: m["name"])

    # Ensure target directory exists
    target_dir = os.path.dirname(TARGET_REGISTRY)
    os.makedirs(target_dir, exist_ok=True)

    # Compare with existing file to avoid noisy writes
    if os.path.exists(TARGET_REGISTRY):
        try:

            def _read_file(path: str) -> str:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()

            contents = await asyncio.to_thread(_read_file, TARGET_REGISTRY)
            existing = json.loads(contents)
            if existing.get("models") == models:
                LOGGER.info("Model registry already up-to-date. Skipping write.")
                return True
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            LOGGER.warning(f"Failed to read existing registry; will overwrite: {e}")

    payload = {
        "models": models,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=target_dir, delete=False, encoding="utf-8"
        ) as tf:
            json.dump(payload, tf, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
            os.replace(tf.name, TARGET_REGISTRY)
        LOGGER.info(f"Synced {len(models)} models -> {TARGET_REGISTRY}")
        return True
    except OSError as e:
        LOGGER.error(f"Failed to write registry: {e}")
        return False


async def run_pipeline():
    LOGGER.info("Starting agents model sync pipeline")
    # Future steps (changelog scraping, docs rebuild) omitted for simplicity
    await fetch_model_registry()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Agents Model Synchronization Utility")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("fetch", help="Fetch and update local gemini_models.json")
    sub.add_parser("pipeline", help="Run sync pipeline (fetch) for now")

    args = parser.parse_args()
    if args.cmd == "fetch":
        asyncio.run(fetch_model_registry())
    elif args.cmd == "pipeline":
        asyncio.run(run_pipeline())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
