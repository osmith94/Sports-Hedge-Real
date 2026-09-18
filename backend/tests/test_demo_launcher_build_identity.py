from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.demo_launcher_pid import (
    decide_demo_start_action,
    decide_demo_stop_action,
    format_checkout_report,
    format_service_disposition,
    start_action_allows_owned_stop,
)
from sports_hedge.application.serving_build import (
    capture_serving_build_info,
    get_serving_build_info,
    reset_serving_build_info,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRENT_SHA = "dab5ea649acda69bc1de4c08ccbeff87024bc0ad"
OTHER_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REPO = r"C:\Sports-Hedge"
PYTHON = r"C:\Sports-Hedge\backend\.venv\Scripts\python.exe"
OWNED_IDENTITY = {
    "pid": 4242,
    "path": PYTHON,
    "command_tokens": ["uvicorn", "sports_hedge.api.main:app"],
    "git_head": CURRENT_SHA,
    "repo_root": REPO,
}
OWNED_LIVE = {
    "live_pid": 4242,
    "live_name": "python",
    "live_path": PYTHON,
    "live_command_line": (
        r'"C:\Sports-Hedge\backend\.venv\Scripts\python.exe" -m uvicorn '
        "sports_hedge.api.main:app --host 127.0.0.1 --port 8000"
    ),
}


def _start(**overrides):
    payload = {
        "identity": OWNED_IDENTITY,
        "health_ok": True,
        "current_git_head": CURRENT_SHA,
        "current_repo_root": REPO,
        **OWNED_LIVE,
    }
    payload.update(overrides)
    return decide_demo_start_action(**payload)


def test_same_sha_reuse_allowed() -> None:
    assert _start() == "reuse"
    assert start_action_allows_owned_stop("reuse") is False


def test_different_sha_owned_process_restart_required() -> None:
    identity = {**OWNED_IDENTITY, "git_head": OTHER_SHA}
    assert _start(identity=identity) == "restart"
    assert start_action_allows_owned_stop("restart") is True
    assert decide_demo_stop_action(identity, **OWNED_LIVE) == "stop"


def test_legacy_owned_identity_without_sha_restarts() -> None:
    identity = {
        "pid": 4242,
        "path": PYTHON,
        "command_tokens": ["uvicorn", "sports_hedge.api.main:app"],
    }
    assert _start(identity=identity) == "restart"
    assert start_action_allows_owned_stop("restart") is True


def test_stale_untrusted_pid_identity_is_not_killed() -> None:
    identity = {**OWNED_IDENTITY, "command_tokens": ["uvicorn", "sports_hedge.api.main:app"]}
    stale_live = {
        "live_pid": 4242,
        "live_name": "notepad",
        "live_path": r"C:\Windows\System32\notepad.exe",
        "live_command_line": r"C:\Windows\System32\notepad.exe",
    }
    assert decide_demo_stop_action(identity, **stale_live) == "stale"
    action = _start(identity=identity, **stale_live)
    assert action == "conflict"
    assert start_action_allows_owned_stop(action) is False
    assert decide_demo_stop_action({"pid": 4242}, **OWNED_LIVE) == "stale"
    assert _start(identity={"pid": 4242}, **OWNED_LIVE) == "conflict"


def test_unrelated_healthy_process_fails_clearly_rather_than_silently_reuse() -> None:
    action = _start(identity=None, live_pid=None, live_name=None, live_path=None, live_command_line=None)
    assert action == "conflict"
    assert start_action_allows_owned_stop(action) is False
    message = format_service_disposition(
        "backend",
        action,
        current_sha=CURRENT_SHA,
    )
    assert "Refusing to reuse or kill" in message
    other_root = {**OWNED_IDENTITY, "repo_root": r"C:\Other-Clone"}
    assert _start(identity=other_root) == "conflict"
    assert start_action_allows_owned_stop("conflict") is False


def test_unhealthy_port_starts_without_kill() -> None:
    assert _start(health_ok=False) == "start"
    assert start_action_allows_owned_stop("start") is False


def test_current_sha_shown_to_operator() -> None:
    branch_line, sha_line = format_checkout_report(branch="owner-live", sha=CURRENT_SHA)
    assert branch_line == "Current branch: owner-live"
    assert sha_line == f"Current SHA: {CURRENT_SHA}"
    start_ps1 = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1").read_text(encoding="utf-8")
    identity_ps1 = (REPO_ROOT / "scripts/windows/Demo-LauncherIdentity.ps1").read_text(
        encoding="utf-8"
    )
    assert "Current branch:" in start_ps1
    assert "Current SHA:" in start_ps1
    assert "reused existing Sports Hedge process for SHA" in start_ps1
    assert "restarted owned process because recorded SHA" in start_ps1
    assert "Refusing to reuse or kill" in start_ps1
    assert "Get-RepoGitIdentity" in start_ps1
    assert "rev-parse HEAD" in identity_ps1
    assert "git_head" in identity_ps1
    assert "repo_root" in identity_ps1
    assert "Get-DemoStartAction" in start_ps1
    assert "Stop-DemoPid" in start_ps1
    assert "$backendAlready = Test-HttpOk $BackendHealth" not in start_ps1
    assert "$frontendAlready = Test-HttpOk $FrontendHealth" not in start_ps1
    reuse = format_service_disposition("backend", "reuse", current_sha=CURRENT_SHA)
    restart = format_service_disposition(
        "backend",
        "restart",
        current_sha=CURRENT_SHA,
        recorded_sha=OTHER_SHA,
    )
    assert reuse in start_ps1 or "reused existing Sports Hedge process for SHA" in start_ps1
    assert "restarted owned process because recorded SHA" in restart
    assert "SPORTS_HEDGE_GIT_SHA" in start_ps1
    assert "/build-info" in start_ps1


def test_restart_path_stops_only_after_ownership_check() -> None:
    identity_ps1 = (REPO_ROOT / "scripts/windows/Demo-LauncherIdentity.ps1").read_text(
        encoding="utf-8"
    )
    stop_index = identity_ps1.index("Stop-Process -Id")
    assert identity_ps1.index("Test-DemoPidOwned") < stop_index
    assert identity_ps1.index('$action -ne "stop"') < stop_index
    start_ps1 = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1").read_text(encoding="utf-8")
    restart_index = start_ps1.index('$action -eq "restart"')
    assert start_ps1.index("Stop-DemoPid") > restart_index
    assert "Wait-HttpGone" in start_ps1
    assert "unexpected occupant" in start_ps1


def test_build_info_prefers_launcher_env_over_git() -> None:
    info = capture_serving_build_info(
        environ_map={
            "SPORTS_HEDGE_GIT_SHA": OTHER_SHA,
            "SPORTS_HEDGE_GIT_BRANCH": "feature/old",
            "SPORTS_HEDGE_REPO_ROOT": REPO,
        }
    )
    assert info.git_sha == OTHER_SHA
    assert info.git_branch == "feature/old"
    assert info.source == "env"
    assert info.as_public_dict()["data_kind"] == "runtime_build_identity"
    assert "repo_root" not in info.as_public_dict()


def test_build_info_endpoint_and_health_report_serving_sha(monkeypatch) -> None:
    monkeypatch.setenv("SPORTS_HEDGE_GIT_SHA", CURRENT_SHA)
    monkeypatch.setenv("SPORTS_HEDGE_GIT_BRANCH", "owner-live")
    reset_serving_build_info()
    client = TestClient(app)
    build = client.get("/build-info")
    assert build.status_code == 200
    payload = build.json()
    assert payload["git_sha"] == CURRENT_SHA
    assert payload["git_branch"] == "owner-live"
    assert payload["source"] == "env"
    assert payload["data_kind"] == "runtime_build_identity"
    health = client.get("/health").json()
    assert health["execution_enabled"] is False
    assert health["mode"] == "paper"
    assert health["build"]["git_sha"] == CURRENT_SHA
    assert health["build"]["data_kind"] == "runtime_build_identity"


def test_build_info_git_fallback_uses_real_checkout_when_env_missing() -> None:
    info = capture_serving_build_info(environ_map={})
    assert info.source in {"git", "unavailable"}
    if info.source == "git":
        assert info.git_sha
        assert len(info.git_sha) >= 7


def test_serving_build_snapshot_does_not_follow_later_env_changes(monkeypatch) -> None:
    monkeypatch.setenv("SPORTS_HEDGE_GIT_SHA", CURRENT_SHA)
    reset_serving_build_info()
    first = get_serving_build_info()
    monkeypatch.setenv("SPORTS_HEDGE_GIT_SHA", OTHER_SHA)
    second = get_serving_build_info()
    assert first.git_sha == CURRENT_SHA
    assert second.git_sha == CURRENT_SHA
    reset_serving_build_info()
