"""Unit tests for the main distributed entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main

pytestmark = [pytest.mark.unit]


@pytest.mark.unit
def test_src_in_sys_path() -> None:
    expected_src = str(Path(main.__file__).resolve().parent / "src")
    assert expected_src in sys.path


@pytest.mark.unit
def test_run_worker_imports_and_calls_worker_main() -> None:
    mock_webhook_agent = MagicMock()
    mock_worker = MagicMock()
    mock_worker.main.return_value = 0
    mock_webhook_agent.worker = mock_worker

    with patch.dict(
        "sys.modules",
        {"webhook_agent": mock_webhook_agent, "webhook_agent.worker": mock_worker},
    ):
        with pytest.raises(SystemExit) as exc_info:
            main.run_worker()
        assert exc_info.value.code == 0
        mock_worker.main.assert_called_once()
