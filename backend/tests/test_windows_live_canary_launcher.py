"""Static contract for the Wave 40 live-canary Windows launcher.

Does not start uvicorn, Node, or place venue orders. The dry-run Real launcher
must stay execution-off.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS = REPO_ROOT / "scripts" / "windows"
CANARY_START = WINDOWS / "Start-SportsHedge-LiveCanary.ps1"
CANARY_STOP = WINDOWS / "Stop-SportsHedge-LiveCanary.ps1"
CANARY_START_BAT = WINDOWS / "Start-SportsHedge-LiveCanary.bat"
CANARY_STOP_BAT = WINDOWS / "Stop-SportsHedge-LiveCanary.bat"
REAL_START = WINDOWS / "Start-SportsHedge-Real.ps1"
RUNBOOK = REPO_ROOT / "docs" / "LIVE_CANARY_RUNBOOK.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _env_assignments(script: str) -> dict[str, str]:
    return dict(re.findall(r'^\$env:([A-Z0-9_]+) = "([^"]*)"', script, flags=re.MULTILINE))


def test_live_canary_launcher_files_exist() -> None:
    for path in (CANARY_START, CANARY_STOP, CANARY_START_BAT, CANARY_STOP_BAT, RUNBOOK):
        assert path.is_file(), path


def test_real_launcher_still_forces_execution_off() -> None:
    assignments = _env_assignments(_read(REAL_START))
    assert assignments["SPORTS_HEDGE_EXECUTION_ENABLED"] == "false"
    assert assignments["PAPER_AUTOFILL_ENABLED"] == "false"
    assert "SPORTS_HEDGE_LIVE_CANARY_ENABLED" not in assignments


def test_canary_launcher_requires_interactive_phrase() -> None:
    script = _read(CANARY_START)
    assert "LIVE REAL-MONEY ORDERS" in script
    assert "Read-Host" in script
    assert "ARM LIVE CANARY" in script
    assert "[Environment]::UserInteractive" in script
    assert "SPORTS_HEDGE_LIVE_CANARY_SKIP" not in script
    assignments = _env_assignments(script)
    assert assignments["SPORTS_HEDGE_EXECUTION_ENABLED"] == "true"
    assert assignments["SPORTS_HEDGE_LIVE_CANARY_ENABLED"] == "true"
    assert assignments["PAPER_AUTOFILL_ENABLED"] == "false"
    exec_idx = script.index('$env:SPORTS_HEDGE_EXECUTION_ENABLED = "true"')
    confirm_idx = script.index("Read-Host")
    assert confirm_idx < exec_idx


def test_canary_health_gate_does_not_use_live_execution_ready() -> None:
    script = _read(CANARY_START)
    gate = script[
        script.index("function Test-LiveCanaryHealthGate") : script.index(
            "function Format-LiveCanaryHealthGate"
        )
    ]
    assert "live_execution_ready" not in gate
    assert "$Health.execution_enabled -ne $true" in gate
    assert "$Health.paper_autofill_enabled -ne $false" in gate
    assert "$Health.live_canary.enabled -ne $true" in gate
    assert "live_execution_ready" in script
    assert "is not permission" in script


def test_canary_uses_separate_pid_files_and_refuses_foreign_occupants() -> None:
    start = _read(CANARY_START)
    stop = _read(CANARY_STOP)
    assert "live-canary-backend.pid" in start
    assert "live-canary-frontend.pid" in start
    assert "real-backend.pid" not in start
    assert "live-canary-backend.pid" in stop
    assert "real-backend.pid" not in stop
    assert "not reused or killed" in start
    assert "taskkill" not in start.lower()
    assert "VPN" in start


def test_runbook_documents_cap_honesty_and_halt() -> None:
    text = _read(RUNBOOK)
    assert "requested GBP outlay" in text
    assert "HALT NEW ORDERS" in text
    assert "does **not** cancel" in text
    assert "Start-SportsHedge-Real.ps1" in text
    assert "live_execution_ready" in text
