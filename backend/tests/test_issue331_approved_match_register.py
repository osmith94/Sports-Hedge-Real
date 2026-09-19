"""Issue #331: Approved Match Register + FTTS Priority Alerts bridge.

Runtime scanning consumes the versioned register. Confidence, learned market
labels, and mapping-review activation are not admission gates for registered
Matchbook↔Kalshi rows. Complete FTTS generalized arbs project onto the
ordinary Priority Alert path.

Deterministic fixture/demo providers. Not owner-live quotes. PAPER / read-only.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import inventory_is_comparable_opportunity
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.priority_alerts.models import FillConfidence
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import (
    CANCEL_RESCHEDULE_FAIR_PRICE,
    ET_RULES,
    REGULATION,
)
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    REGISTER_ADMITTED_REASON,
    REGISTER_ISSUE,
    REGISTER_PARENT_COMMIT,
    REGISTER_VERSION,
    canonical_key_for_market,
    registered_structural_match,
)
from sports_hedge.matching.markets import MarketMatcher
from test_issue316_catalogue_registry import (
    AWAY,
    HOME,
    MB_EVENT_ID,
    _DisabledPolymarket,
    _collect,
    _costs,
    _fx,
    _kalshi_btts_event,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _kalshi_total_event,
    _mb_event,
    _series,
)
from test_issue324_four_market_sibling_convergence import HotOverlapMatchbook
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue326_paper_four_families import LOCKED_FAMILIES, _incomplete_kalshi_events

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _arb_back(odds: str) -> dict[str, str]:
    return {"side": "back", "odds": odds, "available-amount": "1000"}


def _arb_runner(runner_id: int, name: str, odds: str = "4.20") -> dict[str, Any]:
    return {"id": runner_id, "name": name, "prices": [_arb_back(odds)]}


def _arb_mb_match_odds() -> dict[str, Any]:
    return {
        "id": 316010,
        "name": "Match Odds",
        "runners": [
            _arb_runner(1, HOME, "4.20"),
            _arb_runner(2, "Draw", "4.20"),
            _arb_runner(3, AWAY, "4.20"),
        ],
    }


def _arb_mb_btts() -> dict[str, Any]:
    return {
        "id": 316020,
        "name": "Both Teams To Score",
        "runners": [_arb_runner(11, "Yes", "2.20"), _arb_runner(12, "No", "2.20")],
    }


def _arb_mb_totals(line: str = "2.5") -> dict[str, Any]:
    return {
        "id": 316030,
        "name": f"Over/Under {line} Goals",
        "line": line,
        "runners": [
            _arb_runner(21, f"Over {line}", "2.20"),
            _arb_runner(22, f"Under {line}", "2.20"),
        ],
    }


def _arb_mb_ftts() -> dict[str, Any]:
    return {
        "id": 316040,
        "name": "First Team To Score",
        "runners": [
            _arb_runner(31, HOME, "3.30"),
            _arb_runner(32, AWAY, "3.30"),
            _arb_runner(33, "No Goal", "3.30"),
        ],
    }


def _arb_kalshi_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.22", "1000.00"]],
            "no_dollars": [["0.75", "1000.00"]],
        }
    }


def _arb_books() -> dict[str, dict[str, Any]]:
    tickers = [
        "KXEPLGAME-26SEP20BRECHE-BRE",
        "KXEPLGAME-26SEP20BRECHE-DRAW",
        "KXEPLGAME-26SEP20BRECHE-CHE",
        "KXEPLBTTS-26SEP20BRECHE-BTTS",
        "KXEPLTOTAL-26SEP20BRECHE-2.5",
        "KXEPLFTTS-26SEP20BRECHE-BRE",
        "KXEPLFTTS-26SEP20BRECHE-CHE",
        "KXEPLFTTS-26SEP20BRECHE-NG",
    ]
    return {ticker: deepcopy(_arb_kalshi_book()) for ticker in tickers}


def _alert_thresholds() -> PriorityAlertThresholds:
    return PriorityAlertThresholds(
        minimum_net_edge=Decimal("0.001"),
        minimum_expected_profit=Decimal("0.01"),
        minimum_executable_depth=Decimal("1"),
        maximum_quote_age_ms=60_000,
        maximum_execution_risk=100,
        minimum_depth_coverage=Decimal("0"),
        minimum_capital_efficiency=Decimal("0"),
        minimum_fill_confidence=FillConfidence.LOW,
    )


def _family_from_decision(decision) -> str | None:
    quotes = []
    if decision.depth_scan is not None:
        quotes = list(decision.depth_scan.selected_quotes)
    elif decision.payoff_scan is not None:
        quotes = list(decision.payoff_scan.selected_quotes)
    outcomes = {item.outcome for item in quotes}
    if outcomes == {"home", "draw", "away"}:
        return "match_result"
    if outcomes == {"yes", "no"}:
        return "both_teams_to_score"
    if outcomes == {"over", "under"}:
        return "total_goals"
    if outcomes == {"home", "away", "no_goal"}:
        return "first_team_to_score"
    return None


def test_register_version_and_canonical_keys() -> None:
    assert REGISTER_VERSION == "v1"
    assert REGISTER_ISSUE == 331
    assert REGISTER_PARENT_COMMIT == "8c0b31e5b593ce098866b38f930102aadffcccc8"
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_match_odds()])
    event = _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)
    kalshi = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLGAME"),
    )
    left = normalize_payload_side(mb)
    right = normalize_payload_side(kalshi)
    assert canonical_key_for_market(left) == CANONICAL_MATCH_RESULT_FT
    assert canonical_key_for_market(right) == CANONICAL_MATCH_RESULT_FT
    assert registered_structural_match(left, right) is True
    assessment = classify_payload_pair(mb, kalshi)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.state is not CatalogueApprovalState.REVIEW_REQUIRED
    assert REGISTER_ADMITTED_REASON in assessment.notes


def test_line_mismatch_is_not_the_same_register_key() -> None:
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_totals("2.5")])
    wrong = _kalshi_total_event("3.5")
    left = normalize_payload_side(mb)
    right = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=wrong,
            markets=list(wrong["markets"]),
            series=_series("KXEPLTOTAL"),
        )
    )
    assert canonical_key_for_market(left) == "TOTAL_GOALS_FT:2.5"
    assert canonical_key_for_market(right) == "TOTAL_GOALS_FT:3.5"
    assert registered_structural_match(left, right) is False
    assessment = classify_payload_pair(
        mb,
        PayloadSide(
            venue=VenueName.KALSHI,
            event=wrong,
            markets=list(wrong["markets"]),
            series=_series("KXEPLTOTAL"),
        ),
    )
    assert assessment.state is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH


def test_registered_pair_ignores_mapping_confidence_threshold() -> None:
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_btts()])
    event = _kalshi_btts_event()
    event["markets"][0]["rules_primary"] = "See contract URL."
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLBTTS"),
    )
    left_m = normalize_payload_side(mb).model_copy(update={"confidence": 0.1})
    right_m = normalize_payload_side(right).model_copy(update={"confidence": 0.1})
    assert canonical_key_for_market(left_m) == CANONICAL_BTTS_FT
    match = MarketMatcher().match(left_m, right_m)
    assert match.matched is True
    assert REGISTER_ADMITTED_REASON in match.reasons
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    decision = PaperScanService(intelligence).scan_pair(
        _observation_from_canonical(left_m),
        _observation_from_canonical(right_m),
        minimum_mapping_confidence=0.99,
        maximum_execution_risk=100,
        fx_snapshots=_fx(),
        venue_costs=_costs(),
    )
    assert "mapping_confidence_below_threshold" not in decision.rejection_reasons


def _observation_from_canonical(market):
    from sports_hedge.application.market_observation import OutcomeOrderBook, VenueMarketObservation

    books = [
        OutcomeOrderBook(outcome=runner.outcome, source_runner_id=runner.source_runner_id)
        for runner in market.runners
    ]
    return VenueMarketObservation(
        market=market,
        outcome_books=books,
        observed_at=NOW,
        quote_age_ms=50,
        native_currency="GBP" if market.source_venue is VenueName.MATCHBOOK else "USD",
        metadata={"quote_age_basis": "test"},
    )


def test_registered_scan_does_not_call_mapping_review(monkeypatch: pytest.MonkeyPatch) -> None:
    import sports_hedge.application.paper_scan as paper_scan_mod

    assert "evidence_from_markets" not in paper_scan_mod.__dict__

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("mapping review must not run for registered admission")

    monkeypatch.setattr(
        "sports_hedge.application.mapping_review.evidence_from_markets",
        boom,
    )
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_match_odds()])
    event = _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLGAME"),
    )
    left_m = normalize_payload_side(mb)
    right_m = normalize_payload_side(right)
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    decision = PaperScanService(intelligence).scan_pair(
        _observation_from_canonical(left_m),
        _observation_from_canonical(right_m),
        minimum_mapping_confidence=0.99,
        maximum_execution_risk=100,
        fx_snapshots=_fx(),
        venue_costs=_costs(),
    )
    assert decision.mapping_review_candidate is None
    assert REGISTER_ADMITTED_REASON in decision.market_match.reasons


@pytest.mark.asyncio
async def test_four_families_emit_independent_priority_alerts() -> None:
    matchbook = HotOverlapMatchbook(
        [_mb_event()],
        {
            str(MB_EVENT_ID): [
                _arb_mb_match_odds(),
                _arb_mb_btts(),
                _arb_mb_totals("2.5"),
                _arb_mb_ftts(),
            ]
        },
    )
    kalshi = OverlapKalshi(
        _incomplete_kalshi_events(),
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
            "KXEPLFTTS": _series("KXEPLFTTS"),
        },
        books=_arb_books(),
    )
    report = await _collect(matchbook, kalshi)
    fixture = next(
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )
    assert fixture.matched_equivalent_count == 4
    comparable = {
        row.family: row
        for row in report.fixture_markets[fixture.canonical_event_id]
        if inventory_is_comparable_opportunity(row.comparison_status)
    }
    assert set(comparable) == LOCKED_FAMILIES

    matched_decisions = [
        item
        for item in report.paper_decisions
        if item.market_match.matched and item.canonical_market_id
    ]
    by_family = {}
    for decision in matched_decisions:
        family = _family_from_decision(decision)
        if family is None:
            continue
        by_family[family] = decision
        assert REGISTER_ADMITTED_REASON in decision.market_match.reasons
        assert "mapping_confidence_below_threshold" not in decision.rejection_reasons
        assert decision.mapping_review_candidate is None
        assert decision.eligible_for_paper_simulation is True
        assert Settings().sports_hedge_execution_enabled is False
    assert set(by_family) == LOCKED_FAMILIES
    assert by_family["first_team_to_score"].payoff_scan is not None
    assert by_family["first_team_to_score"].payoff_scan.solution.is_arbitrage
    assert by_family["first_team_to_score"].solver_model == "generalized_payoff"
    assert by_family["first_team_to_score"].depth_scan is None

    ids = {decision.canonical_market_id for decision in by_family.values()}
    assert len(ids) == 4

    relationships = relationships_from_fixture_markets(report.fixture_markets)
    persisted = [item for group in relationships.values() for item in group]
    assert {item.family for item in persisted} == LOCKED_FAMILIES

    store = FixtureCurrentStateStore()
    store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=NOW)
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(NOW)
    assert unique == 1

    settings = Settings(max_slippage_bps=0, fx_spread_bps=0)
    assert settings.sports_hedge_execution_enabled is False
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=60_000)
    alerts = PriorityAlertService(thresholds=_alert_thresholds(), settings=settings)
    ops = PaperOperationsService(watchlist=watchlist, alerts=alerts, settings=settings)
    for decision in by_family.values():
        ops.persist_triggered_chain(decision, provenance=DataProvenance.FIXTURE_DEMO)

    current = alerts.current_alerts()
    alert_ids = {alert.canonical_market_id for alert in current}
    assert ids <= alert_ids
    assert len(alert_ids) >= 4
    assert by_family["first_team_to_score"].canonical_market_id in alert_ids
    assert by_family["match_result"].canonical_market_id in alert_ids
    assert by_family["both_teams_to_score"].canonical_market_id in alert_ids
    assert by_family["total_goals"].canonical_market_id in alert_ids

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
        matchbook.get_market_calls.clear()
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
    persisted_mb_ids = {
        str(item.matchbook.source_market_id)
        for item in persisted
        if item.matchbook is not None
    }
    assert {call[1] for call in matchbook.get_market_calls} <= persisted_mb_ids
    diagnostics = hot.scan_diagnostics.get("hot_targeted_refresh") or {}
    assert diagnostics.get("missing", 0) == 0


    unique, _lifecycle, _promoted = store.hot_membership_breakdown(NOW)
    assert unique == 1


@pytest.mark.asyncio
async def test_absent_ftts_emits_no_priority_alert_or_hot_row() -> None:
    matchbook = OverlapMatchbook(
        [_mb_event()],
        {str(MB_EVENT_ID): [_arb_mb_match_odds(), _arb_mb_btts(), _arb_mb_totals("2.5"), _arb_mb_ftts()]},
    )
    kalshi = OverlapKalshi(
        [
            _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE),
            _kalshi_btts_event(),
            _kalshi_total_event("2.5"),
        ],
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
        },
        books=_arb_books(),
    )
    report = await _collect(matchbook, kalshi)
    comparable = {
        row.family
        for rows in report.fixture_markets.values()
        for row in rows
        if inventory_is_comparable_opportunity(row.comparison_status)
    }
    assert "first_team_to_score" not in comparable
    matched = [item for item in report.paper_decisions if item.market_match.matched]
    assert all(_family_from_decision(item) != "first_team_to_score" for item in matched)
    settings = Settings(max_slippage_bps=0, fx_spread_bps=0)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=60_000)
    alerts = PriorityAlertService(thresholds=_alert_thresholds(), settings=settings)
    ops = PaperOperationsService(watchlist=watchlist, alerts=alerts, settings=settings)
    for decision in matched:
        if decision.canonical_market_id:
            ops.persist_triggered_chain(decision, provenance=DataProvenance.FIXTURE_DEMO)
    ftts_ids = {
        item.canonical_market_id
        for item in matched
        if _family_from_decision(item) == "first_team_to_score"
    }
    assert not ftts_ids
    assert all(alert.canonical_market_id not in ftts_ids for alert in alerts.current_alerts())
    relationships = [
        item
        for group in relationships_from_fixture_markets(report.fixture_markets).values()
        for item in group
    ]
    assert "first_team_to_score" not in {item.family for item in relationships}


def test_default_scanner_has_no_learned_market_label_applicator() -> None:
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    service = PaperScanService(intelligence)
    assert service.market_matcher.event_matcher.learned_applicator is None
    assert Settings().sports_hedge_execution_enabled is False
    ftts_left = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_ftts()])
    )
    event = _kalshi_ftts_event()
    for market in event["markets"]:
        market["rules_primary"] = "Winner of the match."
    ftts_right = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=event,
            markets=list(event["markets"]),
            series=_series("KXEPLFTTS"),
        )
    )
    assert canonical_key_for_market(ftts_left) == CANONICAL_FTTS_FT
    assert canonical_key_for_market(ftts_right) == CANONICAL_FTTS_FT
    assert registered_structural_match(ftts_left, ftts_right) is True
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_ftts()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=event,
            markets=list(event["markets"]),
            series=_series("KXEPLFTTS"),
        ),
    )
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.state is not CatalogueApprovalState.REVIEW_REQUIRED


def test_integer_total_is_outside_the_paper_register() -> None:
    mb = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_totals("2.0")])
    )
    assert canonical_key_for_market(mb) is None


def test_ftts_bridge_does_not_project_dnb_or_integer_totals() -> None:
    from sports_hedge.application.ftts_alert_bridge import project_ftts_payoff_to_ordinary_depth
    from sports_hedge.arbitrage.depth import DepthQuoteCandidate
    from sports_hedge.arbitrage.models import PayoffSolution
    from sports_hedge.arbitrage.payoff_scan import PayoffScanResult

    dnb = PayoffScanResult(
        solution=PayoffSolution(is_arbitrage=True, minimum_state_pnl=Decimal("1")),
        selected_quotes=[
            DepthQuoteCandidate(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="1",
                source_runner_id="1",
                gross_weighted_odds=Decimal("2.2"),
                net_decimal_odds=Decimal("2.2"),
                cumulative_depth=Decimal("100"),
                levels_consumed=1,
            ),
            DepthQuoteCandidate(
                outcome="away",
                venue=VenueName.KALSHI,
                source_market_id="2",
                source_runner_id="2",
                gross_weighted_odds=Decimal("2.2"),
                net_decimal_odds=Decimal("2.2"),
                cumulative_depth=Decimal("100"),
                levels_consumed=1,
            ),
        ],
        combinations_evaluated=1,
    )
    assert project_ftts_payoff_to_ordinary_depth(dnb) is None
    integer_total = dnb.model_copy(
        update={
            "selected_quotes": [
                quote.model_copy(update={"outcome": outcome})
                for quote, outcome in zip(dnb.selected_quotes, ("over", "under"), strict=True)
            ]
        }
    )
    assert project_ftts_payoff_to_ordinary_depth(integer_total) is None


def _registered_payload_pairs() -> list[tuple[str, PayloadSide, PayloadSide]]:
    game = _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)
    btts = _kalshi_btts_event()
    btts["markets"][0]["rules_primary"] = "See contract URL."
    total = _kalshi_total_event("2.5")
    total["markets"][0]["rules_primary"] = "See contract URL."
    ftts = _kalshi_ftts_event()
    for market in ftts["markets"]:
        market["rules_primary"] = "Winner of the match."
    return [
        (
            CANONICAL_MATCH_RESULT_FT,
            PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_match_odds()]),
            PayloadSide(
                venue=VenueName.KALSHI,
                event=game,
                markets=list(game["markets"]),
                series=_series("KXEPLGAME"),
            ),
        ),
        (
            CANONICAL_BTTS_FT,
            PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_btts()]),
            PayloadSide(
                venue=VenueName.KALSHI,
                event=btts,
                markets=list(btts["markets"]),
                series=_series("KXEPLBTTS"),
            ),
        ),
        (
            "TOTAL_GOALS_FT:2.5",
            PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_totals("2.5")]),
            PayloadSide(
                venue=VenueName.KALSHI,
                event=total,
                markets=list(total["markets"]),
                series=_series("KXEPLTOTAL"),
            ),
        ),
        (
            CANONICAL_FTTS_FT,
            PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_ftts()]),
            PayloadSide(
                venue=VenueName.KALSHI,
                event=ftts,
                markets=list(ftts["markets"]),
                series=_series("KXEPLFTTS"),
            ),
        ),
    ]


@pytest.mark.parametrize("expected_key,_left,_right", _registered_payload_pairs())
def test_settlement_prose_cannot_veto_registered_paper_admission(
    expected_key: str,
    _left: PayloadSide,
    _right: PayloadSide,
) -> None:
    left = normalize_payload_side(_left)
    right = normalize_payload_side(_right)
    assert canonical_key_for_market(left) == expected_key
    assert canonical_key_for_market(right) == expected_key
    baseline = MarketMatcher().match(left, right)
    assert baseline.matched is True
    mutated = right.model_copy(
        update={
            "settlement": right.settlement.model_copy(
                update={
                    "unknown_reason": "see_contract_url",
                    "postponement_rule": "fair-price-on-cancel",
                    "abandonment_rule": "void-or-fair-price",
                    "source_rule_version": "mutated-for-test",
                }
            )
        }
    )
    after = MarketMatcher().match(left, mutated)
    assert after.matched is True
    assert REGISTER_ADMITTED_REASON in after.reasons
    assert f"canonical_key={expected_key}" in after.reasons
    assessment = classify_payload_pair(_left, _right)
    assert assessment.paper_mode_admitted is True
    assert assessment.state in {
        CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT,
        CatalogueApprovalState.APPROVED_EQUIVALENT,
    }


def test_registered_key_and_structure_are_sufficient_after_fixture_identity() -> None:
    for expected_key, left_side, right_side in _registered_payload_pairs():
        left = normalize_payload_side(left_side)
        right = normalize_payload_side(right_side)
        match = MarketMatcher().match(left, right)
        assert match.matched is True, expected_key
        assert REGISTER_ADMITTED_REASON in match.reasons
        assert registered_structural_match(left, right) is True
        assert classify_payload_pair(left_side, right_side).paper_mode_admitted is True


def test_wrong_fixture_family_period_line_and_outcomes_fail_register_gate() -> None:
    mb = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_match_odds()])
    )
    game = _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)
    kalshi = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=game,
            markets=list(game["markets"]),
            series=_series("KXEPLGAME"),
        )
    )
    wrong_event = kalshi.model_copy(
        update={"event": kalshi.event.model_copy(update={"home_team": "Different FC"})}
    )
    fixture = MarketMatcher().match(mb, wrong_event)
    assert fixture.matched is False
    assert "event_mismatch" in fixture.reasons

    btts = _kalshi_btts_event()
    family = MarketMatcher().match(
        mb,
        normalize_payload_side(
            PayloadSide(
                venue=VenueName.KALSHI,
                event=btts,
                markets=list(btts["markets"]),
                series=_series("KXEPLBTTS"),
            )
        ),
    )
    assert family.matched is False
    assert "market_family_mismatch" in family.reasons

    from sports_hedge.domain.football import FootballPeriod

    half = kalshi.model_copy(update={"period": FootballPeriod.FIRST_HALF})
    period = MarketMatcher().match(mb, half)
    assert period.matched is False
    assert "period_mismatch" in period.reasons

    totals_left = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_totals("2.5")])
    )
    wrong_line = _kalshi_total_event("3.5")
    line = MarketMatcher().match(
        totals_left,
        normalize_payload_side(
            PayloadSide(
                venue=VenueName.KALSHI,
                event=wrong_line,
                markets=list(wrong_line["markets"]),
                series=_series("KXEPLTOTAL"),
            )
        ),
    )
    assert line.matched is False
    assert "line_mismatch" in line.reasons

    drop_draw = kalshi.model_copy(
        update={"runners": [runner for runner in kalshi.runners if runner.outcome.value != "draw"]}
    )
    outcomes = MarketMatcher().match(mb, drop_draw)
    assert outcomes.matched is False
    assert "outcome_space_mismatch" in outcomes.reasons


def test_extra_time_is_unregistered_archetype_not_fingerprint_veto() -> None:
    from sports_hedge.matching.approved_register import canonical_key_for_market

    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_arb_mb_match_odds()])
    et_event = _kalshi_game_event(rules=ET_RULES)
    et = PayloadSide(
        venue=VenueName.KALSHI,
        event=et_event,
        markets=list(et_event["markets"]),
        series=_series("KXEPLGAME"),
    )
    right = normalize_payload_side(et)
    assert canonical_key_for_market(right) is None
    match = MarketMatcher().match(normalize_payload_side(mb), right)
    assert match.matched is False
    assert "settlement_mismatch" not in match.reasons
    assert "incomplete_settlement" not in match.reasons
    assessment = classify_payload_pair(mb, et)
    assert assessment.state is CatalogueApprovalState.UNSUPPORTED
    assert assessment.paper_mode_admitted is False
    assert assessment.state is not CatalogueApprovalState.KNOWN_CONTRADICTION


def test_unregistered_pair_does_not_invoke_mapping_review_or_confidence_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sports_hedge.application.paper_scan as paper_scan_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("mapping review must not run for unregistered pairs")

    monkeypatch.setattr(
        "sports_hedge.application.mapping_review.evidence_from_markets",
        boom,
    )
    from sports_hedge.catalogue.corpus import census_corpus

    entry = next(item for item in census_corpus() if item.entry_id == "bad-1x2-mb-pm-unknown-settlement")
    left_m = normalize_payload_side(entry.left).model_copy(update={"confidence": 0.11})
    right_m = normalize_payload_side(entry.right).model_copy(update={"confidence": 0.11})
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    decision = PaperScanService(intelligence).scan_pair(
        _observation_from_canonical(left_m),
        _observation_from_canonical(right_m),
        minimum_mapping_confidence=0.99,
        maximum_execution_risk=100,
        fx_snapshots=_fx(),
        venue_costs=_costs(),
    )
    assert decision.market_match.matched is False
    assert "not_registered" in decision.market_match.reasons
    assert decision.mapping_review_candidate is None
    assert "mapping_confidence_below_threshold" not in decision.rejection_reasons
    assert "evidence_from_markets" not in paper_scan_mod.__dict__


def test_ftts_bridge_requires_register_identity() -> None:
    from sports_hedge.application.ftts_alert_bridge import (
        attach_ftts_ordinary_depth,
        ftts_register_admitted,
    )
    from sports_hedge.arbitrage.depth import DepthQuoteCandidate
    from sports_hedge.arbitrage.models import PayoffSolution
    from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
    from sports_hedge.matching.markets import MarketMatchResult
    from sports_hedge.paper.models import PaperScanDecision

    quotes = [
        DepthQuoteCandidate(
            outcome=outcome,
            venue=VenueName.MATCHBOOK,
            source_market_id="1",
            source_runner_id=str(index),
            gross_weighted_odds=Decimal("3.30"),
            net_decimal_odds=Decimal("3.30"),
            cumulative_depth=Decimal("100"),
            levels_consumed=1,
        )
        for index, outcome in enumerate(("home", "away", "no_goal"), start=1)
    ]
    payoff = PayoffScanResult(
        solution=PayoffSolution(is_arbitrage=True, minimum_state_pnl=Decimal("1")),
        selected_quotes=quotes,
        combinations_evaluated=1,
    )
    unregistered = PaperScanDecision(
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["teams_equivalent"]),
        payoff_scan=payoff,
        solver_model="generalized_payoff",
        eligible_for_paper_simulation=True,
    )
    assert ftts_register_admitted(unregistered) is False
    assert attach_ftts_ordinary_depth(unregistered).depth_scan is None

