"""Collection-time import graph. Distinguishes ImportError from liveness.

Windows targeted liveness collects issue572 first. That path used to raise
``ImportError: request_from_complete_set`` from a partially initialized
allocation.adapters module. Fixture/demo only. No venue orders.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src"

_CLEAN_IMPORTS = """
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.allocation.adapters import request_from_complete_set
from sports_hedge.arbitrage.priority_alerts import PriorityAlertService
from sports_hedge.api import desktop_host
assert PaperScanService is not None
assert callable(request_from_complete_set)
assert PriorityAlertService is not None
assert desktop_host is not None
"""


def _clean_python(code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_paper_scan_adapters_priority_alerts_import_in_clean_interpreter() -> None:
    result = _clean_python(_CLEAN_IMPORTS)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "partially initialized" not in (result.stderr or "")


def test_windows_liveness_modules_collect() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/test_issue572_event_loop_stalls.py",
            "tests/test_issue517_windows_event_loop_liveness.py",
            "tests/test_issue496_event_loop_responsiveness.py",
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "ImportError" not in (result.stderr or "")
    assert "error during collection" not in (result.stdout or "").lower()
