"""Unit tests for the main distributed entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main

pytestmark = [pytest.mark.unit, pytest.mark.webhook_agent]


def test_src_in_sys_path() -> None:
    expected_src = str(Path(main.__file__).resolve().parent / "src")
    assert expected_src in sys.path


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


# --- Tests for LOG_MODEL_THOUGHTS logging configuration ---


@pytest.mark.parametrize(
    "log_model_thoughts,log_level,expected_level",
    [
        # LOG_MODEL_THOUGHTS=1 (default) forces DEBUG regardless of LOG_LEVEL
        ("1", "INFO", logging.DEBUG),
        ("true", "WARNING", logging.DEBUG),
        ("yes", "ERROR", logging.DEBUG),
        ("on", "CRITICAL", logging.DEBUG),
        # LOG_MODEL_THOUGHTS=0 respects LOG_LEVEL
        ("0", "INFO", logging.INFO),
        ("false", "WARNING", logging.WARNING),
        ("no", "ERROR", logging.ERROR),
        ("off", "CRITICAL", logging.CRITICAL),
        ("", "DEBUG", logging.DEBUG),
        ("random", "INFO", logging.INFO),
        # LOG_MODEL_THOUGHTS not set (default is "1" from constants) forces DEBUG
        (None, "INFO", logging.DEBUG),
        (None, "WARNING", logging.DEBUG),
    ],
)
def test_log_level_respects_log_model_thoughts(
    monkeypatch: pytest.MonkeyPatch,
    log_model_thoughts: str | None,
    log_level: str,
    expected_level: int,
) -> None:
    """Verify logging.basicConfig is called with correct level based on LOG_MODEL_THOUGHTS."""
    # We need to test the module-level code that runs on import
    # Since main.py runs logging config at import time, we test by reloading the module
    import importlib

    # Set up environment
    if log_model_thoughts is None:
        monkeypatch.delenv("LOG_MODEL_THOUGHTS", raising=False)
    else:
        monkeypatch.setenv("LOG_MODEL_THOUGHTS", log_model_thoughts)
    monkeypatch.setenv("LOG_LEVEL", log_level)

    # Capture logging.basicConfig calls
    with patch("logging.basicConfig") as mock_basic_config:
        # Reload main module to trigger the logging config code
        importlib.reload(main)
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args.kwargs
        assert call_kwargs["level"] == expected_level


def test_log_level_defaults_to_info_when_log_model_thoughts_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LOG_MODEL_THOUGHTS=0 and LOG_LEVEL not set, defaults to INFO."""
    import importlib

    monkeypatch.setenv("LOG_MODEL_THOUGHTS", "0")
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    with patch("logging.basicConfig") as mock_basic_config:
        importlib.reload(main)
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args.kwargs
        assert call_kwargs["level"] == logging.INFO


def test_log_level_defaults_to_debug_when_log_model_thoughts_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LOG_MODEL_THOUGHTS=1 (default) and LOG_LEVEL not set, defaults to DEBUG."""
    import importlib

    monkeypatch.delenv("LOG_MODEL_THOUGHTS", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    with patch("logging.basicConfig") as mock_basic_config:
        importlib.reload(main)
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args.kwargs
        assert call_kwargs["level"] == logging.DEBUG
