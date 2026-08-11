"""Rate Limit PDF Parser & Dual-Tier Generator.

Classifies vendor PDFs into 'free' or 'paid' tier reports and outputs dual-tier JSON schemas.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

import pypdf

logger = logging.getLogger("rate_limit_parser")


def classify_pdf(raw_text: str) -> str:
    """Determine if PDF text represents a 'paid' tier (Tier 1+) or 'free' tier rate limit document."""
    text_lower = raw_text.lower()
    if "free tier" in text_lower or "tier 0" in text_lower:
        return "free"
    if (
        "tier 1" in text_lower
        or "pay-as-you-go" in text_lower
        or "tier 2" in text_lower
        or "chatbot-project-hannibal" in text_lower
    ):
        return "paid"
    return "free"


def parse_scaled_value(val: Any) -> float:
    """Parse string numerical values with optional scale suffixes (K, M, B)."""
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
    reader = pypdf.PdfReader(str(pdf_path))
    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    tier = classify_pdf(full_text)
    records: dict[str, Any] = {}

    # Basic regex pattern to capture model entries
    pattern = re.compile(
        r"(models/[\w\.\-]+)\s+(\d+[\w\,]*|-)\s+(\d+[\w\,]*|-)\s+(\d+[\w\.\,]*|-)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(full_text):
        model, rpm, tpm, rpd = match.groups()
        records[model] = extract_tier_entry({"RPM": rpm, "TPM": tpm, "RPD": rpd})

    return tier, records


def main() -> None:
    """CLI entrypoint for processing rate limit PDFs."""
    parser = argparse.ArgumentParser(description="Parse Rate Limit PDFs")
    parser.add_argument("pdf_files", nargs="+", help="Paths to PDF files")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    dual_tier_registry: dict[str, dict[str, Any]] = {}

    for path_str in args.pdf_files:
        pdf_path = Path(path_str)
        if not pdf_path.exists():
            logger.warning("File not found: %s", pdf_path)
            continue
        tier, records = parse_pdf_file(pdf_path)
        for model, entry in records.items():
            dual_tier_registry.setdefault(model, {})[tier] = entry

    output_json = json.dumps(dual_tier_registry, indent=4)
    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")
        logger.info("Saved registry to %s", args.output)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
