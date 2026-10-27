"""Rate Limit PDF Parser & Dual-Tier Generator for Hannibal Hub Agents.

Parses vendor PDFs into 'free' or 'paid' tier reports, saves individual tier JSON files
in data/, combines them into a unified model registry within src/**, sorts models by TPM,
and injects zero synthetic fallbacks for missing models.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

try:
    import pdfplumber
except ImportError:
    pdfplumber: Any = None  # type: ignore[no-redef]

try:
    import pypdf
except ImportError:
    pypdf: Any = None  # type: ignore[no-redef]

logger = logging.getLogger("rate_limit_parser")

MODEL_MAPPING = {
    "gemini 3.8 flash": "models/gemini-3.8-flash",
    "gemini 3.7 flash": "models/gemini-3.7-flash",
    "gemini 3.6 flash": "models/gemini-3.6-flash",
    "gemini 3.5 flash": "models/gemini-3.5-flash",
    "gemini 3.5 flash lite": "models/gemini-3.5-flash-lite",
    "gemini 3.1 flash lite": "models/gemini-3.1-flash-lite",
    "gemini 3.1 pro": "models/gemini-3.1-pro-preview",
    "gemini 3 flash": "models/gemini-3-flash-preview",
    "gemini 2.5 flash": "models/gemini-2.5-flash",
    "gemini 2.5 flash lite": "models/gemini-2.5-flash-lite",
    "gemini 2.5 pro": "models/gemini-2.5-pro",
    "gemini 2 flash": "models/gemini-2.0-flash",
    "gemini 2.0 flash": "models/gemini-2.0-flash",
    "gemini 2 flash lite": "models/gemini-2.0-flash-lite",
    "gemini 2.0 flash lite": "models/gemini-2.0-flash-lite",
    "gemini 4 argon": "models/gemini-4-argon",
    "gemma 4 31b": "models/gemma-4-31b-it",
    "gemma 4 26b": "models/gemma-4-26b-a4b-it",
}

KNOWN_MODEL_METADATA: dict[str, dict[str, Any]] = {
    "models/gemini-2.0-flash": {
        "version": "001",
        "display_name": "Gemini 2.0 Flash",
        "description": "Gemini 2.0 Flash offering high performance and speed with 4M TPM paid limit.",
        "input_token_limit": 1048576,
        "output_token_limit": 8192,
        "supported_actions": [
            "generateContent",
            "countTokens",
            "createCachedContent",
            "batchGenerateContent",
        ],
        "accessible_tiers": ["paid"],
    },
    "models/gemini-2.0-flash-lite": {
        "version": "001",
        "display_name": "Gemini 2.0 Flash-Lite",
        "description": "Gemini 2.0 Flash-Lite optimized for cost efficiency and low latency with 4M TPM paid limit.",
        "input_token_limit": 1048576,
        "output_token_limit": 8192,
        "supported_actions": [
            "generateContent",
            "countTokens",
            "createCachedContent",
            "batchGenerateContent",
        ],
        "accessible_tiers": ["paid"],
    },
    "models/gemini-4-argon": {
        "version": "001",
        "display_name": "Gemini 4 Argon",
        "description": "Advanced reasoning model with 2M TPM limit.",
        "input_token_limit": 1048576,
        "output_token_limit": 65536,
        "supported_actions": [
            "generateContent",
            "countTokens",
            "createCachedContent",
            "batchGenerateContent",
        ],
        "accessible_tiers": ["paid"],
    },
}


def parse_pdf_text(pdf_path: Path) -> str:
    """Extract raw text from PDF file using pdfplumber or pypdf."""
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    text = ""
    if pdfplumber:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as e:
            logger.warning("pdfplumber failed for %s: %s", pdf_path, e)

    if not text and pypdf:
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            logger.error("pypdf failed for %s: %s", pdf_path, e)

    return text


def extract_project_name(raw_text: str) -> str:
    """Extract project name from Google AI Studio PDF text."""
    match = re.search(
        r"Project\s+([A-Za-z0-9\s\-_]+?)(?:\s*|\s+Time Range|\n|$)",
        raw_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return ""


def is_webhook_project_pdf(raw_text: str) -> bool:
    """Verify if PDF header/footer matches the Webhook Agent project IDs from envrc or keywords."""
    free_proj = os.environ.get("WEBHOOK_FREE_PROJECT", "gen-lang-client-0615466973").lower()
    paid_proj = os.environ.get("WEBHOOK_PAID_PROJECT", "cgj8702-webhook-agent").lower()
    chatbot_paid = os.environ.get("CHATBOT_PAID_PROJECT", "chatbot-project-hannibal").lower()
    chatbot_free = os.environ.get("CHATBOT_FREE_PROJECT", "gen-lang-client-0035989819").lower()

    text_lower = raw_text.lower()

    if (free_proj and free_proj in text_lower) or (paid_proj and paid_proj in text_lower):
        return True

    if (chatbot_paid and chatbot_paid in text_lower) or (
        chatbot_free and chatbot_free in text_lower
    ):
        return False

    project_name = extract_project_name(raw_text).lower()
    return not ("chatbot" in project_name and "webhook" not in project_name)


def classify_pdf(raw_text: str, filename: str = "") -> str:
    """Determine if PDF text/filename represents a 'paid' tier (Tier 1+) or 'free' tier document."""
    free_proj = os.environ.get("WEBHOOK_FREE_PROJECT", "gen-lang-client-0615466973").lower()
    paid_proj = os.environ.get("WEBHOOK_PAID_PROJECT", "cgj8702-webhook-agent").lower()

    combined = (raw_text + " " + filename).lower()

    if paid_proj and paid_proj in combined:
        return "paid"
    if free_proj and free_proj in combined:
        return "free"

    if "free tier" in combined or "free" in filename.lower() or "tier 0" in combined:
        return "free"
    if (
        "paid" in filename.lower()
        or "tier 1" in combined
        or "tier 2" in combined
        or "pay-as-you-go" in combined
    ):
        return "paid"
    return "free"


def parse_scaled_value(val: Any) -> float:
    """Parse numerical values with scale factor (K, M, B) or Unlimited."""
    if not val or val == "-":
        return 0.0
    val_str = str(val).strip().upper()
    if val_str == "UNLIMITED":
        return 1000000.0
    multiplier = 1.0
    if val_str.endswith("K"):
        multiplier = 1000.0
        val_str = val_str[:-1]
    elif val_str.endswith("M"):
        multiplier = 1000000.0
        val_str = val_str[:-1]
    elif val_str.endswith("B"):
        multiplier = 1000000000.0
        val_str = val_str[:-1]
    try:
        return float(val_str.replace(",", "")) * multiplier
    except ValueError:
        return 0.0


def extract_tier_entry(entry_data: dict[str, Any] | None) -> dict[str, float | int] | None:
    """Extract numeric rpm, tpm, and rpd from raw parsed tier entry dictionary.

    Returns None if entry_data is missing, strictly avoiding synthetic fallback injection.
    """
    if not entry_data:
        return None
    return {
        "rpm": int(parse_scaled_value(entry_data.get("RPM", "0"))),
        "tpm": int(parse_scaled_value(entry_data.get("TPM", "0"))),
        "rpd": parse_scaled_value(entry_data.get("RPD", "0")),
    }


def extract_rate_limit_records(full_text: str) -> dict[str, dict[str, str]]:
    """Parse raw text into structured rate limit dictionaries by model display name."""
    pattern = re.compile(
        r"(?:Spend\s+)?(Gemini[\w\.\s\-]+?|Gemma[\w\.\s\-]+?)\s+(Text-out models|Other models|Multi-modal generative models)\s+[\d\.\,KMB]+\s*/\s*([\d\.\,KMB]+|Unlimited|-)\s+[\d\.\,KMB]+\s*/\s*([\d\.\,KMB]+|Unlimited|-)\s+[\d\.\,KMB]+\s*/\s*([\d\.\,KMB]+|Unlimited|-)",
        re.IGNORECASE,
    )
    records: dict[str, dict[str, str]] = {}
    for match in pattern.finditer(full_text):
        raw_name, category, rpm_lim, tpm_lim, rpd_lim = match.groups()
        name_clean = re.sub(
            r"^(?:spend\s+|changelog[^\w\s]*\s*|\n)+", "", raw_name.strip(), flags=re.IGNORECASE
        ).strip()
        if category == "Multi-modal generative models" and "TTS" in name_clean:
            continue
        records[name_clean] = {
            "Category": category,
            "RPM": rpm_lim,
            "TPM": tpm_lim,
            "RPD": rpd_lim,
        }
    return records


def parse_pdf_file(pdf_path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """Parse text and extract model rate limit records from a single PDF file."""
    full_text = parse_pdf_text(pdf_path)
    tier = classify_pdf(full_text, filename=pdf_path.name)

    if not is_webhook_project_pdf(full_text):
        logger.info("Skipping non-webhook PDF document: %s", pdf_path.name)
        return tier, {}

    records = extract_rate_limit_records(full_text)
    return tier, records


def sort_models_by_tpm(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort models descending by TPM (paid tier first, then free tier), then RPD, then RPM, then name."""

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


def _tier_tpm_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[float, float, float, str]:
    name, limits = item
    tpm = parse_scaled_value(limits.get("TPM", "-"))
    rpd = parse_scaled_value(limits.get("RPD", "-"))
    rpm = parse_scaled_value(limits.get("RPM", "-"))
    return (-tpm, -rpd, -rpm, name)


def main(args_list: list[str] | None = None) -> None:
    """CLI entrypoint for processing rate limit PDFs."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s"
    )
    parser = argparse.ArgumentParser(description="Parse Webhook Rate Limit PDFs")
    parser.add_argument("pdf_files", nargs="*", help="Optional explicit PDF file paths")
    parser.add_argument("--output", help="Optional explicit output JSON path")
    args = parser.parse_args(args_list)

    root_dir = Path(__file__).resolve().parents[1]
    src_registry_path = (
        root_dir / "src" / "webhook_agent" / "assets" / "registries" / "gemini_models.json"
    )
    root_registry_path = root_dir / "assets" / "registries" / "gemini_models.json"

    output_path = Path(args.output) if args.output else src_registry_path

    candidates: list[Path] = []
    if args.pdf_files:
        candidates = [Path(p) for p in args.pdf_files if Path(p).exists()]
    else:
        ignore_dirs = {".git", ".venv", "node_modules", "_archive", "adk-samples"}
        for p in root_dir.rglob("*.pdf"):
            if any(part in ignore_dirs or part.startswith(".") for part in p.parts):
                continue
            candidates.append(p)

    free_records: dict[str, dict[str, str]] = {}
    paid_records: dict[str, dict[str, str]] = {}

    logger.info("Found %d rate limit PDF candidate(s) to process", len(candidates))
    for pdf_path in candidates:
        tier, records = parse_pdf_file(pdf_path)
        logger.info("Parsed %d model quota(s) from %s (%s tier)", len(records), pdf_path.name, tier)
        if tier == "free":
            free_records.update(records)
        elif tier == "paid":
            paid_records.update(records)

    # 1. Feature 1: Writing 2 separate JSON files in data/
    sorted_free = dict(sorted(free_records.items(), key=_tier_tpm_sort_key))
    sorted_paid = dict(sorted(paid_records.items(), key=_tier_tpm_sort_key))

    data_dir = root_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if sorted_free:
        free_file = data_dir / "free_tier_rate_limits.json"
        free_file.write_text(json.dumps(sorted_free, indent=2), encoding="utf-8")
        logger.info("Saved %d free tier rate limits to %s", len(sorted_free), free_file)

    if sorted_paid:
        paid_file = data_dir / "paid_tier_rate_limits.json"
        paid_file.write_text(json.dumps(sorted_paid, indent=2), encoding="utf-8")
        logger.info("Saved %d paid tier rate limits to %s", len(sorted_paid), paid_file)

    # 2. Build dual-tier system registry (NO fallback injection for missing models)
    dual_tier_registry: dict[str, dict[str, Any]] = {}
    all_names = sorted(set(sorted_free.keys()).union(sorted_paid.keys()))

    for name in all_names:
        clean_lower = name.strip().lower()
        model_id = MODEL_MAPPING.get(clean_lower, f"models/{clean_lower.replace(' ', '-')}")
        free_entry = extract_tier_entry(sorted_free.get(name))
        paid_entry = extract_tier_entry(sorted_paid.get(name))

        tier_entry: dict[str, Any] = {}
        if free_entry is not None:
            tier_entry["free"] = free_entry
        if paid_entry is not None:
            tier_entry["paid"] = paid_entry

        if tier_entry:
            dual_tier_registry[model_id] = tier_entry

    if not dual_tier_registry:
        logger.warning("No webhook PDF rate limit data extracted.")
        return

    # 3. Feature 2: Combining them into 1 unified JSON within src/**
    base_file = src_registry_path if src_registry_path.exists() else root_registry_path
    models_json: dict[str, Any] = {"models": []}
    if base_file.exists():
        try:
            models_json = json.loads(base_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load existing registry %s: %s", base_file, e)

    models_list = models_json.get("models", [])
    existing_model_ids = {m.get("name") for m in models_list if isinstance(m, dict)}

    # Update rate limits for existing models without injecting fallbacks
    for m in models_list:
        mid = m.get("name")
        if mid in dual_tier_registry:
            m["rate_limits"] = dual_tier_registry[mid]
            # Update accessible tiers based on parsed non-zero quotas
            accessible = list(m.get("accessible_tiers", []))
            for tier in ("free", "paid"):
                t_data = dual_tier_registry[mid].get(tier, {})
                rpm = t_data.get("rpm", 0)
                rpd = t_data.get("rpd", 0.0)
                if (rpm > 0 and rpd > 0) and tier not in accessible:
                    accessible.append(tier)
                elif rpm == 0 and rpd == 0 and tier in accessible:
                    accessible.remove(tier)
            m["accessible_tiers"] = accessible

    # Add any known models present in the parsed PDFs but not yet in the registry
    for mid, meta in KNOWN_MODEL_METADATA.items():
        if mid in dual_tier_registry and mid not in existing_model_ids:
            new_entry = {
                "name": mid,
                "version": meta["version"],
                "display_name": meta["display_name"],
                "description": meta["description"],
                "input_token_limit": meta["input_token_limit"],
                "output_token_limit": meta["output_token_limit"],
                "supported_actions": meta["supported_actions"],
                "accessible_tiers": list(meta["accessible_tiers"]),
                "rate_limits": dual_tier_registry[mid],
            }
            models_list.append(new_entry)
            existing_model_ids.add(mid)

    # Filter models list during synthesis using eligibility rules
    try:
        from dev.model_sync import is_eligible_registry_model
    except ImportError:
        try:
            from model_sync import is_eligible_registry_model  # type: ignore[import-not-found, no-redef]
        except ImportError:

            def is_eligible_registry_model(model_entry: dict[str, Any]) -> bool:
                return True

    eligible_models = [m for m in models_list if is_eligible_registry_model(m)]

    # 4. Feature 3: Sorting the models in the final src file by TPM
    models_json["models"] = sort_models_by_tpm(eligible_models)

    # Write to primary target (src/**)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(models_json, indent=2), encoding="utf-8")
    logger.info("Saved unified model registry to %s", output_path)

    # If output_path is src_registry_path, also keep root_registry_path in sync
    if output_path == src_registry_path and root_registry_path != src_registry_path:
        root_registry_path.parent.mkdir(parents=True, exist_ok=True)
        root_registry_path.write_text(json.dumps(models_json, indent=2), encoding="utf-8")
        logger.info("Synced registry mirror to %s", root_registry_path)


if __name__ == "__main__":
    main()
