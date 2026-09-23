from __future__ import annotations

from pathlib import Path

from sports_hedge.application.demo_launcher_pid import (
    collect_owned_tree_pids,
    decide_demo_stop_action,
    decide_descendant_stop_action,
    format_refresh_descendant_alive_message,
    format_refresh_port_occupied_message,
    plan_owned_process_tree_stop,
    refresh_allows_next_cache_clear,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
IDENTITY_PS1 = REPO_ROOT / "scripts/windows/Demo-LauncherIdentity.ps1"
REFRESH_PS1 = REPO_ROOT / "scripts/windows/Refresh-SportsHedge-Demo.ps1"
REFRESH_BAT = REPO_ROOT / "scripts/windows/Refresh-SportsHedge-Demo.bat"
START_PS1 = REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1"
STOP_PS1 = REPO_ROOT / "scripts/windows/Stop-SportsHedge-Demo.ps1"

REPO = r"C:\Sports-Hedge"
NPM = r"C:\Program Files\nodejs\npm.cmd"
NODE = r"C:\Program Files\nodejs\node.exe"
OWNED_FRONTEND = {
    "pid": 100,
    "path": NPM,
    "name": "cmd",
    "command_tokens": ["run", "dev", "127.0.0.1", "3000"],
    "git_head": "dab5ea649acda69bc1de4c08ccbeff87024bc0ad",
    "repo_root": REPO,
}
OWNED_FRONTEND_LIVE = {
    "live_pid": 100,
    "live_name": "cmd",
    "live_path": NPM,
    "live_command_line": (
        r'"C:\Program Files\nodejs\npm.cmd" run dev -- -H 127.0.0.1 -p 3000'
    ),
}

FRONTEND_TREE = (
    {
        "pid": 100,
        "parent_pid": 1,
        "name": "cmd",
        "path": NPM,
        "command_line": OWNED_FRONTEND_LIVE["live_command_line"],
    },
    {
        "pid": 101,
        "parent_pid": 100,
        "name": "cmd",
        "path": r"C:\Windows\System32\cmd.exe",
        "command_line": r"C:\Windows\system32\cmd.exe /d /s /c npm run dev",
    },
    {
        "pid": 102,
        "parent_pid": 101,
        "name": "node",
        "path": NODE,
        "command_line": (
            rf'"{NODE}" "{REPO}\frontend\node_modules\npm\bin\npm-cli.js" '
            "run dev -- -H 127.0.0.1 -p 3000"
        ),
    },
    {
        "pid": 103,
        "parent_pid": 102,
        "name": "node",
        "path": NODE,
        "command_line": (
            rf'"{NODE}" "{REPO}\frontend\node_modules\next\dist\bin\next" '
            "dev -H 127.0.0.1 -p 3000"
        ),
    },
    {
        "pid": 999,
        "parent_pid": 1,
        "name": "node",
        "path": NODE,
        "command_line": r'"C:\Program Files\nodejs\node.exe" C:\OtherApp\server.js',
    },
)


def _plan(**overrides):
    payload = {
        "identity": OWNED_FRONTEND,
        "processes": FRONTEND_TREE,
        "label": "frontend",
        **OWNED_FRONTEND_LIVE,
    }
    payload.update(overrides)
    return plan_owned_process_tree_stop(**payload)


def test_owned_npm_wrapper_stops_next_child_deepest_first() -> None:
    action, stop_pids, refused = _plan()
    assert action == "stop"
    assert stop_pids == [103, 102, 101, 100]
    assert 999 not in stop_pids
    assert refused == []
    assert collect_owned_tree_pids(100, FRONTEND_TREE) == [103, 102, 101, 100]
    next_child = decide_descendant_stop_action(
        OWNED_FRONTEND,
        descendant_pid=103,
        tree_pids={100, 101, 102, 103},
        label="frontend",
        live_name="node",
        live_path=NODE,
        live_command_line=FRONTEND_TREE[3]["command_line"],
    )
    assert next_child == "stop"
    assert (
        decide_descendant_stop_action(
            OWNED_FRONTEND,
            descendant_pid=103,
            tree_pids={100, 101, 102, 103},
            label="frontend",
            live_name="node",
            live_path=NODE,
            live_command_line=None,
        )
        == "stop"
    )


def test_unrelated_node_process_is_never_killed() -> None:
    action, stop_pids, refused = _plan()
    assert action == "stop"
    assert 999 not in stop_pids
    assert 999 not in refused
    assert (
        decide_descendant_stop_action(
            OWNED_FRONTEND,
            descendant_pid=999,
            tree_pids={100, 101, 102, 103},
            label="frontend",
            live_name="node",
            live_path=NODE,
            live_command_line=FRONTEND_TREE[4]["command_line"],
        )
        == "refuse"
    )
    in_tree_unrelated = decide_descendant_stop_action(
        OWNED_FRONTEND,
        descendant_pid=103,
        tree_pids={100, 101, 102, 103},
        label="frontend",
        live_name="node",
        live_path=NODE,
        live_command_line=r'"C:\Program Files\nodejs\node.exe" C:\OtherApp\server.js',
    )
    assert in_tree_unrelated == "refuse"


def test_stale_pid_file_still_fails_safely_without_tree_stop() -> None:
    stale_live = {
        "live_pid": 100,
        "live_name": "notepad",
        "live_path": r"C:\Windows\System32\notepad.exe",
        "live_command_line": r"C:\Windows\System32\notepad.exe",
    }
    assert decide_demo_stop_action(OWNED_FRONTEND, **stale_live) == "stale"
    action, stop_pids, refused = _plan(**stale_live)
    assert action == "stale"
    assert stop_pids == []
    assert refused == []
    assert decide_demo_stop_action({"pid": 100}, **OWNED_FRONTEND_LIVE) == "stale"
    action, stop_pids, refused = _plan(identity={"pid": 100})
    assert action == "stale"
    assert stop_pids == []
    identity_ps1 = IDENTITY_PS1.read_text(encoding="utf-8")
    stop_index = identity_ps1.index("Stop-Process -Id")
    assert identity_ps1.index("Test-DemoPidOwned") < stop_index
    assert identity_ps1.index('$action -ne "stop"') < stop_index
    assert identity_ps1.index("Test-DemoDescendantOwned") < stop_index
    assert identity_ps1.index("Get-DemoOwnedTreePids") < stop_index
    assert identity_ps1.rindex("Remove-Item $PidFile") > stop_index


def test_refresh_never_removes_next_while_verified_frontend_descendants_alive() -> None:
    assert (
        refresh_allows_next_cache_clear(
            stop_completed=True,
            owned_frontend_descendants_alive=False,
            frontend_port_listening=False,
            backend_port_listening=False,
        )
        is True
    )
    assert (
        refresh_allows_next_cache_clear(
            stop_completed=True,
            owned_frontend_descendants_alive=True,
            frontend_port_listening=False,
            backend_port_listening=False,
        )
        is False
    )
    assert (
        refresh_allows_next_cache_clear(
            stop_completed=True,
            owned_frontend_descendants_alive=False,
            frontend_port_listening=True,
            backend_port_listening=False,
        )
        is False
    )
    assert (
        refresh_allows_next_cache_clear(
            stop_completed=False,
            owned_frontend_descendants_alive=False,
            frontend_port_listening=False,
            backend_port_listening=False,
        )
        is False
    )
    refresh_ps1 = REFRESH_PS1.read_text(encoding="utf-8")
    stop_idx = refresh_ps1.index("Stop-SportsHedge-Demo.ps1")
    descendant_idx = refresh_ps1.index("Verified frontend descendant PID")
    port_idx = refresh_ps1.index("Test-DemoPortListening -Port 3000")
    backend_port_idx = refresh_ps1.index("Test-DemoPortListening -Port 8000")
    git_fetch_idx = refresh_ps1.index('"fetch", "origin"')
    git_switch_idx = refresh_ps1.index('"switch", "owner-live"')
    git_pull_idx = refresh_ps1.index('"pull", "--ff-only"')
    remove_idx = refresh_ps1.index("Remove-Item -LiteralPath $NextDir")
    start_idx = refresh_ps1.index("Start-SportsHedge-Demo.ps1")
    assert stop_idx < descendant_idx < port_idx < backend_port_idx
    assert backend_port_idx < git_fetch_idx < git_switch_idx < git_pull_idx < remove_idx < start_idx
    remove_block = refresh_ps1.split("Remove-Item", 1)[1].split("Current branch", 1)[0]
    assert "node_modules" not in remove_block
    assert format_refresh_descendant_alive_message(103).replace("103", "$procId") in refresh_ps1
    assert format_refresh_port_occupied_message(port=3000, role="Frontend") in refresh_ps1
    assert format_refresh_port_occupied_message(port=8000, role="Backend") in refresh_ps1
    assert r"frontend\.next was not deleted" in refresh_ps1


def test_refresh_remains_paper_only_and_delegates_start() -> None:
    refresh_ps1 = REFRESH_PS1.read_text(encoding="utf-8")
    start_ps1 = START_PS1.read_text(encoding="utf-8")
    stop_ps1 = STOP_PS1.read_text(encoding="utf-8")
    identity_ps1 = IDENTITY_PS1.read_text(encoding="utf-8")
    refresh_bat = REFRESH_BAT.read_text(encoding="utf-8")

    assert "Refresh-SportsHedge-Demo.ps1" in refresh_bat
    assert "Demo-LauncherIdentity.ps1" in refresh_ps1
    assert "Stop-SportsHedge-Demo.ps1" in refresh_ps1
    assert "Start-SportsHedge-Demo.ps1" in refresh_ps1
    assert "Start-Process" not in refresh_ps1
    assert "uvicorn" not in refresh_ps1
    assert "npm.cmd" not in refresh_ps1
    assert "$env:SPORTS_HEDGE_EXECUTION_ENABLED" not in refresh_ps1
    assert "$env:PAPER_AUTOFILL_ENABLED" not in refresh_ps1
    assert "$env:PAPER_LIVE_REFRESH_ENABLED" not in refresh_ps1
    assert "place_order" not in refresh_ps1
    assert "MATCHBOOK_PASSWORD" not in refresh_ps1
    assert "taskkill" not in refresh_ps1.lower()
    assert "Get-Process node" not in refresh_ps1
    assert "Stop-Process -Name" not in refresh_ps1
    assert "Stop-Process -Name" not in identity_ps1
    assert "taskkill" not in identity_ps1.lower()
    assert "Get-Process node" not in identity_ps1
    assert "ParentProcessId" in identity_ps1
    assert "Get-DemoOwnedTreePids" in identity_ps1
    assert "Test-DemoDescendantOwned" in identity_ps1
    assert "Test-DemoPortListening" in identity_ps1
    assert "Wait-DemoPortGone" in identity_ps1
    assert "Stop-DemoPid" in stop_ps1
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in start_ps1
    assert "place_order" not in start_ps1
    assert "Current branch:" in refresh_ps1
    assert "Current SHA:" in refresh_ps1
    assert refresh_ps1.index("Current SHA:") > refresh_ps1.index("Remove-Item -LiteralPath $NextDir")


def test_scripts_encode_process_tree_contract() -> None:
    identity_ps1 = IDENTITY_PS1.read_text(encoding="utf-8")
    refresh_ps1 = REFRESH_PS1.read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "docs/DEMO_RUNBOOK.md").read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs/DEMO_READINESS.md").read_text(encoding="utf-8")
    assert "Win32_Process" in identity_ps1
    assert "ParentProcessId" in identity_ps1
    assert "unrelated process was not killed" in identity_ps1
    assert "Refusing to kill an unexpected occupant" in identity_ps1
    assert "Refresh-SportsHedge-Demo.ps1" in readme
    assert "Refresh-SportsHedge-Demo.ps1" in runbook
    assert "ParentProcessId" in docs or "verified descendant" in docs.lower()
    assert "owner-live" in refresh_ps1
    assert "--ff-only" in refresh_ps1
    assert r"frontend\.next" in refresh_ps1
