"""Issue #548: one registry and shared discovery seams for the current wave.

Composes PM selected-series discovery, display-only fixture filters, ATP/WTA
Stage 1, MLB Stage 1, and the rule that tennis observation rows do not widen
catalogue admission for the sports that already have a scan-eligible set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import test_tennis_stage1 as tennis_fixtures

from sports_hedge.application.collector import (
    matchbook_scope_discovery_params,
    universe_catalogue_pairs,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.fixture_sport import resolve_discovered_fixture_sport
from sports_hedge.application.target_competitions import (
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    TARGET_COMPETITIONS,
    _scope_diagnostic_sport,
    competition_by_code,
    kalshi_series_tickers_for_codes,
    operator_competition_catalog,
    polymarket_series_ids_for_codes,
    selected_includes_soccer,
)
from sports_hedge.application.universe_matching_report import identity_rule_for_sport
from sports_hedge.catalogue.admission import catalogue_allows_solver
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatcher, MarketMatchResult
from sports_hedge.matching.paper_assumed import paper_assumed_solver_model
from sports_hedge.mlb.constants import MATCHBOOK_MLB_COMPETITION_TAG_ID
from sports_hedge.nba.constants import MATCHBOOK_NBA_COMPETITION_TAG_ID
from sports_hedge.tennis.tournaments import admitted_tournament, tournament_alias_labels

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
COMBINED = ["premier_league", "nfl", "mlb", "atp", "wta"]


def test_registry_is_one_version_with_mlb_atp_and_wta() -> None:
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert len(TARGET_COMPETITIONS) == PRINCIPAL_OPERATOR_COMPETITION_COUNT == 37
    assert len(catalog) == 37
    assert OPERATOR_COMPETITION_REGISTRY_VERSION == 9
    for code in ("mlb", "atp", "wta"):
        assert catalog[code]["selectable"] is True
        assert catalog[code]["default_selected"] is False
        assert catalog[code]["paper_executable"] is False
    assert catalog["atp"]["selector_label"] == "ATP (Hangzhou, Chengdu)"
    assert catalog["wta"]["selector_label"] == "WTA (Singapore, Seoul)"
    assert catalog["premier_league"]["paper_executable"] is True
    assert selected_includes_soccer(COMBINED) is True


def test_admitted_tennis_tournaments_are_the_observed_four_only() -> None:
    assert set(tournament_alias_labels()) == {
        "atp hangzhou",
        "hangzhou open",
        "atp chengdu",
        "chengdu open",
        "wta singapore",
        "singapore open",
        "wta seoul",
    }
    assert admitted_tournament("Rome Masters") is None
    assert admitted_tournament("Korea Open") is None
    assert admitted_tournament("Wimbledon") is None


def test_combined_scope_keeps_every_provider_family_without_a_dropping_tag() -> None:
    params = matchbook_scope_discovery_params(
        COMBINED,
        football_sport_id="15",
        american_football_sport_id="1",
        baseball_sport_id="3",
        tennis_sport_id="9",
    )
    assert params["sport-ids"] == "15,1,3,9"
    assert "tag-ids" not in params
    nba_mixed = matchbook_scope_discovery_params(
        ["nba", "mlb", "atp"],
        basketball_sport_id="4",
        baseball_sport_id="3",
        tennis_sport_id="9",
    )
    assert nba_mixed["sport-ids"] == "4,3,9"
    assert "tag-ids" not in nba_mixed
    assert MATCHBOOK_NBA_COMPETITION_TAG_ID not in nba_mixed.values()
    mlb_with_tennis = matchbook_scope_discovery_params(
        ["mlb", "wta"],
        baseball_sport_id="3",
        tennis_sport_id="9",
    )
    assert mlb_with_tennis["sport-ids"] == "3,9"
    assert MATCHBOOK_MLB_COMPETITION_TAG_ID not in mlb_with_tennis.values()

    series = polymarket_series_ids_for_codes(
        ["premier_league", "uefa_nations_league", "mlb", "atp", "wta"]
    )
    assert "11446" in series
    assert "3" in series
    assert "10365" in series
    assert "10366" in series
    tickers = kalshi_series_tickers_for_codes(COMBINED)
    for ticker in (
        "KXEPLGAME",
        "KXNFLGAME",
        "KXMLBGAME",
        "KXMLBTOTAL",
        "KXATPMATCH",
        "KXWTAMATCH",
    ):
        assert ticker in tickers
    assert "KXMLBSPREAD" not in tickers
    assert "KXATPGAME" not in tickers


def test_scope_and_matching_report_name_each_sport_rule() -> None:
    assert _scope_diagnostic_sport(competition_by_code("premier_league")) == "football"
    assert _scope_diagnostic_sport(competition_by_code("nfl")) == "american_football"
    assert _scope_diagnostic_sport(competition_by_code("nba")) == "basketball"
    assert _scope_diagnostic_sport(competition_by_code("ncaab")) == "basketball"
    assert _scope_diagnostic_sport(competition_by_code("mlb")) == "baseball"
    assert _scope_diagnostic_sport(competition_by_code("atp")) == "tennis"
    assert _scope_diagnostic_sport(competition_by_code("wta")) == "tennis"
    assert identity_rule_for_sport("football") == "football_participants_kickoff_5m"
    assert identity_rule_for_sport("american_football") == "nfl_curated_clubs_kickoff_5m"
    assert identity_rule_for_sport("basketball") == "basketball_curated_clubs_kickoff_5m"
    assert identity_rule_for_sport("baseball") == "mlb_curated_clubs_minute_game_key"
    assert identity_rule_for_sport("tennis") == "tennis_player_pair_tour_tournament_round_14d"


def test_fixture_sport_read_model_names_all_five_sports() -> None:
    assert resolve_discovered_fixture_sport(target_competition_code="premier_league") == "football"
    assert resolve_discovered_fixture_sport(target_competition_code="nfl") == "american_football"
    assert resolve_discovered_fixture_sport(target_competition_code="nba") == "basketball"
    assert resolve_discovered_fixture_sport(target_competition_code="ncaab") == "basketball"
    assert resolve_discovered_fixture_sport(target_competition_code="mlb") == "baseball"
    assert resolve_discovered_fixture_sport(target_competition_code="atp") == "tennis"
    assert resolve_discovered_fixture_sport(target_competition_code="wta") == "tennis"
    assert resolve_discovered_fixture_sport(register_canonical_key="MLB_TOTAL_RUNS_FT:7.5") == "baseball"
    assert resolve_discovered_fixture_sport(register_canonical_key="TENNIS_MATCH_WINNER") == "tennis"


def test_tennis_observation_rows_do_not_widen_other_sports(monkeypatch) -> None:
    left, right = tennis_fixtures._pair_markets()
    matched = MarketMatcher().match(left, right)
    tennis_pair = _wrapped(left, right, matched)
    assert scan_eligible_pair(left, right, matched) is False
    assert catalogue_allows_solver(left, right) is False
    assert paper_assumed_solver_model(left, right) is None
    assert universe_catalogue_pairs([tennis_pair]) == [tennis_pair]

    football_event = CanonicalEvent(
        sport="football",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=NOW,
        source_venue=VenueName.MATCHBOOK,
        source_event_id="pl-1",
    )
    football_left = CanonicalMarket(
        event=football_event,
        source_venue=VenueName.MATCHBOOK,
        source_market_id="mb-pl",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=SettlementFingerprint(),
    )
    football_right = football_left.model_copy(
        update={"source_venue": VenueName.KALSHI, "source_market_id": "k-pl"}
    )
    unmatched = MarketMatchResult(matched=False, confidence=0.0, reasons=["event_mismatch"])
    football_pair = _wrapped(football_left, football_right, unmatched)
    monkeypatch.setattr(
        "sports_hedge.matching.approved_register.registered_canonical_key",
        lambda *_args, **_kwargs: "MATCH_RESULT_FT",
    )
    assert universe_catalogue_pairs([football_pair]) == []
    assert universe_catalogue_pairs([tennis_pair, football_pair]) == [tennis_pair]


class _Normalized:
    def __init__(self, canonical: CanonicalMarket) -> None:
        self.canonical = canonical


def _wrapped(left: CanonicalMarket, right: CanonicalMarket, match: MarketMatchResult):
    return (left.source_venue, right.source_venue, _Normalized(left), _Normalized(right), match)
