from __future__ import annotations

from pathlib import Path

from sports_hedge.application.target_competitions import UNMATCHED_POLYMARKET_COVERAGE

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

HEADERS = [
    "Fixture",
    "Kickoff",
    "Matchbook status",
    "Polymarket",
    "Matched markets",
    "Family / outcomes",
    "Best Matchbook",
    "Best Polymarket",
    "Net edge",
    "Trigger",
    "Distance",
    "Freshness",
    "Comparison",
    "Arb claim",
    "Score",
    "Last seen",
]


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _pp(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}pp"


def test_discovery_ui_renders_target_rows_from_backend_fields() -> None:
    epl = {
        "home_team": "Newcastle United",
        "away_team": "Chelsea",
        "matched_market_count": 1,
        "market_family": "both_teams_to_score",
        "outcome_context": "yes/no",
        "current_net_edge": 0.012,
        "trigger_net_edge": 0.005,
        "distance_to_trigger_pp": -0.7,
        "solver_is_arbitrage": True,
        "polymarket_matched": True,
        "no_comparison_reason": None,
    }
    championship = {
        "home_team": "Leeds United",
        "away_team": "Leicester City",
        "polymarket_matched": False,
        "no_comparison_reason": UNMATCHED_POLYMARKET_COVERAGE,
        "solver_is_arbitrage": False,
    }
    la_liga = {
        "home_team": "Real Madrid",
        "away_team": "Barcelona",
        "polymarket_matched": False,
        "no_comparison_reason": UNMATCHED_POLYMARKET_COVERAGE,
        "solver_is_arbitrage": False,
    }

    assert _percent(epl["current_net_edge"]) == "1.20%"
    assert _percent(epl["trigger_net_edge"]) == "0.50%"
    assert _pp(epl["distance_to_trigger_pp"]) == "-0.70pp"
    assert championship["no_comparison_reason"] == UNMATCHED_POLYMARKET_COVERAGE
    assert la_liga["polymarket_matched"] is False

    source = (FRONTEND / "components" / "discovered-fixtures.tsx").read_text(encoding="utf-8")
    display = (FRONTEND / "lib" / "discovered-fixture-display.ts").read_text(encoding="utf-8")
    combined = source + display
    for header in HEADERS:
        assert header in combined
    assert "percent(item.current_net_edge)" in source
    assert "percentPoints(item.distance_to_trigger_pp)" in source
    assert "solver_is_arbitrage" in source or "arbClaimLabel" in source
    assert "1 / " not in source
    display = (FRONTEND / "lib" / "discovered-fixture-display.ts").read_text(encoding="utf-8")
    assert "current_net_edge" in display
    assert "distance_to_trigger_pp" in display
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "Demo walkthrough · not live operations" in page
    assert "liveConnected" in page
    assert "Newcastle" in page
