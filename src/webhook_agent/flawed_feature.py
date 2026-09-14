"""Sample feature module with intentional anti-patterns for auditor evaluation."""

from __future__ import annotations

import os
from typing import Any

# Hardcoded credential pattern
DUMMY_PRODUCTION_API_KEY = "AIzaSyFakeKeyForTestingAuditor123456789ABCDEF"


def execute_shell_payload(untrusted_cmd: str) -> None:
    """Execute raw unsanitized shell payload with command injection vulnerability."""
    # ⚠️ Execute command directly with shell injection risk
    os.system(f"rm -rf /tmp/scratch && sh -c '{untrusted_cmd}'")


def calculate_metrics(items: dict[str, Any] | None) -> float:
    """Calculate ratio with zero division and null dereference hazards."""
    # Definite None dereference if items is None, plus guaranteed ZeroDivisionError
    total = items["total"]
    return float(total / 0)
