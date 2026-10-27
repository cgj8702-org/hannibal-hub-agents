"""Unit tests for rate limit parser, TPM sorting, and dual-tier registries."""

import json
from pathlib import Path
from typing import Any

import pytest

from dev.model_sync import is_eligible_registry_model
from dev.rate_limit_parser import (
    extract_rate_limit_records,
    extract_tier_entry,
    parse_scaled_value,
    sort_models_by_tpm,
)
from dev.rate_limit_parser import (
    main as run_parser,
)
from webhook_agent.models.rate_limiter import _resolve_registry_path

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_extract_tier_entry_strict_no_fallbacks() -> None:
    """Verify extract_tier_entry returns None for missing/empty input without synthetic fallbacks."""
    assert extract_tier_entry(None) is None
    assert extract_tier_entry({}) is None

    valid_entry = {"RPM": "4K", "TPM": "4M", "RPD": "Unlimited"}
    result = extract_tier_entry(valid_entry)
    assert result == {"rpm": 4000, "tpm": 4000000, "rpd": 1000000.0}

    zero_entry = {"RPM": "0", "TPM": "0", "RPD": "0"}
    assert extract_tier_entry(zero_entry) == {"rpm": 0, "tpm": 0, "rpd": 0.0}


def test_parse_scaled_value() -> None:
    """Verify metric scale parsing for K, M, B, Unlimited, and dashes."""
    assert parse_scaled_value("-") == 0.0
    assert parse_scaled_value(None) == 0.0
    assert parse_scaled_value("15") == 15.0
    assert parse_scaled_value("250K") == 250000.0
    assert parse_scaled_value("4M") == 4000000.0
    assert parse_scaled_value("1B") == 1000000000.0
    assert parse_scaled_value("Unlimited") == 1000000.0


def test_sort_models_by_tpm() -> None:
    """Verify sort_models_by_tpm prioritizes 4M TPM models, then 2M, 1M, 16K, and unrated."""
    models: list[dict[str, Any]] = [
        {
            "name": "models/gemma-4-31b-it",
            "rate_limits": {
                "free": {"rpm": 30, "tpm": 16000, "rpd": 14400.0},
                "paid": {"rpm": 30, "tpm": 16000, "rpd": 14400.0},
            },
        },
        {
            "name": "models/gemini-2.0-flash",
            "rate_limits": {
                "free": {"rpm": 0, "tpm": 0, "rpd": 0.0},
                "paid": {"rpm": 2000, "tpm": 4000000, "rpd": 1000000.0},
            },
        },
        {
            "name": "models/gemini-3-flash-preview",
            "rate_limits": {
                "free": {"rpm": 5, "tpm": 250000, "rpd": 20.0},
                "paid": {"rpm": 1000, "tpm": 2000000, "rpd": 10000.0},
            },
        },
        {
            "name": "models/gemini-2.5-flash-lite",
            "rate_limits": {
                "free": {"rpm": 10, "tpm": 250000, "rpd": 20.0},
                "paid": {"rpm": 4000, "tpm": 4000000, "rpd": 1000000.0},
            },
        },
        {
            "name": "models/gemini-unrated-model",
            "rate_limits": None,
        },
    ]

    sorted_list = sort_models_by_tpm(models)
    names = [m["name"] for m in sorted_list]

    # 4M TPM models must precede 2M, 16K, and unrated models
    assert names[0] == "models/gemini-2.5-flash-lite"  # 4M paid TPM, 250K free TPM
    assert names[1] == "models/gemini-2.0-flash"  # 4M paid TPM, 0 free TPM
    assert names[2] == "models/gemini-3-flash-preview"  # 2M paid TPM
    assert names[3] == "models/gemma-4-31b-it"  # 16K paid TPM
    assert names[4] == "models/gemini-unrated-model"  # Unrated (0 TPM)


def test_extract_rate_limit_records_from_text() -> None:
    """Verify regex extraction from vendor PDF raw string output."""
    raw_sample = """
    Gemini API Rate Limit Tier 1 Docs
    Gemini 2 Flash Text-out models 0 / 2K 0 / 4M 0 / Unlimited 
    Gemini 2 Flash Lite Text-out models 0 / 4K 0 / 4M 0 / Unlimited 
    Gemini 2.5 Flash Lite Text-out models 0 / 4K 0 / 4M 0 / Unlimited 
    Gemma 4 31B Other models 0 / 30 0 / 16K 0 / 14.4K 
    """
    records = extract_rate_limit_records(raw_sample)
    assert "Gemini 2 Flash" in records
    assert records["Gemini 2 Flash"]["RPM"] == "2K"
    assert records["Gemini 2 Flash"]["TPM"] == "4M"
    assert records["Gemini 2 Flash"]["RPD"] == "Unlimited"

    assert "Gemini 2.5 Flash Lite" in records
    assert records["Gemini 2.5 Flash Lite"]["TPM"] == "4M"


def test_parser_end_to_end_artifacts(tmp_path: Path) -> None:
    """Verify running the parser produces data/ tier JSON files and src/ registry sorted by TPM."""
    repo_root = Path(__file__).resolve().parents[3]
    data_dir = repo_root / "data"
    free_json = data_dir / "free_tier_rate_limits.json"
    paid_json = data_dir / "paid_tier_rate_limits.json"
    src_registry = (
        repo_root / "src" / "webhook_agent" / "assets" / "registries" / "gemini_models.json"
    )

    # Run parser directly
    run_parser([])

    # 1. Feature 1: Two separate JSON files in data/
    assert free_json.exists(), "data/free_tier_rate_limits.json must be generated"
    assert paid_json.exists(), "data/paid_tier_rate_limits.json must be generated"

    free_data = json.loads(free_json.read_text(encoding="utf-8"))
    paid_data = json.loads(paid_json.read_text(encoding="utf-8"))
    assert len(free_data) > 0
    assert len(paid_data) > 0

    # Verify sorting in paid_tier_rate_limits.json
    first_paid = next(iter(paid_data.values()))
    assert first_paid["TPM"] == "4M"

    # 2. Feature 2: Combined registry inside src/**
    assert src_registry.exists(), (
        "src/webhook_agent/assets/registries/gemini_models.json must exist"
    )
    registry_data = json.loads(src_registry.read_text(encoding="utf-8"))
    models = registry_data.get("models", [])
    assert len(models) > 0

    # 3. Feature 3: Models in src/ are sorted by TPM
    first_model = models[0]
    first_paid_tpm = first_model.get("rate_limits", {}).get("paid", {}).get("tpm", 0)
    assert first_paid_tpm == 4000000

    # 4. Feature 4: No synthetic fallback for unrated models
    unrated = next((m for m in models if m.get("name") == "models/gemini-flash-latest"), None)
    if unrated:
        assert unrated.get("rate_limits") is None


def test_resolve_registry_path_locates_src_registry() -> None:
    """Verify rate_limiter resolves to src/webhook_agent/assets/registries/gemini_models.json."""
    resolved = _resolve_registry_path()
    assert resolved.exists()
    assert "src/webhook_agent/assets/registries" in str(resolved)


def test_is_eligible_registry_model_filtering() -> None:
    """Verify is_eligible_registry_model filters non-text models and keyword exclusions."""
    valid_model = {
        "name": "models/gemini-2.5-flash",
        "display_name": "Gemini 2.5 Flash",
        "description": "Fast text model",
        "supported_actions": ["generateContent"],
    }
    embedding_model = {
        "name": "models/text-embedding-004",
        "display_name": "Text Embedding 004",
        "description": "Embedding model",
        "supported_actions": ["embedContent"],
    }
    excluded_keyword_model = {
        "name": "models/imagen-3.0-generate-002",
        "display_name": "Imagen 3",
        "description": "Image generation",
        "supported_actions": ["generateContent"],
    }

    assert is_eligible_registry_model(valid_model) is True
    assert is_eligible_registry_model(embedding_model) is False
    assert is_eligible_registry_model(excluded_keyword_model) is False
