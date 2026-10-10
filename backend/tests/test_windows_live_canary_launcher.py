"""Wave 40 uses the existing Real launcher plus a dashboard toggle.

There is no separate live-canary Windows application.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS = REPO_ROOT / "scripts" / "windows"
REAL_START = WINDOWS / "Start-SportsHedge-Real.ps1"
PROVIDER = REPO_ROOT / "frontend" / "components" / "runtime-mode-provider.tsx"
LAYOUT = REPO_ROOT / "frontend" / "app" / "layout.tsx"


def test_real_launcher_still_forces_execution_off() -> None:
    script = REAL_START.read_text(encoding="utf-8")
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in script
    assert '$env:PAPER_AUTOFILL_ENABLED = "false"' in script
    assert "SPORTS_HEDGE_LIVE_CANARY_ENABLED" not in script
    assert "Start-SportsHedge-LiveCanary" not in script


def test_no_separate_live_canary_launcher() -> None:
    names = {path.name for path in WINDOWS.glob("*LiveCanary*")}
    assert names == set()


def test_top_right_execution_toggle_exists() -> None:
    provider = PROVIDER.read_text(encoding="utf-8")
    layout = LAYOUT.read_text(encoding="utf-8")
    assert "RuntimeModeClock" in layout
    assert "EXECUTION ON" in provider
    assert "EXECUTION OFF" in provider
    assert "CHECKING" in provider
    assert "ARM LIVE CANARY" in provider
    assert "armLiveCanary" in provider
    assert "disarmLiveCanary" in provider
    assert "does not cancel" in provider
