"""Ownership contract for Windows demo start/stop PID files.

The PowerShell start/stop launchers must apply the same rules:

- never force-kill a reused PID unless the live process command/path matches
  the identity written when the demo launcher started the backend or frontend;
- reuse a healthy process only when that owned identity also records this
  checkout's repo root and Git HEAD;
- if the owned process is healthy on a different HEAD, restart it;
- if a healthy port occupant is not this launcher's process, fail clearly
  rather than silently reuse or kill it.
"""

from __future__ import annotations

from typing import Any, Literal

StopAction = Literal["stop", "stale", "missing"]
StartAction = Literal["start", "reuse", "restart", "conflict"]


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
