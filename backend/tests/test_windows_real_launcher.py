"""Static contract for the Windows Real dry-run launcher.

The paper demo launcher stays on its own PID/log namespace and paper settings.
These tests do not start uvicorn or Node.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS = REPO_ROOT / "scripts" / "windows"
REAL_START = WINDOWS / "Start-SportsHedge-Real.ps1"
REAL_STOP = WINDOWS / "Stop-SportsHedge-Real.ps1"
REAL_START_BAT = WINDOWS / "Start-SportsHedge-Real.bat"
REAL_STOP_BAT = WINDOWS / "Stop-SportsHedge-Real.bat"
DEMO_START = WINDOWS / "Start-SportsHedge-Demo.ps1"
DEMO_STOP = WINDOWS / "Stop-SportsHedge-Demo.ps1"
RUNBOOK = REPO_ROOT / "docs" / "REAL_RUNBOOK.md"

REAL_ENV = {
    "SPORTS_HEDGE_MODE": "real",
    "SPORTS_HEDGE_EXECUTION_ENABLED": "false",
    "PAPER_LIVE_REFRESH_ENABLED": "true",
    "PAPER_AUTOFILL_ENABLED": "false",
    "PAPER_AUTO_UNWIND_ENABLED": "false",
    "ACCOUNTING_SCHEDULE_ENABLED": "false",
}

RESUME_ENDPOINTS = (
    "/scanner/resume",
    "/scanner/universe-schedule/resume",
    "/scanner/background-pricing/resume",
    "/scanner/settlement-scans/resume",
)

BROAD_KILL_MARKERS = (
    "taskkill",
    "stop-process -name",
    "get-process python",
    "get-process node",
    "killall",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _env_assignments(script: str) -> dict[str, str]:
    return dict(re.findall(r'^\$env:([A-Z0-9_]+) = "([^"]*)"', script, flags=re.MULTILINE))


def test_real_launcher_files_exist() -> None:
    for path in (REAL_START, REAL_STOP, REAL_START_BAT, REAL_STOP_BAT, RUNBOOK):
        assert path.is_file(), path


def test_real_runtime_environment_is_explicit() -> None:
    assignments = _env_assignments(_read(REAL_START))
    for name, expected in REAL_ENV.items():
        assert assignments[name] == expected
    assert assignments["SPORTS_HEDGE_MODE"] == "real"
    assert assignments["SPORTS_HEDGE_EXECUTION_ENABLED"] == "false"
    assert assignments["PAPER_LIVE_REFRESH_ENABLED"] == "true"
    assert assignments["PAPER_AUTOFILL_ENABLED"] == "false"
    assert assignments["PAPER_AUTO_UNWIND_ENABLED"] == "false"


def test_real_health_gate_requires_mode_execution_and_scanner_loop() -> None:
    script = _read(REAL_START)
    gate = script[script.index("function Test-RealHealthGate") : script.index("function Format-RealHealthGate")]
    assert '[string]$Health.mode -ne "real"' in gate
    assert "$Health.execution_enabled -ne $false" in gate
    assert "$Health.live_refresh.server_loop_enabled -ne $true" in gate
    assert "Invoke-RestMethod -Uri $Url" in script
    running_at = script.index("Sports Hedge Real is running.")
    assert script.index("Test-RealHealthGate -Health $health") < running_at
    assert script.index("live_refresh.server_loop_enabled=true") < running_at
    assert "GET /health confirmed mode=real execution_enabled=false" in script


def test_real_launcher_does_not_resume_or_unpause_operator_controls() -> None:
    combined = "\n".join(_read(path) for path in (REAL_START, REAL_STOP, REAL_START_BAT, REAL_STOP_BAT))
    lowered = combined.lower()
    for endpoint in RESUME_ENDPOINTS:
        assert endpoint not in lowered
    assert "invoke-restmethod" in lowered
    assert "-method post" not in lowered
    assert "background pricing is persisted as paused by operator." in lowered
    assert "persisted operator pauses were reported only" in lowered
    assert "does not resume scanner" in lowered


def test_real_launcher_does_not_broadly_kill_processes() -> None:
    combined = "\n".join(
        _read(path).lower() for path in (REAL_START, REAL_STOP, REAL_START_BAT, REAL_STOP_BAT)
    )
    for marker in BROAD_KILL_MARKERS:
        assert marker not in combined
    stop = _read(REAL_STOP)
    assert "Stop-DemoPid" in stop
    assert "real-backend.pid" in stop
    assert "real-frontend.pid" in stop
    assert "demo-backend.pid" not in stop
    assert "demo-frontend.pid" not in stop
    start = _read(REAL_START)
    assert "Get-DemoPortListenerPids" in start
    assert "Refusing to reuse or kill the unrelated process" in start
    assert re.search(r"(?m)^[^#\r\n]*Start-Process\s+-Command", start) is None
    assert '-FilePath $Python -ArgumentList @(' in start
    assert "npm.cmd" in start


def test_real_and_demo_pid_and_log_namespaces_are_separate() -> None:
    start = _read(REAL_START)
    demo = _read(DEMO_START)
    for name in (
        "real-backend.pid",
        "real-frontend.pid",
        "real-backend.out.log",
        "real-backend.err.log",
        "real-frontend.out.log",
        "real-frontend.err.log",
    ):
        assert name in start
        assert name not in demo
    for name in (
        "demo-backend.pid",
        "demo-frontend.pid",
        "demo-backend.out.log",
        "demo-backend.err.log",
    ):
        assert name in demo
        assert name not in start
        assert name not in _read(REAL_STOP)


def test_real_startup_wording_and_process_shape() -> None:
    script = _read(REAL_START)
    assert "REAL MODE" in script
    assert "LIVE MARKET SCANNING ENABLED" in script
    assert "LIVE ORDER EXECUTION DISABLED" in script
    assert "Sports Hedge paper demo is running." not in script
    assert "PAPER MODE." not in script
    assert 'python.exe -m uvicorn sports_hedge.api.main:app --host 127.0.0.1 --port 8000' not in script
    assert '"-m", "uvicorn", "sports_hedge.api.main:app"' in script
    assert '"--host", "127.0.0.1", "--port", "8000"' in script
    assert '-WorkingDirectory $backendDir' in script
    assert "http://127.0.0.1:3000/" in script
    assert "scanner_stopped=" in script
    assert "universe_scans_paused=" in script
    assert "background_pricing_paused=" in script
    assert "settlement_scans_paused=" in script
    assert "Serving Git branch:" in script
    bat = _read(REAL_START_BAT)
    assert "Start-SportsHedge-Real.ps1" in bat
    assert "-File" in bat
    assert "-Command" not in bat


def test_real_runbook_documents_scanner_loop_and_port_ownership() -> None:
    runbook = _read(RUNBOOK)
    assert (
        "powershell.exe -ExecutionPolicy Bypass -File .\\scripts\\windows\\Start-SportsHedge-Real.ps1"
        in runbook
    )
    assert "scripts\\windows\\Start-SportsHedge-Real.bat" in runbook
    assert "live_refresh.server_loop_enabled" in runbook
    assert "does **not** switch the runtime into paper mode" in runbook
    assert "WinError 10048" in runbook
    assert "execution_enabled" in runbook
    assert "false" in runbook
    assert "Start-Process -Command" in runbook


def test_demo_launcher_behavior_remains_unchanged() -> None:
    start = _read(DEMO_START)
    stop = _read(DEMO_STOP)
    assignments = _env_assignments(start)
    assert assignments["SPORTS_HEDGE_MODE"] == "paper"
    assert assignments["SPORTS_HEDGE_EXECUTION_ENABLED"] == "false"
    assert assignments["PAPER_AUTOFILL_ENABLED"] == "true"
    assert assignments["PAPER_AUTO_UNWIND_ENABLED"] == "true"
    assert assignments["PAPER_LIVE_REFRESH_ENABLED"] == "true"
    assert assignments["ACCOUNTING_SCHEDULE_ENABLED"] == "true"
    assert "demo-backend.pid" in start
    assert "demo-frontend.pid" in start
    assert "Sports Hedge paper demo is running." in start
    assert "real-backend.pid" not in start
    assert "Test-RealHealthGate" not in start
    assert "Stop-DemoPid" in stop
    assert "demo-backend.pid" in stop
    assert "real-backend.pid" not in stop
