"""Issue #324: live four-market sibling convergence within the 5-minute window.

Owner-live observation: Kalshi GAME/BTTS/TOTAL/FTTS sibling events for the same
curated fixture often differ by a few minutes (for example Espanyol v Elche at
20:00 vs 20:03). EventMatcher declared a 5-minute kickoff tolerance but the
weighted kickoff penalty scored an exact same-team pair at +3 minutes ~0.88,
below the 0.92 threshold. HOT scheduling keyed the exact kickoff minute, so
the duplicate survived. TOTAL then surfaced as a Matchbook venue-only 0.5 row
instead of the exact 2.5 intersection.

This file uses deterministic fixture/demo providers. Not live venue quotes.
PAPER / read-only. Execution stays disabled. Fuzzy matching is not loosened.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from sports_hedge.application.collector import MarketEvaluationState, ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_clusters import VenueEvent, cluster_venue_events
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.hot_identity import hot_scheduling_key
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import GAMEWIN_TEMPLATE
from sports_hedge.catalogue.coverage_rows import fixture_catalogue_coverage
from sports_hedge.catalogue.registry import CatalogueCoverageState
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import (
    MB_EVENT_ID,
    _DisabledPolymarket,
    _all_books,
    _collect,
    _costs,
    _fx,
    _kalshi_btts_event,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _kalshi_total_event,
    _mb_btts,
    _mb_event,
    _mb_ftts,
    _mb_match_odds,
    _mb_totals,
    _series,
)


GAME_KICKOFF = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
BTTS_KICKOFF = GAME_KICKOFF + timedelta(minutes=1)
TOTAL_KICKOFF = GAME_KICKOFF + timedelta(minutes=3)
FTTS_KICKOFF = GAME_KICKOFF + timedelta(minutes=4)
OUTSIDE_KICKOFF = GAME_KICKOFF + timedelta(minutes=6)


def _canonical(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str,
    source_event_id: str,
    kickoff: datetime,
) -> CanonicalEvent:
    return CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )


def _venue_event(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str,
    source_event_id: str,
    kickoff: datetime,
) -> VenueEvent:
    canonical = _canonical(
        venue,
        home,
        away,
        competition=competition,
        source_event_id=source_event_id,
        kickoff=kickoff,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _stamp_kickoff(event: dict[str, Any], kickoff: datetime) -> dict[str, Any]:
    stamped = deepcopy(event)
    stamped["strike_date"] = kickoff.isoformat()
    milestone = stamped.get("milestone")
    if isinstance(milestone, dict):
        milestone["start_date"] = kickoff.isoformat()
    else:
        stamped["milestone"] = {"start_date": kickoff.isoformat()}
    return stamped


def _mb_event_at(kickoff: datetime) -> dict[str, Any]:
    event = _mb_event()
    event["start"] = kickoff.isoformat()
    return event


def test_paper_boundary_holds() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert not hasattr(MatchbookClient, "place_order")
    assert not hasattr(KalshiClient, "place_order")


def test_event_matcher_threshold_and_tolerance_unchanged() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance == timedelta(minutes=5)


def test_exact_curated_20_00_siblings_converge_inside_five_minute_window() -> None:
    matcher = EventMatcher()
    game = _canonical(
        VenueName.MATCHBOOK,
        "Espanyol",
        "Elche",
        competition="La Liga",
        source_event_id="mb-esp-elc",
        kickoff=GAME_KICKOFF,
    )
    total = _canonical(
        VenueName.KALSHI,
        "Espanyol Barcelona",
        "Elche CF",
        competition="La Liga",
        source_event_id="k-total",
        kickoff=TOTAL_KICKOFF,
    )
    ftts = _canonical(
        VenueName.KALSHI,
        "RCD Espanyol",
        "Elche",
        competition="La Liga",
        source_event_id="k-ftts",
        kickoff=FTTS_KICKOFF,
    )
    game_vs_total = matcher.match(game, total)
    assert game_vs_total.matched is True
    assert game_vs_total.confidence >= 0.92
    assert matcher.could_match(game, total) is True
    assert matcher.match(game, ftts).matched is True

    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Espanyol",
                "Elche CF",
                competition="La Liga",
                source_event_id="mb-esp-elc",
                kickoff=GAME_KICKOFF,
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Espanyol Barcelona",
                "Elche CF",
                competition="La Liga",
                source_event_id="KX-GAME",
                kickoff=GAME_KICKOFF,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Espanyol",
                "Elche",
                competition="La Liga",
                source_event_id="KX-BTTS",
                kickoff=BTTS_KICKOFF,
            ),
            _venue_event(
                VenueName.KALSHI,
                "RCD Espanyol Barcelona",
                "Elche CF",
                competition="La Liga",
                source_event_id="KX-TOTAL",
                kickoff=TOTAL_KICKOFF,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Espanyol",
                "Elche CF",
                competition="La Liga",
                source_event_id="KX-FTTS",
                kickoff=FTTS_KICKOFF,
            ),
        ],
        matcher=matcher,
        max_event_pairs=16,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.matchbook is not None
    assert {item.source_event_id for item in cluster.kalshi_events} == {
        "KX-GAME",
        "KX-BTTS",
        "KX-TOTAL",
        "KX-FTTS",
    }
    assert counts["matchbook_kalshi"] == 1


def test_hot_scheduling_collapses_minute_offset_siblings() -> None:
    left = SimpleNamespace(
        home_team="Espanyol",
        away_team="Elche CF",
        kickoff_utc=GAME_KICKOFF,
    )
    right = SimpleNamespace(
        home_team="Espanyol Barcelona",
        away_team="Elche",
        kickoff_utc=TOTAL_KICKOFF,
    )
    later = SimpleNamespace(
        home_team="Espanyol",
        away_team="Elche",
        kickoff_utc=OUTSIDE_KICKOFF,
    )
    assert hot_scheduling_key(left) is not None
    assert hot_scheduling_key(left) == hot_scheduling_key(right)
    assert hot_scheduling_key(left) != hot_scheduling_key(later)


def test_fail_closed_different_teams_competition_or_outside_tolerance() -> None:
    matcher = EventMatcher()
    base = _canonical(
        VenueName.MATCHBOOK,
        "Espanyol",
        "Elche",
        competition="La Liga",
        source_event_id="left",
        kickoff=GAME_KICKOFF,
    )
    other_team = matcher.match(
        base,
        _canonical(
            VenueName.KALSHI,
            "Espanyol",
            "Sevilla",
            competition="La Liga",
            source_event_id="right-team",
            kickoff=TOTAL_KICKOFF,
        ),
    )
    other_comp = matcher.match(
        base,
        _canonical(
            VenueName.KALSHI,
            "Espanyol",
            "Elche",
            competition="Premier League",
            source_event_id="right-comp",
            kickoff=TOTAL_KICKOFF,
        ),
    )
    outside = matcher.match(
        base,
        _canonical(
            VenueName.KALSHI,
            "Espanyol",
            "Elche",
            competition="La Liga",
            source_event_id="right-time",
            kickoff=OUTSIDE_KICKOFF,
        ),
    )
    youth = matcher.match(
        base,
        _canonical(
            VenueName.KALSHI,
            "Espanyol",
            "Elche U21",
            competition="La Liga",
            source_event_id="right-youth",
            kickoff=TOTAL_KICKOFF,
        ),
    )
    fuzzy = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Leeds United",
            "Chelsea",
            competition="Premier League",
            source_event_id="fuzzy-left",
            kickoff=GAME_KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Leeds Utd",
            "Chelsea",
            competition="Premier League",
            source_event_id="fuzzy-right",
            kickoff=TOTAL_KICKOFF,
        ),
    )
    unknown = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Unknownville",
            "Chelsea",
            competition="Premier League",
            source_event_id="unknown-left",
            kickoff=GAME_KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Unknownville",
            "Chelsea",
            competition="Premier League",
            source_event_id="unknown-right",
            kickoff=TOTAL_KICKOFF,
        ),
    )
    assert other_team.matched is False
    assert other_comp.matched is False
    assert outside.matched is False
    assert outside.reasons == ["kickoff_outside_tolerance"]
    assert youth.matched is False
    assert fuzzy.matched is False
    assert unknown.matched is False
    assert matcher.threshold == 0.92

    clusters, _ = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Espanyol",
                "Elche",
                competition="La Liga",
                source_event_id="mb-a",
                kickoff=GAME_KICKOFF,
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Espanyol",
                "Sevilla",
                competition="La Liga",
                source_event_id="k-b",
                kickoff=TOTAL_KICKOFF,
            )
        ],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 2


def test_gamewin_1x2_is_paper_assumed_across_three_minute_offset() -> None:
    mb_event = _mb_event_at(GAME_KICKOFF)
    kalshi_event = _stamp_kickoff(_kalshi_game_event(rules=GAMEWIN_TEMPLATE), TOTAL_KICKOFF)
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=kalshi_event,
            markets=list(kalshi_event["markets"]),
            series=_series("KXEPLGAME"),
        ),
    )
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.settlement_assumption == "regulation_time"
    assert assessment.execution_eligible is False
    assert assessment.paper_mode_admitted is True

    left = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()])
    )
    right = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=kalshi_event,
            markets=list(kalshi_event["markets"]),
            series=_series("KXEPLGAME"),
        )
    )
    match = MarketMatcher().match(left, right)
    assert match.matched is True
    assert "paper_assumed_equivalent" in match.reasons
    assert "settlement_assumption=regulation_time" in match.reasons


@pytest.mark.asyncio
async def test_offset_siblings_admit_four_families_and_hot_exact_ids() -> None:
    matchbook = OverlapMatchbook(
        [_mb_event_at(GAME_KICKOFF)],
        {str(MB_EVENT_ID): [_mb_match_odds(), _mb_btts(), _mb_totals("0.5"), _mb_totals("2.5"), _mb_ftts()]},
    )
    kalshi = OverlapKalshi(
        [
            _stamp_kickoff(_kalshi_game_event(rules=GAMEWIN_TEMPLATE), GAME_KICKOFF),
            _stamp_kickoff(_kalshi_btts_event(), BTTS_KICKOFF),
            _stamp_kickoff(_kalshi_total_event("2.5"), TOTAL_KICKOFF),
            _stamp_kickoff(_kalshi_ftts_event(), FTTS_KICKOFF),
        ],
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
            "KXEPLFTTS": _series("KXEPLFTTS"),
        },
        books=_all_books(),
    )
    report = await _collect(matchbook, kalshi)
    matched = [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(matched) == 1
    fixture = matched[0]
    assert fixture.matched_equivalent_count == 4
    source_ids = {
        str(row["source_event_id"])
        for row in report.fixture_source_events.get(fixture.canonical_event_id, [])
    }
    assert str(MB_EVENT_ID) in source_ids
    assert any("GAME" in item for item in source_ids)
    assert any("BTTS" in item for item in source_ids)
    assert any("TOTAL" in item for item in source_ids)
    assert any("FTTS" in item for item in source_ids)
    assert hot_scheduling_key(fixture) is not None

    rows = report.fixture_markets[fixture.canonical_event_id]
    comparable = {
        row.family: row
        for row in rows
        if row.comparison_status
        in {
            InventoryComparisonStatus.MATCHED_EQUIVALENT,
            InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
        }
    }
    assert comparable["match_result"].comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    assert comparable["both_teams_to_score"].comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert comparable["total_goals"].comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert comparable["total_goals"].line == Decimal("2.5")
    assert comparable["first_team_to_score"].comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    leftover_half = [
        row
        for row in rows
        if row.family == "total_goals" and str(row.line) == "0.5"
    ]
    assert leftover_half
    assert leftover_half[0].kalshi is None
    assert leftover_half[0].comparison_status is InventoryComparisonStatus.VENUE_ONLY

    coverage = fixture.catalogue_coverage or fixture_catalogue_coverage(
        rows,
        matchbook_matched=True,
        kalshi_matched=True,
        target_competition_code="premier_league",
    )
    by_label = {row.display_label: row for row in coverage.rows}
    assert by_label["1X2"].state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    assert "Kalshi settlement proof missing" not in by_label["1X2"].reason
    assert by_label["BTTS"].state is CatalogueCoverageState.APPROVED_EQUIVALENT
    assert by_label["FTTS"].state is CatalogueCoverageState.APPROVED_EQUIVALENT
    total_row = next(row for row in coverage.rows if row.display_label.startswith("TOTAL"))
    assert total_row.state is CatalogueCoverageState.APPROVED_EQUIVALENT
    assert total_row.line == "2.5" or str(total_row.line).startswith("2.5")

    relationships = relationships_from_fixture_markets(report.fixture_markets)
    persisted = [item for group in relationships.values() for item in group]
    families = {item.family for item in persisted}
    assert families == {
        "match_result",
        "both_teams_to_score",
        "total_goals",
        "first_team_to_score",
    }
    proofs = {item.family: item.proof_status for item in persisted}
    assert proofs["match_result"] == InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT.value
    assert proofs["both_teams_to_score"] == InventoryComparisonStatus.MATCHED_EQUIVALENT.value
    assert proofs["total_goals"] == InventoryComparisonStatus.MATCHED_EQUIVALENT.value
    assert proofs["first_team_to_score"] == InventoryComparisonStatus.MATCHED_EQUIVALENT.value

    store = FixtureCurrentStateStore()
    scanned = GAME_KICKOFF - timedelta(minutes=30)
    store.upsert_from_report(
        report,
        scan_lane=ScanLane.UNIVERSE,
        now=scanned,
    )
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(scanned)
    assert len(store._rows) == 1
    assert unique == 1

    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        matchbook.list_events_calls = 0
        matchbook.list_markets_calls.clear()
        kalshi.list_events_calls = 0
        kalshi.list_markets_calls.clear()
        kalshi.book_calls.clear()
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=report.fixture_source_events,
            hot_market_relationships=relationships,
            unbounded_cycle=True,
        )
    finally:
        repository.close()

    assert matchbook.list_events_calls == 0
    assert kalshi.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert kalshi.list_markets_calls == []
    persisted_kalshi_ids = {
        str(contract)
        for item in persisted
        if item.kalshi is not None
        for contract in (
            item.kalshi.constituent_contract_ids
            or ([item.kalshi.source_market_id] if item.kalshi.source_market_id else [])
        )
    }
    assert persisted_kalshi_ids
    assert kalshi.book_calls
    assert set(kalshi.book_calls) <= persisted_kalshi_ids
    hot_fixture = next(
        item for item in hot.discovered_fixtures if item.canonical_event_id == fixture.canonical_event_id
    )
    assert hot_fixture.market_evaluation_state == MarketEvaluationState.EVALUATED.value
    diagnostics = hot.scan_diagnostics.get("hot_targeted_refresh") or {}
    assert diagnostics.get("missing", 0) == 0


def test_championship_ftts_stays_explicitly_venue_unavailable() -> None:
    coverage = fixture_catalogue_coverage(
        [],
        matchbook_matched=True,
        kalshi_matched=True,
        target_competition_code="championship",
    )
    by_label = {row.display_label: row for row in coverage.rows}
    assert by_label["FTTS"].state is CatalogueCoverageState.VENUE_UNAVAILABLE
