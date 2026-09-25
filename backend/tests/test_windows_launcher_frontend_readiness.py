"""Launcher startup gating: frontend readiness is the lightweight
``/api/desktop/status`` route, never the expensive operator homepage ``/``.

The homepage stays the browser target. The PID-identity occupancy probe and
unrelated-listener refusal are unchanged, and the PAPER environment contract
is still enforced by both the PowerShell launcher and SportsHedge.exe.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
START_PS1 = REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1"
STATUS_ROUTE = REPO_ROOT / "frontend/app/api/desktop/status/route.ts"
DESKTOP_CORE = REPO_ROOT / "desktop/SportsHedge.Launcher.Core"
HEALTH_PROBE = DESKTOP_CORE / "Health/HealthProbe.cs"
SESSION_CONTROLLER = DESKTOP_CORE / "SessionController.cs"
LAUNCHER_CONSTANTS = DESKTOP_CORE / "LauncherConstants.cs"
CHILD_ENVIRONMENT = DESKTOP_CORE / "ProcessEnvironment/ChildEnvironment.cs"


def _ps1() -> str:
    return START_PS1.read_text(encoding="utf-8")


def _assignment(script: str, name: str) -> str:
    match = re.search(rf'^\${name} = "([^"]*)"\s*$', script, flags=re.MULTILINE)
    assert match, f"${name} is not assigned in Start-SportsHedge-Demo.ps1"
    return match.group(1)


def _wait_http_ok_urls(script: str) -> list[str]:
    return re.findall(r"^Wait-HttpOk -Url \$(\w+)", script, flags=re.MULTILINE)


def test_powershell_frontend_readiness_gate_is_the_lightweight_status_route() -> None:
    script = _ps1()
    assert _assignment(script, "FrontendReady") == "http://127.0.0.1:3000/api/desktop/status"
    assert _wait_http_ok_urls(script) == ["BackendHealth", "FrontendReady"]
    assert STATUS_ROUTE.exists()


def test_powershell_homepage_is_not_a_readiness_gate() -> None:
    script = _ps1()
    gate_vars = _wait_http_ok_urls(script)
    for var in gate_vars:
        url = _assignment(script, var)
        assert url.rstrip("/") not in {"http://127.0.0.1:3000", "http://localhost:3000"}, (
            f"Wait-HttpOk gates startup on the operator homepage via ${var}={url}"
        )
    assert "Wait-HttpOk -Url $DemoUrl" not in script
    assert "Wait-HttpOk -Url $FrontendHealth" not in script


def test_powershell_backend_health_verification_is_kept() -> None:
    script = _ps1()
    assert _assignment(script, "BackendHealth") == "http://127.0.0.1:8000/health"
    assert 'Wait-HttpOk -Url $BackendHealth -Label "Sports Hedge backend" | Out-Null' in script


def test_powershell_browser_target_remains_homepage_after_readiness() -> None:
    script = _ps1()
    assert _assignment(script, "DemoUrl") == "http://127.0.0.1:3000/"
    open_idx = script.index("Start-Process $DemoUrl")
    assert script.index("Wait-HttpOk -Url $FrontendReady") < open_idx
    assert script.index("Wait-HttpOk -Url $BackendHealth") < open_idx


def test_powershell_unrelated_listener_protection_unchanged() -> None:
    script = _ps1()
    # The reuse/restart/conflict decision still probes any HTTP listener on 3000,
    # so an unrelated occupant answering "/" is refused rather than reused/killed.
    assert _assignment(script, "FrontendHealth") == "http://127.0.0.1:3000"
    assert (
        'Invoke-DemoOwnedService -Label "frontend" -HealthUrl $FrontendHealth '
        "-PidFile $FrontendPidFile"
    ) in script
    assert (
        'Invoke-DemoOwnedService -Label "backend" -HealthUrl $BackendHealth '
        "-PidFile $BackendPidFile"
    ) in script
    assert "$healthOk = Test-HttpOk $HealthUrl" in script
    assert "Get-DemoStartAction -HealthOk $healthOk -Identity $identity -Live $live" in script
    assert "Refusing to reuse or kill the unrelated process occupying the port." in script
    assert "Wait-HttpGone -Url $HealthUrl -Label $Label" in script
    assert "Refusing to kill an unexpected occupant of the port." in script
    restart_idx = script.index('if ($action -eq "restart") {')
    assert script.index("Stop-DemoPid -PidFile $PidFile -Label $Label") > restart_idx


def test_powershell_paper_enforcement_unchanged() -> None:
    script = _ps1()
    assert '$env:SPORTS_HEDGE_MODE = "paper"' in script
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in script
    env_idx = script.index('$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"')
    assert env_idx < script.index('Invoke-DemoOwnedService -Label "backend"')


def test_exe_startup_frontend_readiness_uses_status_only() -> None:
    probe = HEALTH_PROBE.read_text(encoding="utf-8")
    controller = SESSION_CONTROLLER.read_text(encoding="utf-8")
    assert "includePage" not in probe
    assert "includePage" not in controller
    assert 'FrontendStatusPath = "/api/desktop/status"' in probe
    assert "LauncherConstants.AppUrl" not in probe
    assert "HealthEvaluator.EvaluateFrontendStatus(status.Detail, expectedSha, sessionId)" in probe
    assert "_deps.Health.CheckFrontendAsync(Git.Sha, _secrets.SessionId, ct)" in controller


def test_exe_frontend_identity_verification_remains_mandatory() -> None:
    probe = HEALTH_PROBE.read_text(encoding="utf-8")
    assert "interface does not report desktop-controller mode" in probe
    assert "(session mismatch)" in probe
    assert "interface reports Git SHA" in probe


def test_exe_browser_target_and_paper_enforcement_unchanged() -> None:
    constants = LAUNCHER_CONSTANTS.read_text(encoding="utf-8")
    probe = HEALTH_PROBE.read_text(encoding="utf-8")
    child_env = CHILD_ENVIRONMENT.read_text(encoding="utf-8")
    assert 'AppUrl = "http://127.0.0.1:3000/"' in constants
    assert 'Str(root, "mode") != "paper"' in probe
    assert "backend does not report execution_enabled=false" in probe
    assert '["SPORTS_HEDGE_MODE"] = "paper"' in child_env
    assert '["SPORTS_HEDGE_EXECUTION_ENABLED"] = "false"' in child_env
