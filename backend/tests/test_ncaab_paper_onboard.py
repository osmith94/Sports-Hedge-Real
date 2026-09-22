"""NCAAB PAPER onboard: census, selector, identity, structural families, fail-closed settlement.

PAPER / read-only. Captured 2026-09-22 public payloads plus structural synthetics.
Not owner-live quotes. Not modelled probabilities.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.catalogue_maintenance import family_key_from_kalshi_series
from sports_hedge.application.complete_set import solver_model_for_pair
from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    build_indexed_candidates,
    naive_pair_space,
)
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.target_competitions import (
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    REJECTED_NON_NCAAB_BASKETBALL,
    TargetCompetitionCode,
    default_operator_competition_code_values,
    operator_competition_catalog,
    resolve_target_competition_from_kalshi_ticker,
    scope_matchbook_event,
    selected_includes_ncaab,
    selected_includes_soccer,
)
from sports_hedge.catalogue.admission import catalogue_allows_live_execution, catalogue_allows_solver
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.resolver import phase1_seed_rules
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.matching.events import EventMatcher
from sports_hedge.ncaab.constants import (
    CANONICAL_NCAAB_GAME_WINNER,
    MATCHBOOK_BASKETBALL_SPORT_ID,
    NCAAB_COMPETITION,
    NCAAB_DIVISION_I_PROGRAM_COUNT,
    NCAAB_MISSING_VENUE_EVIDENCE_REASON,
    NCAAB_PAIR_UNAPPROVED_REASON,
    NCAAB_SPORT,
    POLYMARKET_NCAAB_SERIES_ID,
    POLYMARKET_NCAAB_SPORT,
)
from sports_hedge.ncaab.detect import is_ncaab_payload, rejected_kalshi_ncaab_series
from sports_hedge.ncaab.markets import is_exact_half_line
from sports_hedge.ncaab.register import ncaab_structural_identity
from sports_hedge.ncaab.settlement import (
    is_ncaab_paper_trade,
    ncaab_automatic_settlement_blocker,
    ncaab_missing_venue_evidence_blocker,
    ncaab_paper_settlement,
)
from sports_hedge.ncaab.teams import (
    NCAAB_PROGRAMS,
    ncaab_program_count,
    resolve_ncaab_team,
)
from sports_hedge.nfl.settlement import is_nfl_paper_trade
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)
from sports_hedge.paper.result_resolution import resolve_paper_trade_settlement
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.venues.matchbook import select_basketball_sport_id

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ncaab"
TIPOFF = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 22, 14, 47, tzinfo=UTC)


def _load(name: str) -> dict:
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _pm_event() -> dict:
    return dict(_load("polymarket_event_cbb_morgst_george_families.json")["payload"])


def _ncaab_event(*, home: str, away: str, venue: VenueName, source_id: str, kickoff: datetime = TIPOFF) -> CanonicalEvent:
    return CanonicalEvent(
        sport=NCAAB_SPORT,
        competition=NCAAB_COMPETITION,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_id,
    )


def _gw_market(event: CanonicalEvent, venue: VenueName, source_id: str) -> CanonicalMarket:
    return CanonicalMarket(
        event=event,
        source_venue=venue,
        source_market_id=source_id,
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=ncaab_paper_settlement(family=MarketFamily.GAME_WINNER),
        runners=[
            CanonicalRunner(source_runner_id=f"{source_id}:h", outcome=CanonicalOutcome.HOME, label="home"),
            CanonicalRunner(source_runner_id=f"{source_id}:a", outcome=CanonicalOutcome.AWAY, label="away"),
        ],
    )


def _matchbook_ncaab_payload() -> dict:
    return {
        "id": "mb-ncaab-duke-xav",
        "name": "Duke at Xavier",
        "sport-id": 4,
        "sport-name": "Basketball",
        "start": "2026-11-16T00:00:00Z",
        "meta-tags": [
            {"id": 4, "name": "Basketball", "type": "SPORT"},
            {"id": "ncaa-tag", "name": "NCAA", "type": "COMPETITION"},
        ],
        "markets": [
            {
                "id": "mb-ncaab-ml",
                "name": "Moneyline",
                "market-type": "money_line",
                "runners": [
                    {"id": "r-duke", "name": "Duke"},
                    {"id": "r-xav", "name": "Xavier"},
                ],
            }
        ],
    }


def _kalshi_game_event() -> dict:
    return {
        "event_ticker": "KXNCAAMBGAME-26NOV15DUKEXAV",
        "series_ticker": "KXNCAAMBGAME",
        "title": "Duke at Xavier",
        "milestone": {
            "type": "basketball_game",
            "title": "Duke at Xavier",
            "start_date": "2026-11-16T00:00:00Z",
            "details": {"league": "NCAAMB", "home_team_id": "home-uuid", "away_team_id": "away-uuid"},
        },
    }


def _kalshi_game_markets() -> list[dict]:
    return [
        {"ticker": "KXNCAAMBGAME-26NOV15DUKEXAV-DUKE", "title": "Duke wins", "yes_sub_title": "Duke"},
        {"ticker": "KXNCAAMBGAME-26NOV15DUKEXAV-XAV", "title": "Xavier wins", "yes_sub_title": "Xavier"},
    ]


def test_selector_visible_with_zero_fixtures_and_not_default() -> None:
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert len(catalog) == PRINCIPAL_OPERATOR_COMPETITION_COUNT
    row = catalog["ncaab"]
    assert row["selectable"] is True
    assert row["default_selected"] is False
    assert row["display_name"] == "NCAA Men's Basketball"
    assert row["selector_label"] == "NCAA Men"
    assert row["group_label"] == "College Basketball"
    assert "ncaaw" not in catalog
    assert TargetCompetitionCode.NCAAB.value not in default_operator_competition_code_values()
    assert selected_includes_ncaab(["ncaab"]) is True
    assert selected_includes_soccer(["ncaab"]) is False
    assert selected_includes_soccer(["ncaab", "nfl"]) is False
    assert selected_includes_soccer(["ncaab", "premier_league"]) is True


def test_division_i_registry_count_and_ambiguous_school_names() -> None:
    assert ncaab_program_count() == NCAAB_DIVISION_I_PROGRAM_COUNT == 362
    assert len({program.canonical for program in NCAAB_PROGRAMS}) == 362
    miami = resolve_ncaab_team("Miami")
    assert miami.ok is False and miami.ambiguous is True
    fl = resolve_ncaab_team("Miami (FL)")
    oh = resolve_ncaab_team("Miami (OH)")
    assert fl.ok and oh.ok
    assert fl.canonical != oh.canonical
    usc = resolve_ncaab_team("USC Trojans")
    south_carolina = resolve_ncaab_team("South Carolina Gamecocks")
    assert usc.ok and south_carolina.ok
    assert usc.canonical != south_carolina.canonical
    assert resolve_ncaab_team("SC").ambiguous is True
    assert resolve_ncaab_team("UT").ambiguous is True
    st_johns = resolve_ncaab_team("St. John's")
    st_joes = resolve_ncaab_team("Saint Joseph's")
    st_marys = resolve_ncaab_team("Saint Mary's")
    assert st_johns.ok and st_joes.ok and st_marys.ok
    assert len({st_johns.canonical, st_joes.canonical, st_marys.canonical}) == 3
    assert resolve_ncaab_team("Loyola").ambiguous is True
    luc = resolve_ncaab_team("Loyola Chicago")
    lmu = resolve_ncaab_team("Loyola Marymount")
    lmd = resolve_ncaab_team("Loyola (MD)")
    assert luc.ok and lmu.ok and lmd.ok
    assert len({luc.canonical, lmu.canonical, lmd.canonical}) == 3
    assert resolve_ncaab_team("State").ambiguous is True
    assert resolve_ncaab_team("Tech").ambiguous is True
    assert resolve_ncaab_team("WNBA").rejected is True
    assert resolve_ncaab_team("NBA").rejected is True


def test_ncaaw_nba_wnba_and_outrights_excluded_from_fixture_pipeline() -> None:
    wnba = _load("matchbook_event_wnba_analogue_not_ncaab.json")["payload"]
    assert is_ncaab_payload(wnba) is False
    decision = scope_matchbook_event(wnba, selected_codes=["ncaab"])
    assert decision.allowed is False
    assert decision.reason == REJECTED_NON_NCAAB_BASKETBALL
    assert is_ncaab_payload(_load("polymarket_sports_cwbb.json")["payload"]) is False
    assert is_ncaab_payload(_load("polymarket_sports_ncaab.json")["payload"]) is False
    assert rejected_kalshi_ncaab_series("KXNCAAWBGAME") is True
    assert rejected_kalshi_ncaab_series("KXNCAABGAME") is True
    assert rejected_kalshi_ncaab_series("KXNCAAMBACC") is True
    assert resolve_target_competition_from_kalshi_ticker("KXMARMAD-27") is None
    assert resolve_target_competition_from_kalshi_ticker("KXNCAAWBGAME") is None
    assert family_key_from_kalshi_series("KXNCAAMBGAME") == CANONICAL_NCAAB_GAME_WINNER
    assert family_key_from_kalshi_series("KXNCAAMBGAME") != "MATCH_RESULT_FT"
    assert family_key_from_kalshi_series("KXNCAAWBGAME") is None
    assert family_key_from_kalshi_series("KXMARMAD-27") is None


def test_captured_census_zero_open_games_is_healthy() -> None:
    kalshi_open = _load("kalshi_events_game_open_empty.json")
    pm_open = _load("polymarket_events_cbb_open_empty.json")
    assert kalshi_open["payload"]["events"] == []
    assert kalshi_open["payload"]["count"] == 0
    assert pm_open["payload"]["events"] == []
    assert pm_open["payload"]["count"] == 0
    cbb = _load("polymarket_sports_cbb.json")["payload"]
    assert str(cbb["series"]) == POLYMARKET_NCAAB_SERIES_ID
    assert cbb["sport"] == POLYMARKET_NCAAB_SPORT
    sports_row = _load("matchbook_lookups_sports_basketball.json")["payload"]
    basketball_id = select_basketball_sport_id([sports_row])
    assert str(basketball_id) == MATCHBOOK_BASKETBALL_SPORT_ID


def test_polymarket_historical_game_winner_and_half_lines_normalize() -> None:
    payload = _pm_event()
    event = PolymarketNormalizer().normalize_event(payload)
    assert event.sport == NCAAB_SPORT
    assert event.competition == NCAAB_COMPETITION
    assert event.home_team == "Georgetown Hoyas"
    assert event.away_team == "Morgan State Bears"
    by_type = {item["sportsMarketType"]: item for item in payload["markets"]}
    moneyline = PolymarketNormalizer().normalize_market(event, by_type["moneyline"])
    assert ncaab_structural_identity(moneyline)
    assert moneyline.family is MarketFamily.GAME_WINNER
    spread = PolymarketNormalizer().normalize_market(event, by_type["spreads"])
    assert ncaab_structural_identity(spread)
    assert is_exact_half_line(spread.line)
    total = PolymarketNormalizer().normalize_market(event, by_type["totals"])
    assert ncaab_structural_identity(total)
    assert total.line == Decimal("154.5")


def test_kalshi_and_matchbook_game_winner_structural_and_integer_rejected() -> None:
    kalshi_event = KalshiNormalizer().normalize_event(_kalshi_game_event())
    assert kalshi_event.home_team == "Xavier Musketeers"
    assert kalshi_event.away_team == "Duke Blue Devils"
    [game] = KalshiNormalizer().assemble_canonical_markets(
        kalshi_event, _kalshi_game_markets(), event_payload=_kalshi_game_event()
    )
    assert ncaab_structural_identity(game)
    mb_payload = _matchbook_ncaab_payload()
    mb_event = MatchbookNormalizer().normalize_event(mb_payload)
    mb_market = MatchbookNormalizer().normalize_market(mb_event, mb_payload["markets"][0])
    assert ncaab_structural_identity(mb_market)
    integer_spread = {
        "id": "pm-int",
        "sportsMarketType": "spreads",
        "question": "Spread: Duke Blue Devils (-7)",
        "line": -7,
        "outcomes": ["Duke Blue Devils", "Xavier Musketeers"],
        "clobTokenIds": ["11111111111111111111111111111111111111111111111111111111111111111111111111111", "22222222222222222222222222222222222222222222222222222222222222222222222222222"],
    }
    with pytest.raises(VenueNormalizationError, match="half-point"):
        PolymarketNormalizer().normalize_market(mb_event, integer_spread)
    half_payload = {
        "id": "mb-h1",
        "name": "1st Half Moneyline",
        "market-type": "money_line",
        "runners": [{"id": "a", "name": "Duke"}, {"id": "b", "name": "Xavier"}],
    }
    with pytest.raises(VenueNormalizationError, match="period"):
        MatchbookNormalizer().normalize_market(mb_event, half_payload)


def test_no_paper_admission_and_settlement_never_ignores_a_venue() -> None:
    home, away = "Xavier Musketeers", "Duke Blue Devils"
    left = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.KALSHI, source_id="k-1"),
        VenueName.KALSHI,
        "k-ml",
    )
    right = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.POLYMARKET, source_id="pm-1"),
        VenueName.POLYMARKET,
        "pm-ml",
    )
    mb = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.MATCHBOOK, source_id="mb-1"),
        VenueName.MATCHBOOK,
        "mb-ml",
    )
    assert registered_canonical_key(left, right) is None
    assert registered_canonical_key(mb, left) is None
    assert registered_canonical_key(mb, right) is None
    assessment = classify_pair(left, mb)
    assert assessment.paper_mode_admitted is False
    assert catalogue_allows_solver(left, mb) is False
    assert catalogue_allows_live_execution(left, mb) is False
    assert solver_model_for_pair(left, right) is None or assessment.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    trade = PaperTrade(
        trade_id="ncaab-1",
        opportunity_id="opp-ncaab-1",
        canonical_event_id="ncaab-duke-xav",
        competition=NCAAB_COMPETITION,
        home_team=home,
        away_team=away,
        market_family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        legs=[
            PaperTradeLeg(
                venue=VenueName.KALSHI,
                outcome="home",
                currency="USD",
                requested_stake=Decimal("1"),
                filled_stake=Decimal("1"),
                displayed_odds=Decimal("1.9"),
                filled_odds=Decimal("1.9"),
                source_market_id="KXNCAAMBGAME-26NOV15DUKEXAV-XAV",
                source_event_id="KXNCAAMBGAME-26NOV15DUKEXAV",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
            PaperTradeLeg(
                venue=VenueName.POLYMARKET,
                outcome="away",
                currency="USD",
                requested_stake=Decimal("1"),
                filled_stake=Decimal("1"),
                displayed_odds=Decimal("2.1"),
                filled_odds=Decimal("2.1"),
                source_market_id="655763",
                source_event_id="68009",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
        ],
    )
    assert is_ncaab_paper_trade(trade) is True
    assert is_nfl_paper_trade(trade) is False
    blocked = resolve_paper_trade_settlement(trade)
    assert blocked.winning_outcome is None
    assert blocked.blocker == NCAAB_PAIR_UNAPPROVED_REASON
    assert ncaab_automatic_settlement_blocker(trade) == NCAAB_PAIR_UNAPPROVED_REASON
    missing = ncaab_missing_venue_evidence_blocker(trade, {VenueName.MATCHBOOK, VenueName.KALSHI})
    assert missing == NCAAB_MISSING_VENUE_EVIDENCE_REASON


def test_exact_identity_does_not_fuzzy_collapse_nearby_college_games() -> None:
    matcher = EventMatcher()
    miami_fl = _ncaab_event(
        home="Miami Hurricanes",
        away="Duke Blue Devils",
        venue=VenueName.KALSHI,
        source_id="k-mia",
    )
    miami_oh = _ncaab_event(
        home="Miami (OH) RedHawks",
        away="Duke Blue Devils",
        venue=VenueName.POLYMARKET,
        source_id="pm-moh",
    )
    result = matcher.match(miami_fl, miami_oh)
    assert result.matched is False
    same = matcher.match(
        miami_fl,
        _ncaab_event(
            home="Miami Hurricanes",
            away="Duke Blue Devils",
            venue=VenueName.POLYMARKET,
            source_id="pm-mia",
        ),
    )
    assert same.matched is True
    assert matcher.could_match(miami_fl, miami_oh) is False


def test_large_ncaab_slate_indexed_candidates_stay_bounded() -> None:
    programs = NCAAB_PROGRAMS[:320]
    games = [(programs[index], programs[index + 1]) for index in range(0, 320, 2)]
    assert len(games) == 160
    items: list[VenueEvent] = []
    for index, (away, home) in enumerate(games):
        kickoff = TIPOFF + timedelta(minutes=(index % 12) * 10)
        for venue, prefix in (
            (VenueName.MATCHBOOK, "mb"),
            (VenueName.POLYMARKET, "pm"),
            (VenueName.KALSHI, "k"),
        ):
            source_id = f"{prefix}-{index}"
            items.append(
                VenueEvent(
                    venue=venue,
                    raw={"id": source_id},
                    canonical=_ncaab_event(
                        home=home.canonical,
                        away=away.canonical,
                        venue=venue,
                        source_id=source_id,
                        kickoff=kickoff,
                    ),
                    source_event_id=source_id,
                )
            )
    naive = naive_pair_space(len(items))
    candidates, diagnostics = build_indexed_candidates(
        items,
        kickoff_tolerance=timedelta(minutes=5),
        matcher=EventMatcher(),
    )
    assert diagnostics["candidate_pairs_generated"] == len(candidates)
    assert diagnostics["candidate_pairs_generated"] < naive
    assert diagnostics["candidate_pairs_generated"] >= 160
    # Unresolved/fuzzy labels must not explode the pair space on a 160-game slate.
    assert diagnostics["candidate_pairs_generated"] < 2_000
    colliding_home = [
        pair
        for pair in candidates
        if {pair[0].canonical.home_team, pair[1].canonical.home_team}
        == {"Miami Hurricanes", "Miami (OH) RedHawks"}
    ]
    assert colliding_home == []
    nearby = []
    for venue, prefix in (
        (VenueName.KALSHI, "k-mia"),
        (VenueName.POLYMARKET, "pm-mia"),
        (VenueName.KALSHI, "k-moh"),
        (VenueName.POLYMARKET, "pm-moh"),
    ):
        home = "Miami Hurricanes" if "mia" in prefix else "Miami (OH) RedHawks"
        nearby.append(
            VenueEvent(
                venue=venue,
                raw={"id": prefix},
                canonical=_ncaab_event(
                    home=home,
                    away="Duke Blue Devils",
                    venue=venue,
                    source_id=prefix,
                    kickoff=TIPOFF,
                ),
                source_event_id=prefix,
            )
        )
    nearby_candidates, _ = build_indexed_candidates(
        nearby,
        kickoff_tolerance=timedelta(minutes=5),
        matcher=EventMatcher(),
    )
    mixed = [
        pair
        for pair in nearby_candidates
        if {pair[0].canonical.home_team, pair[1].canonical.home_team}
        == {"Miami Hurricanes", "Miami (OH) RedHawks"}
    ]
    assert mixed == []
    assert any(
        pair[0].canonical.home_team == pair[1].canonical.home_team == "Miami Hurricanes"
        for pair in nearby_candidates
    )


def test_venue_fees_and_provider_concurrency_unchanged() -> None:
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    fees_root = Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "fees"
    for path in fees_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "ncaab" not in text.casefold()
        assert "KXNCAAMB" not in text
    collector = Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "application" / "collector.py"
    tree = ast.parse(collector.read_text(encoding="utf-8"))
    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_PROVIDER_CONCURRENCY":
                    found = True
    assert found
    rules = phase1_seed_rules()
    assert all("ncaab" not in (rule.detail or "").casefold() for rule in rules)
