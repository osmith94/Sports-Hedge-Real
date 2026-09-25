"""Behavioural tests for the PowerShell demo launcher's start/reuse/restart/refuse
decision. The real ``Start-SportsHedge-Demo.ps1`` and ``Demo-LauncherIdentity.ps1``
run under pwsh with OS listener, process, and HTTP calls stubbed.

Occupancy comes from the port listener plus PID/repo/Git identity. HTTP is only
post-start readiness (backend ``/health``, frontend ``/api/desktop/status``). The
operator homepage ``/`` is only the browser target: a slow or silent homepage must
never make an occupied port look free (the EADDRINUSE regression).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_SCRIPTS = REPO_ROOT / "scripts/windows"
STUBS = Path(__file__).resolve().parent / "fixtures/windows_launcher/launcher_stubs.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh is required to run the launcher")

CURRENT_SHA = "1111111111111111111111111111111111111111"
OLD_SHA = "2222222222222222222222222222222222222222"
BACKEND_HEALTH = "http://127.0.0.1:8000/health"
FRONTEND_STATUS = "http://127.0.0.1:3000/api/desktop/status"
HOMEPAGE_URLS = {"http://127.0.0.1:3000", "http://127.0.0.1:3000/", "http://localhost:3000/"}
BROWSER_TARGET = "http://127.0.0.1:3000/"

CMD_PATH = r"C:\Windows\System32\cmd.exe"
NODE_PATH = r"C:\nodejs\node.exe"
PYTHON_PATH = r"C:\Sports-Hedge\backend\.venv\Scripts\python.exe"
UNRELATED_PID = 7777
BACKEND_PID = 4000
FRONTEND_TREE = [5000, 5001, 5002]
FRONTEND_TOKENS = ["run", "dev", "127.0.0.1", "3000"]
BACKEND_TOKENS = ["uvicorn", "sports_hedge.api.main:app"]


def _processes(repo: str) -> list[dict]:
    return [
        {
            "pid": BACKEND_PID,
            "parent_pid": 1,
            "name": "python",
            "path": PYTHON_PATH,
            "command_line": f'"{PYTHON_PATH}" -m uvicorn sports_hedge.api.main:app --host 127.0.0.1 --port 8000',
        },
        {
            "pid": 5000,
            "parent_pid": 1,
            "name": "cmd",
            "path": CMD_PATH,
            "command_line": f'{CMD_PATH} /d /s /c "npm run dev -- -H 127.0.0.1 -p 3000"',
        },
        {
            "pid": 5001,
            "parent_pid": 5000,
            "name": "node",
            "path": NODE_PATH,
            "command_line": f'"{NODE_PATH}" "{repo}\\frontend\\node_modules\\next\\dist\\bin\\next" dev -H 127.0.0.1 -p 3000',
        },
        {
            "pid": 5002,
            "parent_pid": 5001,
            "name": "node",
            "path": NODE_PATH,
            "command_line": f'"{NODE_PATH}" "{repo}\\frontend\\node_modules\\next\\dist\\server\\lib\\start-server.js"',
        },
        {
            "pid": UNRELATED_PID,
            "parent_pid": 1,
            "name": "node",
            "path": NODE_PATH,
            "command_line": rf'"{NODE_PATH}" C:\other-project\server.js --port 3000',
        },
    ]


def _backend_identity(repo: str, sha: str = CURRENT_SHA) -> dict:
    return {
        "pid": BACKEND_PID,
        "path": PYTHON_PATH,
        "name": "python",
        "command_tokens": BACKEND_TOKENS,
        "git_head": sha,
        "repo_root": repo,
    }


def _frontend_identity(repo: str, sha: str = CURRENT_SHA, pid: int = 5000) -> dict:
    return {
        "pid": pid,
        "path": CMD_PATH,
        "name": "cmd",
        "command_tokens": FRONTEND_TOKENS,
        "git_head": sha,
        "repo_root": repo,
    }


@dataclass
class LauncherRun:
    returncode: int
    output: str
    events: list[tuple[str, str]]
    logs: Path

    def of(self, kind: str) -> list[str]:
        return [value for event_kind, value in self.events if event_kind == kind]

    @property
    def started(self) -> list[str]:
        return self.of("START")

    @property
    def stopped(self) -> list[int]:
        return [int(value) for value in self.of("STOP")]

    def frontend_started(self) -> bool:
        return any("npm.cmd run dev" in value for value in self.started)

    def backend_started(self) -> bool:
        return any("uvicorn sports_hedge.api.main:app" in value for value in self.started)

    def pid_identity(self, name: str) -> dict:
        return json.loads((self.logs / name).read_text(encoding="utf-8-sig"))


@pytest.fixture
def launcher(tmp_path: Path):
    root = tmp_path / "repo"
    scripts = root / "scripts/windows"
    scripts.mkdir(parents=True)
    (root / "frontend/node_modules").mkdir(parents=True)
    logs = root / "logs"
    logs.mkdir()
    shutil.copy(WINDOWS_SCRIPTS / "Start-SportsHedge-Demo.ps1", scripts)
    identity = (WINDOWS_SCRIPTS / "Demo-LauncherIdentity.ps1").read_text(encoding="utf-8")
    (scripts / "Demo-LauncherIdentity.ps1").write_text(
        identity + f"\n. '{STUBS}'\n", encoding="utf-8"
    )
    repo = str(root)

    def run(
        *,
        backend_listening: bool,
        frontend_listening: bool,
        backend_identity: dict | None = None,
        frontend_identity: dict | None = None,
        frontend_released_by: list[int] | None = None,
        http_ok: tuple[str, ...] = (BACKEND_HEALTH, FRONTEND_STATUS),
    ) -> LauncherRun:
        if backend_identity is not None:
            (logs / "demo-backend.pid").write_text(json.dumps(backend_identity), encoding="utf-8")
        if frontend_identity is not None:
            (logs / "demo-frontend.pid").write_text(json.dumps(frontend_identity), encoding="utf-8")
        scenario = {
            "git_sha": CURRENT_SHA,
            "processes": _processes(repo),
            "ports": {
                "8000": {"listening": backend_listening, "released_by": [BACKEND_PID]},
                "3000": {
                    "listening": frontend_listening,
                    "released_by": FRONTEND_TREE if frontend_released_by is None else frontend_released_by,
                },
            },
            "http_ok": list(http_ok),
        }
        scenario_path = tmp_path / "scenario.json"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        events_path = tmp_path / "events.tsv"
        events_path.write_text("", encoding="utf-8")
        proc = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(scripts / "Start-SportsHedge-Demo.ps1")],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "SH_LAUNCHER_SCENARIO": str(scenario_path),
                "SH_LAUNCHER_EVENTS": str(events_path),
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        events = [
            tuple(line.split("\t", 1))
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        result = LauncherRun(proc.returncode, proc.stdout + proc.stderr, events, logs)
        # Invariants for every scenario.
        assert not set(result.of("HTTP")) & HOMEPAGE_URLS, result.events
        assert UNRELATED_PID not in result.stopped, result.events
        assert set(result.of("PORTCHECK")) >= {"8000", "3000"} or result.returncode != 0
        for value in result.started:
            assert "SPORTS_HEDGE_MODE=paper" in value
            assert "SPORTS_HEDGE_EXECUTION_ENABLED=false" in value
        return result

    run.repo = repo
    return run


def test_a_free_ports_start_both_services(launcher) -> None:
    result = launcher(backend_listening=False, frontend_listening=False)
    assert result.returncode == 0, result.output
    assert result.backend_started() and result.frontend_started()
    assert result.stopped == []
    assert result.of("BROWSER") == [BROWSER_TARGET]
    assert result.pid_identity("demo-frontend.pid")["git_head"] == CURRENT_SHA
    assert result.pid_identity("demo-backend.pid")["git_head"] == CURRENT_SHA


def test_b_owned_current_sha_listeners_are_reused_without_starting(launcher) -> None:
    result = launcher(
        backend_listening=True,
        frontend_listening=True,
        backend_identity=_backend_identity(launcher.repo),
        frontend_identity=_frontend_identity(launcher.repo),
    )
    assert result.returncode == 0, result.output
    assert result.started == []
    assert result.stopped == []
    assert f"backend: reused existing Sports Hedge process for SHA {CURRENT_SHA}" in result.output
    assert f"frontend: reused existing Sports Hedge process for SHA {CURRENT_SHA}" in result.output
    assert result.of("BROWSER") == [BROWSER_TARGET]


def test_c_owned_old_sha_stops_only_owned_tree_waits_for_port_then_restarts(launcher) -> None:
    result = launcher(
        backend_listening=True,
        frontend_listening=True,
        backend_identity=_backend_identity(launcher.repo),
        frontend_identity=_frontend_identity(launcher.repo, sha=OLD_SHA),
    )
    assert result.returncode == 0, result.output
    assert sorted(result.stopped) == FRONTEND_TREE
    assert not result.backend_started()
    assert result.frontend_started()
    # Restart only after the listener on 3000 was observed gone.
    last_stop = max(i for i, (kind, _) in enumerate(result.events) if kind == "STOP")
    start_idx = next(i for i, (kind, _) in enumerate(result.events) if kind == "START")
    assert any(
        kind == "PORTCHECK" and value == "3000" for kind, value in result.events[last_stop:start_idx]
    )
    assert f"restarted owned process because recorded SHA {OLD_SHA} != current {CURRENT_SHA}" in result.output
    assert result.pid_identity("demo-frontend.pid")["git_head"] == CURRENT_SHA


def test_c_owned_old_sha_restart_refused_if_port_stays_occupied(launcher) -> None:
    result = launcher(
        backend_listening=True,
        frontend_listening=True,
        backend_identity=_backend_identity(launcher.repo),
        frontend_identity=_frontend_identity(launcher.repo, sha=OLD_SHA),
        frontend_released_by=[],
    )
    assert result.returncode == 1
    assert sorted(result.stopped) == FRONTEND_TREE
    assert not result.frontend_started()
    assert "port 3000 is still listening" in result.output
    assert result.of("BROWSER") == []


def test_d_unrelated_listener_without_pid_file_is_refused_not_killed(launcher) -> None:
    result = launcher(
        backend_listening=True,
        frontend_listening=True,
        backend_identity=_backend_identity(launcher.repo),
    )
    assert result.returncode == 1
    assert result.stopped == []
    assert not result.frontend_started()
    assert "frontend port 3000 is already in use but is not a Sports Hedge launcher process" in result.output
    assert "Refusing to reuse or kill the unrelated process occupying the port." in result.output
    assert result.of("BROWSER") == []


@pytest.mark.parametrize(
    "identity_kind",
    ["pid_reused_by_unrelated_process", "other_checkout"],
)
def test_d_unverified_owner_is_refused_not_killed(launcher, identity_kind: str) -> None:
    if identity_kind == "pid_reused_by_unrelated_process":
        frontend_identity = _frontend_identity(launcher.repo, pid=UNRELATED_PID)
    else:
        frontend_identity = {**_frontend_identity(launcher.repo), "repo_root": r"C:\Other-Clone"}
    result = launcher(
        backend_listening=True,
        frontend_listening=True,
        backend_identity=_backend_identity(launcher.repo),
        frontend_identity=frontend_identity,
    )
    assert result.returncode == 1
    assert result.stopped == []
    assert not result.frontend_started()
    assert "Refusing to reuse or kill" in result.output


def test_e_slow_homepage_listener_is_reused_and_no_second_next_is_started(launcher) -> None:
    """Regression: an owned current-SHA Next is listening on 3000 but ``/`` never
    answers. It must be reused, never treated as absent (EADDRINUSE)."""
    result = launcher(
        backend_listening=False,
        frontend_listening=True,
        frontend_identity=_frontend_identity(launcher.repo),
        http_ok=(BACKEND_HEALTH, FRONTEND_STATUS),
    )
    assert result.returncode == 0, result.output
    assert result.backend_started()
    assert not result.frontend_started()
    assert result.stopped == []
    assert f"frontend: reused existing Sports Hedge process for SHA {CURRENT_SHA}" in result.output
    assert FRONTEND_STATUS in result.of("HTTP")
    assert result.of("BROWSER") == [BROWSER_TARGET]


def test_f_frontend_status_failure_after_reuse_fails_readiness(launcher) -> None:
    result = launcher(
        backend_listening=True,
        frontend_listening=True,
        backend_identity=_backend_identity(launcher.repo),
        frontend_identity=_frontend_identity(launcher.repo),
        http_ok=(BACKEND_HEALTH,),
    )
    assert result.returncode == 1
    assert result.started == []
    assert f"Sports Hedge operator console did not become healthy at {FRONTEND_STATUS}" in result.output
    assert result.of("BROWSER") == []


def test_netstat_fallback_detects_listeners_when_get_nettcpconnection_is_unavailable(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        f". '{WINDOWS_SCRIPTS / 'Demo-LauncherIdentity.ps1'}'\n"
        + r"""
function Get-NetTCPConnection { throw "restricted host" }
function netstat {
    "  Proto  Local Address          Foreign Address        State           PID"
    "  TCP    127.0.0.1:3000         0.0.0.0:0              LISTENING       5002"
    "  TCP    [::]:8000              [::]:0                 LISTENING       4000"
    "  TCP    127.0.0.1:52000        127.0.0.1:5000         ESTABLISHED     12"
    "  TCP    127.0.0.1:30000        0.0.0.0:0              LISTENING       13"
}
foreach ($port in 3000, 8000, 5000, 300) {
    "$port=$(Test-DemoPortListening -Port $port)"
}
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(probe)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.split() == ["3000=True", "8000=True", "5000=False", "300=False"]


def test_g_backend_health_failure_after_start_fails_readiness(launcher) -> None:
    result = launcher(
        backend_listening=False,
        frontend_listening=False,
        http_ok=(FRONTEND_STATUS,),
    )
    assert result.returncode == 1
    assert result.backend_started() and result.frontend_started()
    assert f"Sports Hedge backend did not become healthy at {BACKEND_HEALTH}" in result.output
    assert result.of("BROWSER") == []
