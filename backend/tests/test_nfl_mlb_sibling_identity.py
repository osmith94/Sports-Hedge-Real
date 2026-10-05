"""NFL/MLB sibling-series identity, catalogue fees, and PAPER settlement display.

Captured-shape fixtures only. No venue writes. PAPER / read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_nfl_paper_venue_fee_reuse import (
    _family_markets,
    _persist_pair,
)

from sports_hedge.application.approved_market_catalogue import CatalogueRowState
from sports_hedge.application.catalogue_maintenance import (
    FamilyDiscoveryCompleteness,
    complete_family_keys,
    family_key_from_kalshi_series,
    incomplete_families_for_failed_sibling_normalization,
)
from sports_hedge.application.fixture_inventory import (
    InventoryMarket,
    _resolve_inventory_cost,
    apply_durable_kalshi_fee_evidence,
    assemble_fixture_inventory,
)
from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.catalogue.admission import catalogue_allows_live_execution
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus
from sports_hedge.matching.approved_register import (
    CANONICAL_MATCH_RESULT_FT,
    registered_canonical_key,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.mlb.constants import (
    CANONICAL_MLB_GAME_WINNER,
    CANONICAL_MLB_TOTAL_RUNS,
    MLB_SETTLEMENT_NOT_EXECUTABLE,
)
from sports_hedge.mlb.normalize import (
    kalshi_mlb_event,
    kalshi_mlb_markets,
    matchbook_mlb_event,
    matchbook_mlb_market,
)
from sports_hedge.mlb.teams import (
    MLB_ABBREVIATIONS,
    mlb_away_home_from_event_ticker,
    resolve_mlb_team,
)
from sports_hedge.nfl.constants import (
    CANONICAL_NFL_GAME_WINNER,
    CANONICAL_NFL_POINT_SPREAD,
    CANONICAL_NFL_TOTAL_POINTS,
    NFL_NOT_LIVE_EXECUTION_REASON,
)
from sports_hedge.nfl.normalize import (
    kalshi_nfl_event,
    kalshi_nfl_markets,
    matchbook_nfl_event,
    matchbook_nfl_market,
)
from sports_hedge.nfl.teams import (
    NFL_ABBREVIATIONS,
    nfl_away_home_from_event_ticker,
    resolve_nfl_team,
)
from sports_hedge.normalization.venues import VenueNormalizationError
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

KICKOFF = "2026-09-27T17:00:00Z"
MLB_START = "2026-09-24T16:35:00Z"
KC = "kansas city chiefs"
MIA = "miami dolphins"
STL = "st louis cardinals"
PIT = "pittsburgh pirates"
KNOWN_FEE = {
    "fee_type": "quadratic_with_maker_fees",
    "fee_multiplier": "1",
    "fee_provenance": "series",
    "fee_resolution_status": "known",
    "snapshot_id": "kfee:test",
}


def _nfl_milestone(title: str) -> dict:
    return {
        "start_date": KICKOFF,
        "title": title,
        "details": {
            "league": "NFL",
            "away_team_id": "kc-uuid",
            "home_team_id": "mia-uuid",
        },
    }


def _nfl_game_payload() -> dict:
    return {
        "event_ticker": "KXNFLGAME-26SEP27KCMIA",
        "series_ticker": "KXNFLGAME",
        "title": "KC Chiefs vs MIA Dolphins",
        "milestone": _nfl_milestone("KC Chiefs at MIA Dolphins"),
        "markets": [
            {
                "ticker": "KXNFLGAME-26SEP27KCMIA-KC",
                "title": "KC Chiefs wins",
                "yes_sub_title": "KC Chiefs",
                "custom_strike": {"football_team": "kc-uuid"},
            },
            {
                "ticker": "KXNFLGAME-26SEP27KCMIA-MIA",
                "title": "MIA Dolphins wins",
                "yes_sub_title": "MIA Dolphins",
                "custom_strike": {"football_team": "mia-uuid"},
            },
        ],
    }


def _nfl_spread_payload(*, line: str = "3.5", ticker_suffix: str = "KC4") -> dict:
    return {
        "event_ticker": "KXNFLSPREAD-26SEP27KCMIA",
        "series_ticker": "KXNFLSPREAD",
        "title": "KC Chiefs vs MIA Dolphins: Spread",
        "milestone": _nfl_milestone("KC Chiefs at MIA Dolphins"),
        "markets": [
            {
                "ticker": f"KXNFLSPREAD-26SEP27KCMIA-{ticker_suffix}",
                "title": f"MIA Dolphins wins by over {line} points?",
                "yes_sub_title": f"MIA Dolphins wins by over {line} points",
                "custom_strike": {"football_team": "mia-uuid"},
            }
        ],
    }


def _nfl_total_payload(*, line: str = "44.5") -> dict:
    return {
        "event_ticker": "KXNFLTOTAL-26SEP27KCMIA",
        "series_ticker": "KXNFLTOTAL",
        "title": "KC Chiefs vs MIA Dolphins: Total Points",
        "milestone": _nfl_milestone("KC Chiefs at MIA Dolphins"),
        "markets": [
            {
                "ticker": "KXNFLTOTAL-26SEP27KCMIA-45",
                "title": f"Full Game: over {line} points scored?",
                "yes_sub_title": f"Over {line} points scored",
                "custom_strike": None,
            }
        ],
    }


def _mlb_milestone() -> dict:
    return {
        "start_date": MLB_START,
        "title": "STL Cardinals at PIT Pirates",
        "details": {
            "league": "MLB",
            "away_team_id": "stl-uuid",
            "home_team_id": "pit-uuid",
        },
    }


def _mlb_game_payload() -> dict:
    return {
        "event_ticker": "KXMLBGAME-26SEP241235STLPIT",
        "series_ticker": "KXMLBGAME",
        "title": "STL Cardinals vs PIT Pirates",
        "milestone": _mlb_milestone(),
        "markets": [
            {
                "ticker": "KXMLBGAME-26SEP241235STLPIT-STL",
                "yes_sub_title": "STL Cardinals wins",
                "custom_strike": {"baseball_team": "stl-uuid"},
            },
            {
                "ticker": "KXMLBGAME-26SEP241235STLPIT-PIT",
                "yes_sub_title": "PIT Pirates wins",
                "custom_strike": {"baseball_team": "pit-uuid"},
            },
        ],
    }


def _mlb_total_payload(*, line: str = "7.5") -> dict:
    return {
        "event_ticker": "KXMLBTOTAL-26SEP241235STLPIT",
        "series_ticker": "KXMLBTOTAL",
        "title": "STL Cardinals at PIT Pirates: Total Runs",
        "milestone": _mlb_milestone(),
        "markets": [
            {
                "ticker": f"KXMLBTOTAL-26SEP241235STLPIT-{line}",
                "title": f"Over {line} runs scored",
                "floor_strike": line,
                "custom_strike": None,
            }
        ],
    }


def _matchbook_nfl():
    event = matchbook_nfl_event(
        {
            "id": "mb-kcmia",
            "name": "Kansas City Chiefs at Miami Dolphins",
            "start": KICKOFF,
        }
    )
    winner = matchbook_nfl_market(
        event,
        {
            "id": "mb-ml",
            "name": "Moneyline",
            "market-type": "money_line",
            "runners": [
                {"id": "away", "name": "Kansas City Chiefs"},
                {"id": "home", "name": "Miami Dolphins"},
            ],
        },
    )
    spread = matchbook_nfl_market(
        event,
        {
            "id": "mb-spread-35",
            "name": "Handicap",
            "market-type": "handicap",
            "runners": [
                {"id": "away-s", "name": "Kansas City Chiefs +3.5", "handicap": "3.5"},
                {"id": "home-s", "name": "Miami Dolphins -3.5", "handicap": "-3.5"},
            ],
        },
    )
    other = matchbook_nfl_market(
        event,
        {
            "id": "mb-spread-45",
            "name": "Handicap",
            "market-type": "handicap",
            "runners": [
                {"id": "away-4", "name": "Kansas City Chiefs +4.5", "handicap": "4.5"},
                {"id": "home-4", "name": "Miami Dolphins -4.5", "handicap": "-4.5"},
            ],
        },
    )
    total = matchbook_nfl_market(
        event,
        {
            "id": "mb-total",
            "name": "Total",
            "market-type": "total",
            "runners": [
                {"id": "over", "name": "Over 44.5", "handicap": "44.5"},
                {"id": "under", "name": "Under 44.5", "handicap": "44.5"},
            ],
        },
    )
    other_total = matchbook_nfl_market(
        event,
        {
            "id": "mb-total-2",
            "name": "Total",
            "market-type": "total",
            "runners": [
                {"id": "over2", "name": "Over 47.5", "handicap": "47.5"},
                {"id": "under2", "name": "Under 47.5", "handicap": "47.5"},
            ],
        },
    )
    return winner, spread, other, total, other_total


def _matchbook_mlb(line: str = "7.5"):
    event = matchbook_mlb_event(
        {
            "id": "mb-stlpit",
            "name": "St. Louis Cardinals at Pittsburgh Pirates",
            "start": MLB_START,
        }
    )
    winner = matchbook_mlb_market(
        event,
        {
            "id": "mb-ml",
            "name": "Moneyline",
            "market-type": "money_line",
            "runners": [
                {"id": "stl", "name": "St. Louis Cardinals"},
                {"id": "pit", "name": "Pittsburgh Pirates"},
            ],
        },
    )
    total = matchbook_mlb_market(
        event,
        {
            "id": f"mb-total-{line}",
            "name": "Total",
            "market-type": "total",
            "handicap": line,
            "runners": [
                {"id": "over", "name": f"Over {line}"},
                {"id": "under", "name": f"Under {line}"},
            ],
        },
    )
    return winner, total


def test_nfl_abbreviations_resolve_deterministically() -> None:
    seen: set[str] = set()
    for abbreviation in NFL_ABBREVIATIONS:
        base = resolve_nfl_team(abbreviation)
        assert base.ok and base.canonical
        assert base.canonical not in seen
        seen.add(base.canonical)
        nickname = base.canonical.split()[-1]
        compound = resolve_nfl_team(f"{abbreviation} {nickname}")
        assert compound.canonical == base.canonical
        assert compound.abbreviation == abbreviation
    assert len(seen) == 32
    assert resolve_nfl_team("KC Chiefs").canonical == KC
    assert resolve_nfl_team("MIA Dolphins").canonical == MIA
    assert resolve_nfl_team("JAC Jaguars").abbreviation == "JAX"
    assert resolve_nfl_team("NY").ambiguous is True
    assert resolve_nfl_team("LA").ambiguous is True
    assert resolve_nfl_team("KC Dolphins").ok is False


def test_nfl_ticker_orientation_and_sibling_events_share_teams() -> None:
    assert nfl_away_home_from_event_ticker("KXNFLSPREAD-26SEP27KCMIA") == (KC, MIA)
    assert nfl_away_home_from_event_ticker("KXNFLGAME-26SEP20INDKC") == (
        "indianapolis colts",
        KC,
    )
    game = kalshi_nfl_event(_nfl_game_payload())
    spread_event = kalshi_nfl_event(_nfl_spread_payload())
    total_event = kalshi_nfl_event(_nfl_total_payload())
    for event in (game, spread_event, total_event):
        assert event.away_team == KC
        assert event.home_team == MIA
        assert event.kickoff_utc == datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
    spread = kalshi_nfl_markets(spread_event, _nfl_spread_payload()["markets"])[0]
    total = kalshi_nfl_markets(total_event, _nfl_total_payload()["markets"])[0]
    game_market = kalshi_nfl_markets(game, _nfl_game_payload()["markets"])[0]
    assert spread.line == Decimal("-3.5")
    assert total.line == Decimal("44.5")
    assert game_market.source_market_id == "KXNFLGAME-26SEP27KCMIA:game_winner"
    assert {runner.source_runner_id for runner in game_market.runners} == {
        "KXNFLGAME-26SEP27KCMIA-KC:YES",
        "KXNFLGAME-26SEP27KCMIA-MIA:YES",
    }


def test_nfl_exact_lines_and_catalogue_families() -> None:
    game = kalshi_nfl_markets(kalshi_nfl_event(_nfl_game_payload()), _nfl_game_payload()["markets"])[0]
    spread = kalshi_nfl_markets(
        kalshi_nfl_event(_nfl_spread_payload()), _nfl_spread_payload()["markets"]
    )[0]
    other_spread = kalshi_nfl_markets(
        kalshi_nfl_event(_nfl_spread_payload(line="4.5", ticker_suffix="KC5")),
        _nfl_spread_payload(line="4.5", ticker_suffix="KC5")["markets"],
    )[0]
    total = kalshi_nfl_markets(
        kalshi_nfl_event(_nfl_total_payload()), _nfl_total_payload()["markets"]
    )[0]
    other_total_event = _nfl_total_payload(line="47.5")
    other_total_event["markets"][0]["title"] = "Full Game: over 47.5 points scored?"
    other_total = kalshi_nfl_markets(kalshi_nfl_event(other_total_event), other_total_event["markets"])[0]
    winner, mb_spread, mb_other, mb_total, mb_other_total = _matchbook_nfl()
    matcher = MarketMatcher()
    assert matcher.match(spread, mb_spread).matched
    assert registered_canonical_key(spread, mb_spread) == f"{CANONICAL_NFL_POINT_SPREAD}:-3.5"
    assert matcher.match(spread, mb_other).matched is False
    assert registered_canonical_key(spread, mb_other) is None
    assert matcher.match(total, mb_total).matched
    assert registered_canonical_key(total, mb_total) == f"{CANONICAL_NFL_TOTAL_POINTS}:44.5"
    assert matcher.match(total, mb_other_total).matched is False
    keys = {
        registered_canonical_key(game, winner),
        registered_canonical_key(spread, mb_spread),
        registered_canonical_key(other_spread, mb_other),
        registered_canonical_key(total, mb_total),
        registered_canonical_key(other_total, mb_other_total),
    }
    assert keys == {
        CANONICAL_NFL_GAME_WINNER,
        f"{CANONICAL_NFL_POINT_SPREAD}:-3.5",
        f"{CANONICAL_NFL_POINT_SPREAD}:-4.5",
        f"{CANONICAL_NFL_TOTAL_POINTS}:44.5",
        f"{CANONICAL_NFL_TOTAL_POINTS}:47.5",
    }
    for left, right in ((game, winner), (spread, mb_spread), (total, mb_total)):
        assessment = classify_pair(left, right)
        assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
        assert catalogue_allows_live_execution(left, right) is True


def test_unsupported_nfl_props_and_periods_stay_rejected() -> None:
    with pytest.raises(VenueNormalizationError, match="unsupported NFL"):
        kalshi_nfl_event(
            {
                "event_ticker": "KXNFL1Q-26SEP27KCMIA",
                "series_ticker": "KXNFL1Q",
                "title": "KC Chiefs vs MIA Dolphins",
                "milestone": _nfl_milestone("KC Chiefs at MIA Dolphins"),
            }
        )
    event = kalshi_nfl_event(_nfl_game_payload())
    with pytest.raises(VenueNormalizationError, match="player props"):
        kalshi_nfl_markets(
            event,
            [{"ticker": "KXNFLGAME-26SEP27KCMIA-KC", "title": "Player passing yards"}],
        )
    with pytest.raises(VenueNormalizationError, match="period"):
        kalshi_nfl_markets(
            event,
            [{"ticker": "KXNFLGAME-26SEP27KCMIA-KC", "title": "1st quarter winner"}],
        )


def test_mlb_abbreviations_and_sibling_events_share_teams() -> None:
    seen: set[str] = set()
    for abbreviation in MLB_ABBREVIATIONS:
        base = resolve_mlb_team(abbreviation)
        assert base.ok and base.canonical
        seen.add(base.canonical)
    assert len(seen) == 30
    assert resolve_mlb_team("STL Cardinals").canonical == STL
    assert resolve_mlb_team("PIT Pirates").canonical == PIT
    assert resolve_mlb_team("BOS Red Sox").canonical == "boston red sox"
    assert resolve_mlb_team("CWS White Sox").canonical == "chicago white sox"
    assert resolve_mlb_team("CHW White Sox").canonical == "chicago white sox"
    assert resolve_mlb_team("TOR Blue Jays").canonical == "toronto blue jays"
    assert resolve_mlb_team("NYY Yankees").canonical == "new york yankees"
    assert resolve_mlb_team("NY").ambiguous is True
    assert mlb_away_home_from_event_ticker("KXMLBTOTAL-26SEP241235STLPIT") == (STL, PIT)
    game = kalshi_mlb_event(_mlb_game_payload())
    total_event = kalshi_mlb_event(_mlb_total_payload())
    assert (game.away_team, game.home_team) == (STL, PIT)
    assert (total_event.away_team, total_event.home_team) == (STL, PIT)
    assert game.scheduled_game_key == total_event.scheduled_game_key


def test_mlb_total_lines_catalogue_and_rejected_run_line() -> None:
    game = kalshi_mlb_markets(kalshi_mlb_event(_mlb_game_payload()), _mlb_game_payload()["markets"])[0]
    total = kalshi_mlb_markets(
        kalshi_mlb_event(_mlb_total_payload()),
        _mlb_total_payload()["markets"],
        event_payload=_mlb_total_payload(),
    )[0]
    other = kalshi_mlb_markets(
        kalshi_mlb_event(_mlb_total_payload(line="8.5")),
        _mlb_total_payload(line="8.5")["markets"],
        event_payload=_mlb_total_payload(line="8.5"),
    )[0]
    winner, mb_total = _matchbook_mlb("7.5")
    _, mb_other = _matchbook_mlb("8.5")
    assert MarketMatcher().match(total, mb_total).matched
    assert registered_canonical_key(total, mb_total) == f"{CANONICAL_MLB_TOTAL_RUNS}:7.5"
    assert registered_canonical_key(total, mb_other) is None
    assert registered_canonical_key(game, winner) == CANONICAL_MLB_GAME_WINNER
    assert registered_canonical_key(other, mb_other) == f"{CANONICAL_MLB_TOTAL_RUNS}:8.5"
    for left, right in ((game, winner), (total, mb_total)):
        assert classify_pair(left, right).state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
        assert catalogue_allows_live_execution(left, right) is True
    with pytest.raises(VenueNormalizationError, match="unsupported MLB"):
        kalshi_mlb_event(
            {
                "event_ticker": "KXMLBSPREAD-26SEP241235STLPIT",
                "series_ticker": "KXMLBSPREAD",
                "title": "STL Cardinals vs PIT Pirates run line",
                "milestone": _mlb_milestone(),
            }
        )
    event = kalshi_mlb_event(_mlb_total_payload())
    with pytest.raises(VenueNormalizationError, match="outside Stage-1"):
        kalshi_mlb_markets(
            event,
            [{"ticker": "KXMLBTOTAL-26SEP241235STLPIT-7", "title": "player prop home runs"}],
            event_payload=_mlb_total_payload(),
        )


def test_failed_sibling_normalization_stays_retryable_and_soccer_keys_unchanged() -> None:
    incomplete = incomplete_families_for_failed_sibling_normalization(
        ["KXNFLGAME-26SEP27KCMIA"],
        [
            "KXNFLSPREAD-26SEP27KCMIA",
            "KXNFLTOTAL-26SEP27KCMIA",
            "KXMLBTOTAL-26SEP241235OTHER",
            "KXEPLGAME-26SEP27TOTMCI",
        ],
    )
    assert incomplete == {CANONICAL_NFL_POINT_SPREAD, CANONICAL_NFL_TOTAL_POINTS}
    assert family_key_from_kalshi_series("KXEPLGAME") == CANONICAL_MATCH_RESULT_FT
    evidence = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=True,
        kalshi_series_results=(
            {"status": "ok", "series": "KXNFLGAME"},
            {"status": "ok", "series": "KXNFLSPREAD"},
            {"status": "timeout", "series": "KXNFLTOTAL"},
            {"status": "ok", "series": "KXEPLGAME"},
        ),
        kalshi_incomplete_family_keys=incomplete,
        target_competition_code="nfl",
    )
    complete = complete_family_keys(evidence)
    assert CANONICAL_NFL_GAME_WINNER in complete
    assert CANONICAL_NFL_POINT_SPREAD not in complete
    assert CANONICAL_NFL_TOTAL_POINTS not in complete
    assert CANONICAL_MATCH_RESULT_FT not in complete


def test_catalogue_native_ids_are_not_duplicated() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    key, kalshi, event_payload, series, polymarket, pm_raw = _family_markets("game_winner")
    _persist_pair(
        store,
        kalshi=kalshi,
        polymarket=polymarket,
        event_payload=event_payload,
        series=series,
        pm_payload=pm_raw,
    )
    _persist_pair(
        store,
        kalshi=kalshi,
        polymarket=polymarket,
        event_payload=event_payload,
        series=series,
        pm_payload=pm_raw,
        generation="g2",
    )
    rows = [row for row in store.list_rows_for_event("evt-indkc") if row.row_state is CatalogueRowState.ACTIVE]
    assert len(rows) == 1
    assert rows[0].register_canonical_key == key
    assert rows[0].kalshi_event_ticker == kalshi.event.source_event_id
    assert rows[0].kalshi_market_tickers
    assert rows[0].kalshi_fee_snapshot_id


def test_universe_only_kalshi_fee_uses_durable_snapshot_for_nfl_and_mlb() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    _key, kalshi, event_payload, series, polymarket, pm_raw = _family_markets("game_winner")
    _persist_pair(
        store,
        kalshi=kalshi,
        polymarket=polymarket,
        event_payload=event_payload,
        series=series,
        pm_payload=pm_raw,
    )
    item = InventoryMarket(
        venue=VenueName.KALSHI,
        source_event_id=kalshi.event.source_event_id,
        source_market_id=kalshi.source_market_id,
        raw_name="Game winner",
        canonical=kalshi,
    )
    apply_durable_kalshi_fee_evidence([item], catalogue_store=store, canonical_event_id="evt-indkc")
    snapshot, source = _resolve_inventory_cost(item, cost_resolver=None)
    assert snapshot is not None and snapshot.is_economically_known()
    assert item.durable_kalshi_fee is not None
    nfl_source = source
    rows = assemble_fixture_inventory([], [], kalshi_markets=[item])
    assert rows[0].kalshi is not None
    assert rows[0].kalshi.fee_status == "known"
    assert "nfl" not in (rows[0].kalshi.fee_source or "").casefold()
    assert "mlb" not in (rows[0].kalshi.fee_source or "").casefold()

    mlb_event = kalshi_mlb_event(_mlb_game_payload())
    mlb_market = kalshi_mlb_markets(mlb_event, _mlb_game_payload()["markets"])[0]
    mlb_item = InventoryMarket(
        venue=VenueName.KALSHI,
        source_event_id=mlb_market.event.source_event_id,
        source_market_id=mlb_market.source_market_id,
        raw_name="Game winner",
        canonical=mlb_market,
        durable_kalshi_fee=dict(KNOWN_FEE),
    )
    mlb_snapshot, mlb_source = _resolve_inventory_cost(mlb_item, cost_resolver=None)
    assert mlb_snapshot is not None and mlb_snapshot.is_economically_known()
    assert mlb_snapshot.formula_name == snapshot.formula_name
    assert "mlb" not in (mlb_source or "").casefold()
    assert nfl_source == source

    ambiguous = InventoryMarket(
        venue=VenueName.KALSHI,
        source_event_id=kalshi.event.source_event_id,
        source_market_id=kalshi.source_market_id,
        raw_name="Game winner",
        canonical=kalshi,
        durable_kalshi_fee={
            "fee_provenance": "event_override",
            "fee_resolution_error": "partial_fee_override",
            "fee_resolution_status": "unknown",
        },
    )
    unknown, _unknown_source = _resolve_inventory_cost(ambiguous, cost_resolver=None)
    assert unknown is not None
    assert unknown.known_status is CostKnownStatus.UNKNOWN

    missing = InventoryMarket(
        venue=VenueName.KALSHI,
        source_event_id=kalshi.event.source_event_id,
        source_market_id=kalshi.source_market_id,
        raw_name="Game winner",
        canonical=kalshi,
    )
    absent, absent_source = _resolve_inventory_cost(missing, cost_resolver=None)
    assert absent is None
    assert absent_source == "unknown_required_venue_cost:kalshi"

    priced = item.model_copy(deep=True)
    priced.observation = VenueMarketObservation(
        market=kalshi,
        observed_at=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
        native_currency="USD",
        outcome_books=[],
        metadata={"kalshi_fee": dict(item.durable_kalshi_fee or {})},
    )
    priced_snapshot, priced_source = _resolve_inventory_cost(priced, cost_resolver=None)
    assert priced_snapshot is not None and priced_snapshot.is_economically_known()
    assert priced_snapshot.formula_name == snapshot.formula_name
    assert priced_source == source


def test_approved_nfl_and_mlb_settlement_is_paper_assumed_not_unknown() -> None:
    game = kalshi_nfl_markets(kalshi_nfl_event(_nfl_game_payload()), _nfl_game_payload()["markets"])[0]
    spread = kalshi_nfl_markets(
        kalshi_nfl_event(_nfl_spread_payload()), _nfl_spread_payload()["markets"]
    )[0]
    total = kalshi_nfl_markets(
        kalshi_nfl_event(_nfl_total_payload()), _nfl_total_payload()["markets"]
    )[0]
    mlb_game = kalshi_mlb_markets(kalshi_mlb_event(_mlb_game_payload()), _mlb_game_payload()["markets"])[0]
    mlb_total = kalshi_mlb_markets(
        kalshi_mlb_event(_mlb_total_payload()),
        _mlb_total_payload()["markets"],
        event_payload=_mlb_total_payload(),
    )[0]
    expected = {
        id(game): NFL_NOT_LIVE_EXECUTION_REASON,
        id(spread): NFL_NOT_LIVE_EXECUTION_REASON,
        id(total): NFL_NOT_LIVE_EXECUTION_REASON,
        id(mlb_game): MLB_SETTLEMENT_NOT_EXECUTABLE,
        id(mlb_total): MLB_SETTLEMENT_NOT_EXECUTABLE,
    }
    for market, provenance in (
        (game, game.settlement.unknown_reason),
        (spread, spread.settlement.unknown_reason),
        (total, total.settlement.unknown_reason),
        (mlb_game, MLB_SETTLEMENT_NOT_EXECUTABLE),
        (mlb_total, MLB_SETTLEMENT_NOT_EXECUTABLE),
    ):
        item = InventoryMarket(
            venue=market.source_venue,
            source_event_id=market.event.source_event_id,
            source_market_id=market.source_market_id,
            raw_name=market.family.value,
            canonical=market,
        )
        rows = assemble_fixture_inventory(
            [],
            [],
            kalshi_markets=[item] if market.source_venue is VenueName.KALSHI else None,
        )
        facts = rows[0].kalshi
        assert facts is not None
        assert facts.settlement_complete is False
        assert facts.settlement_status == "paper_assumed"
        assert facts.settlement_provenance == provenance
        assert expected[id(market)]
    unsupported = game.model_copy(update={"family": MarketFamily.TEAM_TOTAL})
    item = InventoryMarket(
        venue=VenueName.KALSHI,
        source_event_id=unsupported.event.source_event_id,
        source_market_id=unsupported.source_market_id,
        raw_name="team total",
        canonical=unsupported,
    )
    facts = assemble_fixture_inventory([], [], kalshi_markets=[item])[0].kalshi
    assert facts is not None
    assert facts.settlement_status == "incomplete"
