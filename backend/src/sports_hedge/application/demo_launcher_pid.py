"""Ownership contract for Windows demo start/stop PID files.

The PowerShell start/stop launchers must apply the same rules:

- never force-kill a reused PID unless the live process command/path matches
  the identity written when the demo launcher started the backend or frontend;
- when an owned frontend wrapper (npm.cmd) is stopped, also stop verified
  descendants discovered via ParentProcessId for the same checkout;
- never broad-kill Node; unrelated processes are refused even if they share
  port 3000/8000;
- reuse a healthy process only when that owned identity also records this
  checkout's repo root and Git HEAD;
- if the owned process is healthy on a different HEAD, restart it;
- if a healthy port occupant is not this launcher's process, fail clearly
  rather than silently reuse or kill it;
- never delete frontend/.next while verified frontend descendants are alive
  or port 3000 is still listening.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

StopAction = Literal["stop", "stale", "missing"]
StartAction = Literal["start", "reuse", "restart", "conflict"]
DescendantAction = Literal["stop", "refuse", "missing"]

_WRAPPER_PROCESS_NAMES = frozenset({"cmd", "npm", "conhost", "powershell", "pwsh"})
_NODE_PROCESS_NAMES = frozenset({"node", "nodejs"})
_PYTHON_PROCESS_NAMES = frozenset({"python", "pythonw", "py"})
_FRONTEND_DESCENDANT_TOKENS = ("next", "frontend", "3000", "npm")
_BACKEND_DESCENDANT_TOKENS = ("uvicorn", "sports_hedge.api.main:app")


def _norm(value: str | None) -> str:
    return (value or "").replace("/", "\\").strip().lower()


def _norm_root(value: str | None) -> str:
    return _norm(value).rstrip("\\")


def decide_demo_stop_action(
    identity: dict[str, Any] | None,
    *,
    live_pid: int | None,
    live_name: str | None = None,
    live_path: str | None = None,
    live_command_line: str | None = None,
) -> StopAction:
    """Return whether Stop-Process is allowed for a demo PID identity."""

    if live_pid is None:
        return "missing"
    if not identity or identity.get("pid") is None:
        return "stale"
    try:
        expected_pid = int(identity["pid"])
    except (TypeError, ValueError):
        return "stale"
    if int(live_pid) != expected_pid:
        return "stale"
    tokens = [str(token) for token in (identity.get("command_tokens") or []) if str(token).strip()]
    if not tokens:
        # A bare numeric PID is not enough to prove ownership after PID reuse.
        return "stale"
    haystack = _norm(" ".join(part for part in (live_command_line, live_path, live_name) if part))
    if not all(_norm(token) in haystack for token in tokens):
        return "stale"
    expected_path = _norm(str(identity.get("path") or ""))
    actual_path = _norm(live_path)
    if expected_path and actual_path and expected_path != actual_path:
        return "stale"
    return "stop"


def decide_demo_start_action(
    identity: dict[str, Any] | None,
    *,
    health_ok: bool,
    current_git_head: str,
    current_repo_root: str,
    live_pid: int | None = None,
    live_name: str | None = None,
    live_path: str | None = None,
    live_command_line: str | None = None,
) -> StartAction:
    """Return whether a healthy demo process may be reused, restarted, or refused."""

    if not health_ok:
        return "start"
    current_sha = (current_git_head or "").strip()
    current_root = _norm_root(current_repo_root)
    if not current_sha or not current_root:
        return "conflict"
    owned = decide_demo_stop_action(
        identity,
        live_pid=live_pid,
        live_name=live_name,
        live_path=live_path,
        live_command_line=live_command_line,
    )
    if owned != "stop" or not identity:
        return "conflict"
    recorded_root = _norm_root(str(identity.get("repo_root") or ""))
    if recorded_root and recorded_root != current_root:
        return "conflict"
    recorded_sha = str(identity.get("git_head") or "").strip().lower()
    if recorded_sha and recorded_sha == current_sha.lower():
        return "reuse"
    return "restart"


def start_action_allows_owned_stop(action: StartAction) -> bool:
    """Only an owned SHA mismatch may Stop-Process. Conflicts never kill."""

    return action == "restart"


def format_checkout_report(*, branch: str, sha: str) -> tuple[str, str]:
    return (f"Current branch: {branch}", f"Current SHA: {sha}")


def format_service_disposition(
    label: str,
    action: StartAction,
    *,
    current_sha: str,
    recorded_sha: str | None = None,
) -> str:
    if action == "reuse":
        return f"{label}: reused existing Sports Hedge process for SHA {current_sha}"
    if action == "restart":
        return (
            f"{label}: restarted owned process because recorded SHA "
            f"{recorded_sha} != current {current_sha}"
        )
    if action == "start":
        return f"{label}: started for SHA {current_sha}"
    return (
        f"{label} is healthy but is not a Sports Hedge launcher process for this "
        f"checkout (SHA {current_sha}). Refusing to reuse or kill the unrelated "
        "process occupying the port."
    )


def _process_name(live_name: str | None) -> str:
    return (live_name or "").strip().lower().removesuffix(".exe")


def collect_owned_tree_pids(
    root_pid: int,
    processes: Sequence[Mapping[str, Any]],
) -> list[int]:
    """Return descendant PIDs deepest-first, then the owned root.

    `processes` entries use `pid` and `parent_pid` (Win32_Process.ParentProcessId).
    Cycles and missing snapshots are ignored; the root is always included.
    """

    children: dict[int, list[int]] = {}
    for proc in processes:
        try:
            pid = int(proc["pid"])
            ppid = int(proc.get("parent_pid") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if pid == ppid:
            continue
        children.setdefault(ppid, []).append(pid)
    ordered: list[int] = []
    visited: set[int] = set()
    stack: list[tuple[int, bool]] = [(int(root_pid), False)]
    while stack:
        pid, expanded = stack.pop()
        if expanded:
            ordered.append(pid)
            continue
        if pid in visited:
            continue
        visited.add(pid)
        stack.append((pid, True))
        for child in reversed(children.get(pid, ())):
            if child not in visited:
                stack.append((child, False))
    return ordered


def decide_descendant_stop_action(
    identity: dict[str, Any] | None,
    *,
    descendant_pid: int,
    tree_pids: set[int],
    label: str,
    live_name: str | None = None,
    live_path: str | None = None,
    live_command_line: str | None = None,
) -> DescendantAction:
    """Return whether a ParentProcessId descendant of an owned PID may be stopped.

    Tree membership is required. Frontend Node/Next descendants are additionally
    checked against the recorded repo root or Next.js command tokens when those
    values are available. Unrelated processes are refused.
    """

    try:
        pid = int(descendant_pid)
    except (TypeError, ValueError):
        return "refuse"
    if pid not in {int(item) for item in tree_pids}:
        return "refuse"
    if not any((live_name, live_path, live_command_line)):
        return "missing"
    name = _process_name(live_name)
    if name in _WRAPPER_PROCESS_NAMES:
        return "stop"
    haystack = _norm(" ".join(part for part in (live_command_line, live_path, live_name) if part))
    repo = _norm_root(str((identity or {}).get("repo_root") or ""))
    kind = (label or "").strip().lower()
    command_available = bool(_norm(live_command_line))
    if kind == "frontend":
        looks_node = name in _NODE_PROCESS_NAMES or "node" in haystack or "next" in haystack
        if not looks_node:
            return "refuse"
        if not command_available:
            return "stop"
        if repo and repo in haystack:
            return "stop"
        if any(token in haystack for token in _FRONTEND_DESCENDANT_TOKENS):
            return "stop"
        return "refuse"
    if kind == "backend":
        looks_python = (
            name in _PYTHON_PROCESS_NAMES or "python" in haystack or "uvicorn" in haystack
        )
        if not looks_python:
            return "refuse"
        if not command_available:
            return "stop"
        if repo and repo in haystack:
            return "stop"
        if any(token in haystack for token in _BACKEND_DESCENDANT_TOKENS):
            return "stop"
        return "refuse"
    return "refuse"


def plan_owned_process_tree_stop(
    identity: dict[str, Any] | None,
    *,
    processes: Sequence[Mapping[str, Any]],
    label: str,
    live_pid: int | None,
    live_name: str | None = None,
    live_path: str | None = None,
    live_command_line: str | None = None,
) -> tuple[StopAction, list[int], list[int]]:
    """Return (root_action, stop_pids deepest-first, refused_pids).

    Stop is allowed only after the recorded wrapper identity matches. Descendants
    are taken from the ParentProcessId tree of that owned PID.
    """

    root_action = decide_demo_stop_action(
        identity,
        live_pid=live_pid,
        live_name=live_name,
        live_path=live_path,
        live_command_line=live_command_line,
    )
    if root_action != "stop" or not identity:
        return root_action, [], []
    try:
        root_pid = int(identity["pid"])
    except (TypeError, ValueError, KeyError):
        return "stale", [], []
    tree = collect_owned_tree_pids(root_pid, processes)
    tree_set = set(tree)
    by_pid: dict[int, Mapping[str, Any]] = {}
    for proc in processes:
        try:
            by_pid[int(proc["pid"])] = proc
        except (TypeError, ValueError, KeyError):
            continue
    stop_pids: list[int] = []
    refused: list[int] = []
    for pid in tree:
        if pid == root_pid:
            stop_pids.append(pid)
            continue
        proc = by_pid.get(pid, {})
        decision = decide_descendant_stop_action(
            identity,
            descendant_pid=pid,
            tree_pids=tree_set,
            label=label,
            live_name=proc.get("name") if proc else None,
            live_path=proc.get("path") if proc else None,
            live_command_line=proc.get("command_line") if proc else None,
        )
        if decision == "stop":
            stop_pids.append(pid)
        elif decision == "refuse":
            refused.append(pid)
    return "stop", stop_pids, refused


def refresh_allows_next_cache_clear(
    *,
    stop_completed: bool,
    owned_frontend_descendants_alive: bool,
    frontend_port_listening: bool,
    backend_port_listening: bool,
) -> bool:
    """frontend/.next may be deleted only after owned stop and ports are gone."""

    return bool(
        stop_completed
        and not owned_frontend_descendants_alive
        and not frontend_port_listening
        and not backend_port_listening
    )


def format_refresh_port_occupied_message(*, port: int, role: str) -> str:
    return (
        f"{role} port {port} is still listening after stop. Refusing to delete "
        r"frontend\.next rather than killing an unexpected occupant of the port."
    )


def format_refresh_descendant_alive_message(pid: int) -> str:
    return (
        f"Verified frontend descendant PID {pid} is still running after stop. "
        r"Refusing to delete frontend\.next."
    )
