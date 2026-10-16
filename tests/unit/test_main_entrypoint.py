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


@pytest.mark.unit
@pytest.mark.parametrize("worker_exitcode,expected_exit", [(0, 0), (3, 3), (None, 0)])
def test_main_propagates_worker_exitcode(worker_exitcode: int | None, expected_exit: int) -> None:
    mock_proc = MagicMock()
    mock_proc.exitcode = worker_exitcode

    with patch("multiprocessing.Process", return_value=mock_proc):
        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == expected_exit
        mock_proc.start.assert_called_once()
        mock_proc.join.assert_called_once()
