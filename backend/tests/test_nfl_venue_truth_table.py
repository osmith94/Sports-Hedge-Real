"""Issue #590: NFL venue truth table. Generic Matchbook ``total`` is not game total.

Observed false positive: Matchbook ``Colston Loveland - Total Receiving Yards``
(market-type=total, 32.5) was treated as ``NFL_TOTAL_POINTS_FT:32.5`` and
PAPER-matched Kalshi ``KXNFLTOTAL`` game total 32.5.

Data class: synthetic shapes matching captured 2026-09-28 PHI@CHI failure mode,
plus captured IND@KC Stage 1B fixtures. Not live quotes.
PAPER / read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_nfl_stage1b_paper_markets import (
    _clone_mb_total,
    _kalshi_total_event_and_market,
    _mb_indkc,
    _mb_market,
    _normalize_kalshi_game,
    _normalize_kalshi_spread,
    _normalize_kalshi_total,
    _normalize_mb_family,
    _normalize_pm_family,
    _pm_indkc,
)

from sports_hedge.application.catalogue_maintenance import pair_identity_from_markets
from sports_hedge.application.market_observation import MatchbookObservationBuilder
from sports_hedge.application.price_engine import (
    RetrievedVenuePayload,
    _canonical_matchbook_market,
    _family_from_key,
)
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.matching.approved_register import canonical_key_for_market, registered_canonical_key
from sports_hedge.nfl.constants import CANONICAL_NFL_GAME_WINNER, CANONICAL_NFL_TOTAL_POINTS
from sports_hedge.nfl.venue_mapping import (
    NFL_VENUE_MAPPING_VERSION,
    classify_kalshi_nfl_market,
    classify_matchbook_nfl_market,
    classify_polymarket_nfl_market,
    matchbook_payload_matches_nfl_family,
)
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
    _matchbook_market_family,
)

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)
PHI_CHI_KICKOFF = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)


def _phi_chi_event_payload() -> dict:
    return {
        "id": "mb-phichi-590",
        "name": "Philadelphia Eagles at Chicago Bears",
        "start": PHI_CHI_KICKOFF.isoformat().replace("+00:00", "Z"),
        "sport-id": 1,
        "meta-tags": [
            {"name": "American Football", "type": "SPORT"},
            {"name": "NFL", "type": "COMPETITION"},
        ],
    }


def _over_under_runners(line: str, *, prefix: str = "r") -> list[dict]:
    return [
        {"id": f"{prefix}-over", "name": f"OVER {line}", "handicap": float(line)},
        {"id": f"{prefix}-under", "name": f"UNDER {line}", "handicap": float(line)},
    ]


def _loveland_receiving_yards_payload() -> dict:
    return {
        "id": "mb-loveland-recv-32-5",
        "name": "Colston Loveland - Total Receiving Yards",
        "market-type": "total",
        "type": "binary",
        "handicap": 32.5,
        "runners": _over_under_runners("32.5", prefix="loveland"),
    }


def _kalshi_phi_chi_total_32_5() -> tuple[dict, dict]:
    event, market = _kalshi_total_event_and_market()
    event = {
        **event,
        "event_ticker": "KXNFLTOTAL-26SEP28PHICHI",
        "series_ticker": "KXNFLTOTAL",
        "title": "Philadelphia vs Chicago: Total Points",
    }
    market = {
        **market,
        "ticker": "KXNFLTOTAL-26SEP28PHICHI-33",
        "event_ticker": "KXNFLTOTAL-26SEP28PHICHI",
        "title": "Full Game: over 32.5 points scored?",
        "yes_sub_title": "Over 32.5 points scored",
    }
    return event, market


def test_truth_table_version_is_stamped_on_approved_markets() -> None:
    _, mb = _normalize_mb_family(_mb_market(_mb_indkc(), name="Total", handicap=45.5))
    assert mb.settlement.source_rule_version == NFL_VENUE_MAPPING_VERSION
    _, kalshi = _normalize_kalshi_total()
    assert kalshi.settlement.source_rule_version == NFL_VENUE_MAPPING_VERSION
    _, pm, _ = _normalize_pm_family("totals")
    assert pm.settlement.source_rule_version == NFL_VENUE_MAPPING_VERSION


def test_generic_matchbook_total_type_is_not_game_total() -> None:
    assert classify_matchbook_nfl_market(_loveland_receiving_yards_payload()) is None
    event = MatchbookNormalizer().normalize_event(_phi_chi_event_payload())
    with pytest.raises(VenueNormalizationError, match="truth table"):
        MatchbookNormalizer().normalize_market(event, _loveland_receiving_yards_payload())


def test_colston_loveland_cannot_produce_nfl_total_points_key() -> None:
    event = MatchbookNormalizer().normalize_event(_phi_chi_event_payload())
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_market(event, _loveland_receiving_yards_payload())


def test_kalshi_game_total_32_5_does_not_pair_with_player_prop() -> None:
    event_payload, market_payload = _kalshi_phi_chi_total_32_5()
    kalshi_event = KalshiNormalizer().normalize_event(event_payload)
    kalshi = KalshiNormalizer().assemble_canonical_markets(
        kalshi_event, [market_payload], event_payload=event_payload
    )[0]
    assert kalshi.family is MarketFamily.TOTAL_POINTS
    assert canonical_key_for_market(kalshi) == f"{CANONICAL_NFL_TOTAL_POINTS}:32.5"
    mb_event = MatchbookNormalizer().normalize_event(_phi_chi_event_payload())
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_market(mb_event, _loveland_receiving_yards_payload())
    assert pair_identity_from_markets(kalshi, kalshi) is None


@pytest.mark.parametrize(
    "name",
    [
        "Total Receiving Yards",
        "Total Rushing Yards",
        "Total Passing Yards",
        "Receptions",
        "Saquon Barkley - Total",
        "Team Total",
        "1st Half Total",
        "1st Quarter Total",
        "First Half Total Points",
        "Winning Margin",
        "Anytime Touchdown",
    ],
)
def test_adversarial_matchbook_totals_are_unsupported(name: str) -> None:
    payload = {
        "id": f"mb-bad-{name}",
        "name": name,
        "market-type": "total",
        "runners": _over_under_runners("32.5"),
    }
    assert classify_matchbook_nfl_market(payload) is None
    event = MatchbookNormalizer().normalize_event(_phi_chi_event_payload())
    with pytest.raises(VenueNormalizationError, match="truth table"):
        MatchbookNormalizer().normalize_market(event, payload)


def test_generic_total_type_without_approved_name_never_maps() -> None:
    payload = {
        "id": "mb-generic-total",
        "name": "Something else",
        "market-type": "total",
        "runners": _over_under_runners("47.5"),
    }
    assert classify_matchbook_nfl_market(payload) is None


def test_approved_matchbook_game_total_still_maps() -> None:
    payload = _mb_market(_mb_indkc(), name="Total", handicap=45.5)
    hit = classify_matchbook_nfl_market(payload)
    assert hit is not None
    assert hit.family is MarketFamily.TOTAL_POINTS
    assert hit.native_archetype == "matchbook_total_points_full_game"
    _, market = _normalize_mb_family(payload)
    assert market.family is MarketFamily.TOTAL_POINTS
    assert canonical_key_for_market(market) == f"{CANONICAL_NFL_TOTAL_POINTS}:45.5"


def test_approved_game_winner_and_spread_still_map() -> None:
    _, kalshi_game = _normalize_kalshi_game()
    _, mb_ml = _normalize_mb_family(_mb_market(_mb_indkc(), name="Moneyline"))
    _, pm_ml, _ = _normalize_pm_family("moneyline")
    assert classify_matchbook_nfl_market(_mb_market(_mb_indkc(), name="Moneyline")).family is (
        MarketFamily.GAME_WINNER
    )
    assert canonical_key_for_market(kalshi_game) == CANONICAL_NFL_GAME_WINNER
    assert canonical_key_for_market(mb_ml) == CANONICAL_NFL_GAME_WINNER
    assert canonical_key_for_market(pm_ml) == CANONICAL_NFL_GAME_WINNER
    assert registered_canonical_key(kalshi_game, mb_ml) == CANONICAL_NFL_GAME_WINNER

    _, kalshi_spread = _normalize_kalshi_spread()
    assert kalshi_spread.family is MarketFamily.POINT_SPREAD
    _, mb_hc = _normalize_mb_family(_mb_market(_mb_indkc(), name="Handicap", handicap=4.5))
    assert mb_hc.family is MarketFamily.POINT_SPREAD
    _, pm_sp, _ = _normalize_pm_family("spreads")
    assert pm_sp.family is MarketFamily.POINT_SPREAD


def test_kalshi_and_polymarket_truth_table_positive_and_reject() -> None:
    event_payload, market_payload = _kalshi_total_event_and_market()
    hit = classify_kalshi_nfl_market(market_payload, series_ticker=event_payload["series_ticker"])
    assert hit is not None and hit.family is MarketFamily.TOTAL_POINTS
    assert classify_kalshi_nfl_market({"ticker": "KXNFLTEAMTOTAL-26SEP28PHICHI"}) is None

    pm_total = next(item for item in _pm_indkc()["markets"] if item["sportsMarketType"] == "totals")
    assert classify_polymarket_nfl_market(pm_total).family is MarketFamily.TOTAL_POINTS
    playerish = {
        **pm_total,
        "question": "Colston Loveland: O/U 32.5 receiving yards",
        "slug": "nfl-phi-chi-loveland-total-receiving-32pt5",
        "sportsMarketType": "totals",
        "groupItemTitle": "O/U 32.5",
    }
    assert classify_polymarket_nfl_market(playerish) is None


def test_price_engine_nfl_path_skips_normalize_market_and_rejects_player_prop() -> None:
    _, mb_total = _normalize_mb_family(_clone_mb_total(_mb_indkc(), line=Decimal("47.5")))
    identity = type(
        "Identity",
        (),
        {
            "register_canonical_key": f"{CANONICAL_NFL_TOTAL_POINTS}:47.5",
            "kickoff_utc": mb_total.event.kickoff_utc,
            "matchbook_event_id": mb_total.event.source_event_id,
            "matchbook_market_id": mb_total.source_market_id,
            "matchbook_runner_ids": [
                type("R", (), {"native_id": runner.source_runner_id, "outcome": runner.outcome.value})()
                for runner in mb_total.runners
            ],
            "line": "47.5",
            "competition": "NFL",
            "home_canonical": mb_total.event.home_team,
            "away_canonical": mb_total.event.away_team,
            "required_outcomes": [CanonicalOutcome.OVER.value, CanonicalOutcome.UNDER.value],
        },
    )()
    assert _family_from_key(identity) is MarketFamily.TOTAL_POINTS
    market = _canonical_matchbook_market(identity)
    assert market is not None
    assert market.family is MarketFamily.TOTAL_POINTS

    calls: list[str] = []
    original = MatchbookNormalizer.normalize_market

    def _track(self, event, payload):
        calls.append(str(payload.get("name")))
        return original(self, event, payload)

    MatchbookNormalizer.normalize_market = _track
    try:
        priced = _clone_mb_total(_mb_indkc(), line=Decimal("47.5"))
        for runner in priced["runners"]:
            runner["prices"] = [
                {"odds-type": "DECIMAL", "side": "back", "odds": 1.91, "available-amount": 50}
            ]
        observation = MatchbookObservationBuilder().build_from_canonical(
            market,
            priced,
            observed_at=NOW,
        )
        assert calls == []
        assert observation.metadata.get("skipped_matchbook_normalize_market") is True
        assert observation.market.family is MarketFamily.TOTAL_POINTS
        assert matchbook_payload_matches_nfl_family(priced, MarketFamily.TOTAL_POINTS)
        assert (
            matchbook_payload_matches_nfl_family(
                _loveland_receiving_yards_payload(), MarketFamily.TOTAL_POINTS
            )
            is False
        )
    finally:
        MatchbookNormalizer.normalize_market = original


def test_poisoned_catalogue_player_prop_fails_closed_on_identity_check() -> None:
    assert (
        matchbook_payload_matches_nfl_family(
            _loveland_receiving_yards_payload(), MarketFamily.TOTAL_POINTS
        )
        is False
    )
    payload = RetrievedVenuePayload(payload=_loveland_receiving_yards_payload(), retrieved_at=NOW)
    assert payload.payload["market-type"] == "total"
    assert classify_matchbook_nfl_market(payload.payload) is None


def test_polymarket_normalizer_rejects_playerish_totals() -> None:
    event = PolymarketNormalizer().normalize_event(_pm_indkc())
    pm_total = next(item for item in _pm_indkc()["markets"] if item["sportsMarketType"] == "totals")
    pm_total = {
        **pm_total,
        "question": "Colston Loveland receiving yards O/U 32.5",
        "slug": "player-total-receiving",
        "clobTokenIds": [
            "101tot000111222333444555666777888999000111222333",
            "202tot000111222333444555666777888999000111222333",
        ],
    }
    with pytest.raises(VenueNormalizationError, match="unsupported Polymarket NFL market"):
        PolymarketNormalizer().normalize_market(event, pm_total)


def test_soccer_matchbook_total_goals_name_is_out_of_scope() -> None:
    """Issue #590 must not retarget soccer Match Odds / Total Goals recognisers."""

    family, _line = _matchbook_market_family(
        "Total Goals",
        {
            "name": "Total Goals",
            "market-type": "point-total",
            "runners": [
                {"name": "Over 2.5", "handicap": 2.5},
                {"name": "Under 2.5", "handicap": 2.5},
            ],
        },
        home_team="Arsenal",
        away_team="Chelsea",
    )
    assert family is MarketFamily.TOTAL_GOALS
    assert classify_matchbook_nfl_market({"name": "Total Goals", "market-type": "point-total"}) is None
