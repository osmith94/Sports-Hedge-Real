"""Ownership contract for Windows demo start/stop PID files.

The PowerShell stop launcher must apply the same rule: never force-kill a
reused PID unless the live process command/path matches the identity written
when the demo launcher started the backend or frontend.
"""

from __future__ import annotations

from typing import Any, Literal

StopAction = Literal["stop", "stale", "missing"]


def _norm(value: str | None) -> str:
    return (value or "").replace("/", "\\").strip().lower()


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
