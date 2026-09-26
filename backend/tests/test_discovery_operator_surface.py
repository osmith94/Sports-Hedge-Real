from __future__ import annotations

from pathlib import Path

from sports_hedge.application.target_competitions import UNMATCHED_POLYMARKET_COVERAGE

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

HEADERS = [
    "Fixture",
    "Kickoff",
    "Matchbook",
    "Polymarket",
    "Kalshi",
    "Equivalent",
    "Best arb market",
    "Net edge",
    "Edge vs trigger",
    "Risk",
    "Last refresh",
]


REMOVED_PRIMARY_HEADERS = ["Phase", "Venues", "State"]


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
    for header in REMOVED_PRIMARY_HEADERS:
        assert f'"{header}"' not in display
    assert "percent(item.current_net_edge)" in display
    assert "distance_to_trigger_pp" in display
    assert "const edgeVsTrigger = -parsed" in display
    assert "solver_is_arbitrage" in source or "arbClaimLabel" in display
    assert "1 / " not in source
    assert "kalshi_matched" in source
    assert "Recognition only" in source
    assert "Universe catalogue" in source
    assert "best_matchbook_price" not in source
    assert "best_polymarket_price" not in source
    assert "best_kalshi_price" not in source
    assert "current_net_edge" not in source
    assert "fixture found" in display
    assert "Not evaluated — scan budget exhausted" in display
    assert "market_evaluation_state" in display
    assert 'return "matched"' not in display
    assert "kickoffLocalLabel" in source
    assert "execution_risk_score" in display
    assert "toISOString" not in source
    assert "series_not_queried" not in source
    assert "current_net_edge" in display
    assert "fixtureHref" in source or "canonical_event_id" in source
    assert "/arbitrage/fixtures/" in source or "fixtureHref" in display
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "Demo walkthrough · not live operations" not in page
    assert "<GenerateMatchingReport" in page
    report = (FRONTEND / "components" / "generate-matching-report.tsx").read_text(encoding="utf-8")
    assert "Generate matching report" in report
    assert "does not start a scan" in report
    assert "liveConnected" in page
    assert "Newcastle" not in page
    assert "Start paper demo walkthrough" not in page
    sidebar = (FRONTEND / "components" / "sidebar.tsx").read_text(encoding="utf-8")
    assert "Operator demo" not in sidebar
    layout = (FRONTEND / "app" / "layout.tsx").read_text(encoding="utf-8")
    health = (FRONTEND / "components" / "venue-health-bar.tsx").read_text(encoding="utf-8")
    health_display = (FRONTEND / "lib" / "venue-health-display.ts").read_text(encoding="utf-8")
    assert "VenueHealthBar" in layout
    assert "getVenueHealth" in health
    assert "kalshi" in health
    assert "matchbook" in health
    assert "polymarket" in health
    assert 'scan === "timeout"' in health_display
    assert "${label} timeout" in health_display
    assert "off (operator)" in health_display
    main = (FRONTEND.parent / "backend" / "src" / "sports_hedge" / "api" / "main.py").read_text(
        encoding="utf-8"
    )
    assert '/venues/health' in main
    assert "health_timed_out_after_{timeout:g}s" in main
    assert "get_shared_provider_runtime" in main
    assert "runtime.kalshi" in main
    scan = (FRONTEND / "components" / "run-paper-scan.tsx").read_text(encoding="utf-8")
    assert "HOT target refresh" in scan
    assert "Refresh interval" not in scan
    assert "operator_summary" in scan
