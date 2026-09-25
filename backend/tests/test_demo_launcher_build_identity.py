from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.demo_launcher_pid import (
    decide_demo_start_action,
    decide_demo_stop_action,
    format_checkout_report,
    format_service_disposition,
    listener_belongs_to_owned_tree,
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
        "port_listening": True,
        "listener_owned": True,
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


def test_unrelated_listener_fails_clearly_rather_than_silently_reuse() -> None:
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


def test_free_port_starts_without_kill() -> None:
    dead = {"live_pid": None, "live_name": None, "live_path": None, "live_command_line": None}
    assert _start(port_listening=False, listener_owned=False, **dead) == "start"
    assert _start(port_listening=False, listener_owned=False, identity=None, **dead) == "start"
    assert start_action_allows_owned_stop("start") is False


def test_owned_current_sha_not_yet_listening_is_awaited_not_duplicated() -> None:
    assert _start(port_listening=False, listener_owned=False) == "await"
    assert start_action_allows_owned_stop("await") is False
    assert "waiting for its port instead of launching a second copy" in format_service_disposition(
        "frontend", "await", current_sha=CURRENT_SHA
    )


def test_owned_old_sha_not_yet_listening_restarts_owned_tree() -> None:
    identity = {**OWNED_IDENTITY, "git_head": OTHER_SHA}
    assert _start(identity=identity, port_listening=False, listener_owned=False) == "restart"


def test_unowned_or_other_checkout_process_with_free_port_starts() -> None:
    other_root = {**OWNED_IDENTITY, "repo_root": r"C:\Other-Clone"}
    assert _start(identity=other_root, port_listening=False, listener_owned=False) == "start"
    assert _start(identity={"pid": 4242}, port_listening=False, listener_owned=False) == "start"


def test_owned_root_alive_but_foreign_listener_is_conflict() -> None:
    assert _start(listener_owned=False) == "conflict"
    identity = {**OWNED_IDENTITY, "git_head": OTHER_SHA}
    assert _start(identity=identity, listener_owned=False) == "conflict"


FRONTEND_IDENTITY = {
    "pid": 5000,
    "path": r"C:\Windows\System32\cmd.exe",
    "command_tokens": ["run", "dev", "127.0.0.1", "3000"],
    "git_head": CURRENT_SHA,
    "repo_root": REPO,
}
FRONTEND_PROCESSES = [
    {"pid": 5000, "parent_pid": 1, "name": "cmd", "command_line": "cmd /c npm run dev -- -p 3000"},
    {
        "pid": 5001,
        "parent_pid": 5000,
        "name": "node",
        "command_line": rf"node {REPO}\frontend\node_modules\next\dist\bin\next dev",
    },
    {"pid": 5002, "parent_pid": 5001, "name": "node", "command_line": "node start-server.js next"},
    {"pid": 7777, "parent_pid": 1, "name": "node", "command_line": r"node C:\other\server.js"},
    {"pid": 8888, "parent_pid": 5001, "name": "notepad", "command_line": "notepad.exe"},
]


def _listener_owned(pids: list[int]) -> bool:
    return listener_belongs_to_owned_tree(
        FRONTEND_IDENTITY, listener_pids=pids, processes=FRONTEND_PROCESSES, label="frontend"
    )


def test_listener_owned_by_verified_descendant_or_root() -> None:
    assert _listener_owned([5002]) is True
    assert _listener_owned([5000]) is True
    assert _listener_owned([5001, 5002]) is True


def test_listener_owned_by_foreign_or_unknown_pid_fails_closed() -> None:
    assert _listener_owned([7777]) is False
    assert _listener_owned([5002, 7777]) is False
    assert _listener_owned([8888]) is False
    assert _listener_owned([4040]) is False
    assert _listener_owned([]) is False
    assert listener_belongs_to_owned_tree(
        None, listener_pids=[5002], processes=FRONTEND_PROCESSES, label="frontend"
    ) is False


def test_current_sha_shown_to_operator() -> None:
    branch_line, sha_line = format_checkout_report(branch="owner-live", sha=CURRENT_SHA)
    assert branch_line == "Current branch: owner-live"
    assert sha_line == f"Current SHA: {CURRENT_SHA}"
    start_ps1 = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1").read_text(encoding="utf-8")
    identity_ps1 = (REPO_ROOT / "scripts/windows/Demo-LauncherIdentity.ps1").read_text(
        encoding="utf-8"
    )
    assert "Current branch:" in start_ps1
    # PowerShell parses "$Label:" as an invalid scoped/drive-style variable reference.
    # Braced interpolation is required anywhere a variable is immediately followed by a colon.
    assert '$Label:' not in start_ps1
    assert '${Label}:' in start_ps1
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
    # Port release after an owned stop is verified from the OS listener, not HTTP.
    assert "Wait-HttpGone" not in start_ps1
    assert start_ps1.index("Wait-DemoPortGone -Port $Port -Label $Label") > start_ps1.index(
        "Stop-DemoPid -PidFile $PidFile -Label $Label"
    )
    assert "Wait-DemoPortGone -Port 3000 -Label $Label" in identity_ps1
    assert "Wait-DemoPortGone -Port 8000 -Label $Label" in identity_ps1
    assert "unexpected occupant" in identity_ps1


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
