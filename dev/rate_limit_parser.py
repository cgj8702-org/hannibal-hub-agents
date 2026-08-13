"""Rate Limit PDF Parser & Dual-Tier Generator for Hannibal Hub Agents.

Parses vendor PDFs into 'free' or 'paid' tier reports and outputs dual-tier JSON schemas.
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
    pdfplumber = None

try:
    import pypdf
except ImportError:
    pypdf = None

logger = logging.getLogger("rate_limit_parser")

MODEL_MAPPING = {
    "gemini 3.6 flash": "models/gemini-3.6-flash",
    "gemini 3.5 flash lite": "models/gemini-3.5-flash-lite",
    "gemini 3.5 flash": "models/gemini-3.5-flash",
    "gemini 3.7 flash": "models/gemini-3.7-flash",
    "gemini 3.1 flash lite": "models/gemini-3.1-flash-lite",
    "gemini 3.1 pro": "models/gemini-3.1-pro",
    "gemini 3 flash": "models/gemini-3-flash",
    "gemini 2.5 flash": "models/gemini-2.5-flash",
    "gemini 2.5 flash lite": "models/gemini-2.5-flash-lite",
    "gemini 2.5 pro": "models/gemini-2.5-pro",
    "gemini 2 flash": "models/gemini-2.0-flash",
    "gemini 2.0 flash": "models/gemini-2.0-flash",
    "gemini 2 flash lite": "models/gemini-2.0-flash-lite",
    "gemini 2.0 flash lite": "models/gemini-2.0-flash-lite",
    "gemma 4 31b": "models/gemma-4-31b-it",
    "gemma 4 26b": "models/gemma-4-26b-a4b-it",
}


def parse_pdf_text(pdf_path: Path) -> str:
    """Extract raw text from PDF file using pdfplumber or pypdf."""
    text = ""
    if pdfplumber:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as e:  # noqa: BLE001
            logger.warning("pdfplumber failed for %s: %s", pdf_path, e)

    if not text and pypdf:
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:  # noqa: BLE001
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
    free_proj = os.environ.get(
        "WEBHOOK_FREE_PROJECT", "gen-lang-client-0615466973"
    ).lower()
    paid_proj = os.environ.get("WEBHOOK_PAID_PROJECT", "cgj8702-webhook-agent").lower()
    chatbot_paid = os.environ.get(
        "CHATBOT_PAID_PROJECT", "chatbot-project-hannibal"
    ).lower()
    chatbot_free = os.environ.get(
        "CHATBOT_FREE_PROJECT", "gen-lang-client-0035989819"
    ).lower()

    text_lower = raw_text.lower()

    if (free_proj and free_proj in text_lower) or (
        paid_proj and paid_proj in text_lower
    ):
        return True

    if (chatbot_paid and chatbot_paid in text_lower) or (
        chatbot_free and chatbot_free in text_lower
    ):
        return False

    project_name = extract_project_name(raw_text).lower()
    if "chatbot" in project_name and "webhook" not in project_name:
        return False
    return True


def classify_pdf(raw_text: str, filename: str = "") -> str:
    """Determine if PDF text/filename represents a 'paid' tier (Tier 1+) or 'free' tier document using envrc projects and keywords."""
    free_proj = os.environ.get(
        "WEBHOOK_FREE_PROJECT", "gen-lang-client-0615466973"
    ).lower()
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


def extract_tier_entry(entry_data: dict[str, Any] | None) -> dict[str, float | int]:
    """Extract rpm, tpm, and rpd from raw parsed tier entry dictionary."""
    if not entry_data:
        return {"rpm": 15, "tpm": 0, "rpd": 0.0}
    return {
        "rpm": int(parse_scaled_value(entry_data.get("RPM", "0"))),
        "tpm": int(parse_scaled_value(entry_data.get("TPM", "0"))),
        "rpd": parse_scaled_value(entry_data.get("RPD", "0")),
    }


def parse_pdf_file(pdf_path: Path) -> tuple[str, dict[str, Any]]:
    """Parse text and extract model rate limit records from a single PDF file."""
    full_text = parse_pdf_text(pdf_path)
    tier = classify_pdf(full_text, filename=pdf_path.name)
    records: dict[str, Any] = {}

    if not is_webhook_project_pdf(full_text):
        logger.info("Skipping non-webhook PDF document: %s", pdf_path.name)
        return tier, {}

    pattern = re.compile(
        r"(Gemini[\w\.\s\-]+?|Gemma[\w\.\s\-]+?)\s+(Text-out models)\s+[\d\.\,KMB]+\s*/\s*([\d\.\,KMB]+|Unlimited|-)\s+[\d\.\,KMB]+\s*/\s*([\d\.\,KMB]+|Unlimited|-)\s+[\d\.\,KMB]+\s*/\s*([\d\.\,KMB]+|Unlimited|-)",
        re.IGNORECASE,
    )

    for match in pattern.finditer(full_text):
        raw_name, cat, rpm_lim, tpm_lim, rpd_lim = match.groups()
        name_clean = raw_name.strip().lower()
        model_id = MODEL_MAPPING.get(
            name_clean, f"models/{name_clean.replace(' ', '-')}"
        )
        records[model_id] = extract_tier_entry(
            {
                "RPM": rpm_lim,
                "TPM": tpm_lim,
                "RPD": rpd_lim,
            }
        )

    return tier, records


def main(args_list: list[str] | None = None) -> None:
    """CLI entrypoint for processing rate limit PDFs."""
    parser = argparse.ArgumentParser(description="Parse Webhook Rate Limit PDFs")
    parser.add_argument("pdf_files", nargs="*", help="Optional explicit PDF file paths")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args(args_list)

    root_dir = Path(__file__).resolve().parents[1]
    output_path = (
        Path(args.output)
        if args.output
        else root_dir / "src" / "assets" / "registries" / "rate_limits.json"
    )

    candidates: list[Path] = []
    if args.pdf_files:
        candidates = [Path(p) for p in args.pdf_files if Path(p).exists()]
    else:
        # Auto-discover PDFs recursively anywhere in project tree
        ignore_dirs = {".git", ".venv", "node_modules", "_archive", "adk-samples"}
        for p in root_dir.rglob("*.pdf"):
            if any(part in ignore_dirs or part.startswith(".") for part in p.parts):
                continue
            candidates.append(p)

    dual_tier_registry: dict[str, dict[str, Any]] = {}
    free_found = False
    paid_found = False

    for pdf_path in candidates:
        tier, records = parse_pdf_file(pdf_path)
        if tier == "free":
            free_found = True
        elif tier == "paid":
            paid_found = True
        for model, entry in records.items():
            dual_tier_registry.setdefault(model, {})[tier] = entry

    if dual_tier_registry:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(dual_tier_registry, indent=4), encoding="utf-8"
        )
        logger.info(
            "Saved rate limits registry to %s (Free: %s, Paid: %s)",
            output_path,
            free_found,
            paid_found,
        )
    else:
        logger.warning("No webhook PDF rate limit data extracted.")


if __name__ == "__main__":
    main()
