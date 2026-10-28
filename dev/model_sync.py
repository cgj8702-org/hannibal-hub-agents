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
from pathlib import Path
from typing import Any


def _load_env_if_present() -> None:
    """Auto-load local environment configuration (.envrc or .env) when run as CLI."""
    envrc_path = Path(__file__).resolve().parents[1] / ".envrc"
    if envrc_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(envrc_path, override=False)
        except ImportError:
            pass


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


class _SuppressApiKeyWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Both GOOGLE_API_KEY and GEMINI_API_KEY are set" not in record.getMessage()


logging.getLogger("google_genai._api_client").addFilter(_SuppressApiKeyWarningFilter())


TARGET_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src",
    "webhook_agent",
    "assets",
    "registries",
    "gemini_models.json",
)
MIRROR_REGISTRY = os.path.join(
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
    "transcribe",
    "speech",
    "music",
    "antigravity",
    "deep-research",
    "banana",
    "omni",
    "argon",
]


def _get_exclusion_keywords() -> list[str]:
    raw = os.environ.get("MODEL_EXCLUSION_KEYWORDS", "")
    if not raw:
        return DEFAULT_EXCLUSION_KEYWORDS
    custom = [k.strip().lower() for k in raw.split(",") if k.strip()]
    return list(set(DEFAULT_EXCLUSION_KEYWORDS + custom))


def is_eligible_registry_model(model_entry: dict[str, Any]) -> bool:
    """Evaluates whether a model entry meets criteria for synthesis into the target model registry.

    Checks:
    - Must support text generation actions (generateContent or bidiGenerateContent) if supported_actions is present.
    - Must not match any exclusion keywords in its name, display_name, or description.
    """
    actions = model_entry.get("supported_actions")
    if actions is not None:
        is_text_gen = any(a in actions for a in ("generateContent", "bidiGenerateContent"))
        if not is_text_gen:
            return False

    excluded_keywords = _get_exclusion_keywords()
    name = str(model_entry.get("name") or "")
    display_name = str(model_entry.get("display_name") or "")
    description = str(model_entry.get("description") or "")
    search_corpus = f"{name} {display_name} {description}".lower()

    return not any(kw in search_corpus for kw in excluded_keywords)


async def fetch_model_registry() -> bool:
    """Fetches the latest model list from the Google Generative AI API across free and paid project keys.

    Returns True on success, False on failure.
    """
    _load_env_if_present()
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
        LOGGER.error("No valid WEBHOOK_FREE_KEY or WEBHOOK_PAID_KEY found in environment.")
        return False

    models_by_name: dict[str, dict[str, Any]] = {}

    existing_limits: dict[str, Any] = {}
    if os.path.exists(TARGET_REGISTRY):
        try:
            with open(TARGET_REGISTRY, encoding="utf-8") as f:
                existing_data = json.load(f)
                for m in existing_data.get("models", []):
                    if isinstance(m, dict) and "name" in m and "rate_limits" in m:
                        existing_limits[m["name"]] = m["rate_limits"]
        except Exception as e:
            LOGGER.warning(f"Failed to read existing rate limits: {e}")

    for tier, api_key in keys_to_poll.items():
        LOGGER.info(f"Polling Gemini models using WEBHOOK_{tier.upper()}_KEY...")
        try:
            client = genai.Client(api_key=api_key)
            for model in client.models.list():
                name = getattr(model, "name", None)
                if not name:
                    continue
                actions = getattr(model, "supported_actions", None) or []
                if name not in models_by_name:
                    entry = {
                        "name": name,
                        "version": getattr(model, "version", None),
                        "display_name": getattr(model, "display_name", None),
                        "description": getattr(model, "description", None),
                        "input_token_limit": getattr(model, "input_token_limit", None),
                        "output_token_limit": getattr(model, "output_token_limit", None),
                        "supported_actions": actions,
                        "accessible_tiers": [],
                    }
                    if name in existing_limits:
                        entry["rate_limits"] = existing_limits[name]
                    models_by_name[name] = entry
                if tier not in models_by_name[name]["accessible_tiers"]:
                    models_by_name[name]["accessible_tiers"].append(tier)
        except Exception as e:
            LOGGER.error(f"Error listing models for tier '{tier}': {e}")

    try:
        from dev.rate_limit_parser import sort_models_by_tpm
    except ImportError:
        try:
            from rate_limit_parser import sort_models_by_tpm  # type: ignore[import-not-found, no-redef]
        except ImportError:

            def sort_models_by_tpm(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
                def _sort_key(m: dict[str, Any]) -> tuple[int, int, float, int, str]:
                    rl = m.get("rate_limits") or {}
                    paid = rl.get("paid") or {}
                    free = rl.get("free") or {}
                    paid_tpm = paid.get("tpm")
                    free_tpm = free.get("tpm")
                    paid_rpd = paid.get("rpd")
                    paid_rpm = paid.get("rpm")
                    pt = int(paid_tpm) if isinstance(paid_tpm, (int, float)) else 0
                    ft = int(free_tpm) if isinstance(free_tpm, (int, float)) else 0
                    prpd = float(paid_rpd) if isinstance(paid_rpd, (int, float)) else 0.0
                    prpm = int(paid_rpm) if isinstance(paid_rpm, (int, float)) else 0
                    name = str(m.get("name") or "")
                    return (-pt, -ft, -prpd, -prpm, name)

                return sorted(models, key=_sort_key)

    synthesized_models = [m for m in models_by_name.values() if is_eligible_registry_model(m)]
    models = sort_models_by_tpm(synthesized_models)

    # Ensure target directory exists
    target_dir = os.path.dirname(TARGET_REGISTRY)
    os.makedirs(target_dir, exist_ok=True)

    # Compare with existing file to avoid noisy writes
    if os.path.exists(TARGET_REGISTRY):
        try:

            def _read_file(path: str) -> str:
                with open(path, encoding="utf-8") as f:
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
        with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8") as tf:
            json.dump(payload, tf, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
            os.replace(tf.name, TARGET_REGISTRY)
        LOGGER.info(f"Synced {len(models)} models -> {TARGET_REGISTRY}")

        # Sync mirror registry if path differs
        if TARGET_REGISTRY != MIRROR_REGISTRY:
            mirror_dir = os.path.dirname(MIRROR_REGISTRY)
            os.makedirs(mirror_dir, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=mirror_dir, delete=False, encoding="utf-8"
            ) as tf:
                json.dump(payload, tf, indent=2)
                tf.flush()
                os.fsync(tf.fileno())
                os.replace(tf.name, MIRROR_REGISTRY)
            LOGGER.info(f"Synced registry mirror -> {MIRROR_REGISTRY}")
        return True
    except OSError as e:
        LOGGER.error(f"Failed to write registry: {e}")
        return False


async def run_pipeline():
    LOGGER.info("Starting agents model sync pipeline")
    # Step 1: Fetch Gemini models from API
    LOGGER.info("Step 1: Synchronizing Gemini models from API...")
    await fetch_model_registry()

    # Step 2: Auto-discover and parse Rate Limit PDFs
    LOGGER.info("Step 2: Ingesting vendor Rate Limit PDFs...")
    try:
        try:
            from dev.rate_limit_parser import main as parse_rate_limits
        except ImportError:
            from rate_limit_parser import main as parse_rate_limits  # type: ignore[no-redef]

        parse_rate_limits([])
    except Exception as e:
        LOGGER.warning(f"Rate limit PDF auto-parse failed or skipped: {e}")

    LOGGER.info("Agents model sync pipeline complete.")


def main():
    import argparse

    _load_env_if_present()

    parser = argparse.ArgumentParser(description="Agents Model Synchronization Utility")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("fetch", help="Fetch and update local gemini_models.json")
    sub.add_parser("parse", help="Parse vendor rate limit PDFs into gemini_models.json")
    sub.add_parser("pipeline", help="Run sync pipeline (fetch + parse PDFs)")

    args = parser.parse_args()
    if args.cmd == "fetch":
        asyncio.run(fetch_model_registry())
    elif args.cmd == "parse":
        try:
            from dev.rate_limit_parser import main as parse_rate_limits
        except ImportError:
            from rate_limit_parser import main as parse_rate_limits  # type: ignore[no-redef]

        parse_rate_limits([])
    elif args.cmd == "pipeline":
        asyncio.run(run_pipeline())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
