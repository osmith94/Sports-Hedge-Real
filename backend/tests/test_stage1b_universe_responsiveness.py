"""Stage 1B: UNIVERSE market work must keep the event loop under 250ms.

The cartesian greedy scan is the semantic oracle. Register pre-indexing and
fixture-local match memoization may change how pairs are found, not which
pairs, reasons, or inventory rows are produced.

Data class: synthetic fixture/demo markets. Not live, historical, or modelled
venue quotes. PAPER / read-only.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from loop_liveness_harness import CallbackProfiler

from sports_hedge.application.collector import (
    _canonical_markets_are_priority_match_result,
    _greedy_unique_market_pairs,
    _NormalizedMarket,
)
from sports_hedge.application.event_loop_activity import (
    loop_subphase_snapshot,
    reset_loop_activity,
    sync_subphase,
)
from sports_hedge.application.fixture_inventory import (
    InventoryComparisonStatus,
    InventoryMarket,
    VenueMarketFacts,
    _InventorySources,
    _KalshiAttachmentIndex,
    _attach_kalshi,
    _inventory_from_facts,
    _kalshi_match_results,
    _kalshi_related_to_row,
    _sort_rows,
    _venue_only_row,
    assemble_fixture_inventory,
    inventory_is_comparable_opportunity,
)
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import canonical_key_for_market
from sports_hedge.matching.bulk_market_pairs import greedy_unique_market_matches
from sports_hedge.matching.markets import MarketMatcher, MarketMatchResult, memoize_market_matches
from sports_hedge.mlb.constants import MLB_SPORT
from sports_hedge.nba.constants import NBA_SPORT
from sports_hedge.ncaab.constants import NCAAB_COMPETITION, NCAAB_SPORT
from sports_hedge.nfl.constants import NFL_SPORT
from sports_hedge.tennis.constants import TENNIS_EVENT_SINGLES, TENNIS_SPORT
from sports_hedge.normalization.kalshi_contract_terms import GAMEWIN_SCOPE_UNAVAILABLE_REASON

KICKOFF = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
SLICE_BOUND_S = 0.25
PRIORITY = _canonical_markets_are_priority_match_result


def _event(
    venue: VenueName,
    *,
    home: str = "Tottenham",
    away: str = "Everton",
    competition: str = "Premier League",
    sport: str = "football",
    source_id: str | None = None,
    scheduled_game_key: str | None = None,
    tournament: str = "",
    round_label: str = "",
    event_type: str = "",
) -> CanonicalEvent:
    return CanonicalEvent(
        sport=sport,
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id or f"{venue.value}-event",
        scheduled_game_key=scheduled_game_key,
        tournament=tournament,
        round_label=round_label,
        event_type=event_type,
    )


def _settlement(
    *,
    line: Decimal | None = None,
    scope: SettlementScope = SettlementScope.REGULATION_TIME,
    extra_time: bool | None = False,
    penalties: bool | None = False,
    unknown_reason: str | None = None,
    period: FootballPeriod = FootballPeriod.FULL_TIME,
) -> SettlementFingerprint:
    return SettlementFingerprint(
        scope=scope,
        period=period,
        line=line,
        push_possible=False if line is None else line != line.to_integral_value(),
        extra_time_included=extra_time,
        penalties_included=penalties,
        unknown_reason=unknown_reason,
    )


def _market(
    event: CanonicalEvent,
    *,
    family: MarketFamily,
    source_id: str,
    outcomes: list[CanonicalOutcome],
    line: Decimal | None = None,
    settlement: SettlementFingerprint | None = None,
) -> CanonicalMarket:
    return CanonicalMarket(
        event=event,
        source_venue=event.source_venue,
        source_market_id=source_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=settlement or _settlement(line=line),
        runners=[
            CanonicalRunner(
                source_runner_id=f"{source_id}-{outcome.value}",
                outcome=outcome,
                label=outcome.value,
            )
            for outcome in outcomes
        ],
    )


def _hda(event: CanonicalEvent, source_id: str, **kwargs: object) -> CanonicalMarket:
    return _market(
        event,
        family=MarketFamily.MATCH_RESULT,
        source_id=source_id,
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY],
        **kwargs,  # type: ignore[arg-type]
    )


def _btts(event: CanonicalEvent, source_id: str) -> CanonicalMarket:
    return _market(
        event,
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        source_id=source_id,
        outcomes=[CanonicalOutcome.YES, CanonicalOutcome.NO],
    )


def _ftts(event: CanonicalEvent, source_id: str) -> CanonicalMarket:
    return _market(
        event,
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        source_id=source_id,
        outcomes=[
            CanonicalOutcome.HOME,
            CanonicalOutcome.AWAY,
            CanonicalOutcome.NO_GOAL,
        ],
    )


def _total(event: CanonicalEvent, source_id: str, line: str) -> CanonicalMarket:
    value = Decimal(line)
    return _market(
        event,
        family=MarketFamily.TOTAL_GOALS,
        source_id=source_id,
        outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
        line=value,
    )


def _moneyline(event: CanonicalEvent, source_id: str) -> CanonicalMarket:
    return _market(
        event,
        family=MarketFamily.GAME_WINNER,
        source_id=source_id,
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
    )


def _spread(event: CanonicalEvent, source_id: str, line: str) -> CanonicalMarket:
    value = Decimal(line)
    return _market(
        event,
        family=MarketFamily.POINT_SPREAD,
        source_id=source_id,
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
        line=value,
    )


def _points(event: CanonicalEvent, source_id: str, line: str) -> CanonicalMarket:
    value = Decimal(line)
    return _market(
        event,
        family=MarketFamily.TOTAL_POINTS,
        source_id=source_id,
        outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
        line=value,
    )


def _inventory(market: CanonicalMarket | None, *, name: str, source_id: str, venue: VenueName) -> InventoryMarket:
    event_id = market.event.source_event_id if market is not None else f"{venue.value}-absent"
    return InventoryMarket(
        venue=venue,
        source_event_id=event_id,
        source_market_id=source_id,
        raw_name=name,
        canonical=market,
        normalize_error=None if market is not None else "absent_leg",
    )


def _signature(index_left: int, index_right: int, match: MarketMatchResult) -> tuple[object, ...]:
    return (
        index_left,
        index_right,
        match.matched,
        match.confidence,
        tuple(match.reasons),
        match.provenance.model_dump(),
    )


def _cartesian_unique(
    left: list[CanonicalMarket | None],
    right: list[CanonicalMarket | None],
    matcher: MarketMatcher,
    *,
    priority_pair: object | None = None,
) -> list[tuple[int, int, MarketMatchResult]]:
    """Pre-Stage-1B scan: every pair, then stable priority/confidence greedy."""

    candidates: list[tuple[int, float, int, int, MarketMatchResult]] = []
    for left_index, left_market in enumerate(left):
        if left_market is None:
            continue
        for right_index, right_market in enumerate(right):
            if right_market is None:
                continue
            match = matcher.match(left_market, right_market)
            if not match.matched:
                continue
            priority = 0
            if priority_pair is not None:
                priority = 1 if priority_pair(left_market, right_market) else 0  # type: ignore[operator]
            candidates.append((priority, match.confidence, left_index, right_index, match))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    used_left: set[int] = set()
    used_right: set[int] = set()
    chosen: list[tuple[int, int, MarketMatchResult]] = []
    for _priority, _confidence, left_index, right_index, match in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        chosen.append((left_index, right_index, match))
    return chosen


def _assert_same_pairs(
    left: list[CanonicalMarket | None],
    right: list[CanonicalMarket | None],
    *,
    priority: bool,
) -> None:
    matcher = MarketMatcher()
    oracle = _cartesian_unique(
        left,
        right,
        matcher,
        priority_pair=PRIORITY if priority else None,
    )
    optimized = greedy_unique_market_matches(
        left,
        right,
        matcher,
        priority_pair=PRIORITY if priority else None,
    )
    assert [_signature(*item) for item in optimized] == [_signature(*item) for item in oracle]
    for left_index, left_market in enumerate(left):
        if left_market is None:
            continue
        for right_market in right:
            if right_market is None:
                continue
            match = matcher.match(left_market, right_market)
            if not match.matched:
                continue
            left_key = canonical_key_for_market(left_market)
            right_key = canonical_key_for_market(right_market)
            assert left_key is not None
            assert left_key == right_key


def _football_case() -> tuple[list[CanonicalMarket], list[CanonicalMarket], list[CanonicalMarket]]:
    matchbook = _event(VenueName.MATCHBOOK)
    kalshi = _event(VenueName.KALSHI)
    other = _event(VenueName.KALSHI, home="Arsenal", away="Chelsea", source_id="other-event")
    left = [
        _hda(matchbook, "mb-1x2"),
        _hda(matchbook, "mb-1x2-dup"),
        _btts(matchbook, "mb-btts"),
        _ftts(matchbook, "mb-ftts"),
        _total(matchbook, "mb-tg-25", "2.5"),
        _total(matchbook, "mb-tg-05", "0.5"),
        _total(matchbook, "mb-tg-int", "2"),
        _hda(
            matchbook,
            "mb-et",
            settlement=_settlement(
                scope=SettlementScope.INCLUDING_EXTRA_TIME,
                extra_time=True,
            ),
        ),
    ]
    unknown = _settlement(
        scope=SettlementScope.UNKNOWN,
        extra_time=None,
        penalties=None,
        period=FootballPeriod.UNKNOWN,
        unknown_reason=GAMEWIN_SCOPE_UNAVAILABLE_REASON,
    )
    right = [
        _hda(kalshi, "k-1x2"),
        _hda(kalshi, "k-1x2-unknown", settlement=unknown),
        _btts(kalshi, "k-btts"),
        _ftts(kalshi, "k-ftts"),
        _total(kalshi, "k-tg-25", "2.5"),
        _total(kalshi, "k-tg-15", "1.5"),
        _total(kalshi, "k-tg-int", "2"),
        _hda(other, "k-other-1x2"),
        _btts(kalshi, "k-btts-dup"),
    ]
    polymarket_event = _event(VenueName.POLYMARKET)
    polymarket = [
        _hda(polymarket_event, "pm-1x2"),
        _btts(polymarket_event, "pm-btts"),
        _total(polymarket_event, "pm-tg-25", "2.5"),
    ]
    return left, right, polymarket


def _nfl_case() -> tuple[list[CanonicalMarket], list[CanonicalMarket], list[CanonicalMarket]]:
    def event(venue: VenueName) -> CanonicalEvent:
        return _event(
            venue,
            home="Buffalo Bills",
            away="Kansas City Chiefs",
            competition="NFL",
            sport=NFL_SPORT,
        )

    ambiguous = _event(
        VenueName.KALSHI,
        home="New York",
        away="Kansas City Chiefs",
        competition="NFL",
        sport=NFL_SPORT,
        source_id="ambiguous-nfl",
    )
    left = [
        _moneyline(event(VenueName.MATCHBOOK), "mb-ml"),
        _moneyline(event(VenueName.MATCHBOOK), "mb-ml-dup"),
        _spread(event(VenueName.MATCHBOOK), "mb-spread", "-3.5"),
        _spread(event(VenueName.MATCHBOOK), "mb-spread-int", "-3"),
        _points(event(VenueName.MATCHBOOK), "mb-total", "47.5"),
    ]
    right = [
        _moneyline(event(VenueName.KALSHI), "k-ml"),
        _spread(event(VenueName.KALSHI), "k-spread", "-3.5"),
        _spread(event(VenueName.KALSHI), "k-spread-other", "-7.5"),
        _points(event(VenueName.KALSHI), "k-total", "47.5"),
        _points(event(VenueName.KALSHI), "k-total-int", "47"),
        _moneyline(ambiguous, "k-ambiguous"),
    ]
    polymarket = [
        _moneyline(event(VenueName.POLYMARKET), "pm-ml"),
        _spread(event(VenueName.POLYMARKET), "pm-spread", "-3.5"),
        _points(event(VenueName.POLYMARKET), "pm-total", "47.5"),
    ]
    return left, right, polymarket


def _row_signature(row: object) -> dict[str, object]:
    return row.model_dump(mode="json")  # type: ignore[attr-defined]


def test_football_pairing_matches_cartesian_oracle() -> None:
    left, right, polymarket = _football_case()
    _prove_pairing(left, right)
    _prove_pairing(left, polymarket)
    _prove_pairing(polymarket, right)
    matcher = MarketMatcher()
    assert matcher.match(left[0], polymarket[0]).matched is True
    chosen = greedy_unique_market_matches(left, right, matcher, priority_pair=PRIORITY)
    paired = {(left[i].source_market_id, right[j].source_market_id) for i, j, _match in chosen}
    assert ("mb-1x2", "k-1x2") in paired
    assert ("mb-btts", "k-btts") in paired
    assert ("mb-ftts", "k-ftts") in paired
    assert ("mb-tg-25", "k-tg-25") in paired
    assert ("mb-tg-int", "k-tg-int") not in paired
    assert all("k-other" not in right_id for _left_id, right_id in paired)


def _memo_matches_uncached(
    left: list[CanonicalMarket | None],
    right: list[CanonicalMarket | None],
) -> None:
    """Memoised MarketMatcher results are the uncached results, pair by pair."""

    matcher = MarketMatcher()
    pairs = [
        (left_market, right_market)
        for left_market in left
        if left_market is not None
        for right_market in right
        if right_market is not None
    ]
    plain = [matcher.match(left_market, right_market).model_dump() for left_market, right_market in pairs]
    with memoize_market_matches():
        cached = [
            matcher.match(left_market, right_market).model_dump() for left_market, right_market in pairs
        ]
    assert cached == plain


def _prove_pairing(
    left: list[CanonicalMarket | None],
    right: list[CanonicalMarket | None],
) -> list[tuple[int, int, MarketMatchResult]]:
    """Indexed selection must equal the cartesian oracle, including tie order."""

    _assert_same_pairs(left, right, priority=True)
    _assert_same_pairs(left, right, priority=False)
    _memo_matches_uncached(left, right)
    matcher = MarketMatcher()
    oracle = _cartesian_unique(left, right, matcher, priority_pair=PRIORITY)
    optimized = greedy_unique_market_matches(left, right, matcher, priority_pair=PRIORITY)
    assert [(item[0], item[1]) for item in optimized] == [(item[0], item[1]) for item in oracle]
    return oracle


def _two_way(event: CanonicalEvent, source_id: str, family: MarketFamily, line: str | None = None) -> CanonicalMarket:
    if family is MarketFamily.TOTAL_POINTS or family is MarketFamily.TOTAL_RUNS:
        return _market(
            event,
            family=family,
            source_id=source_id,
            outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
            line=Decimal(line or "0"),
        )
    return _market(
        event,
        family=family,
        source_id=source_id,
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
        line=None if line is None else Decimal(line),
    )


def _nba_case() -> tuple[list[CanonicalMarket | None], list[CanonicalMarket | None]]:
    def event(venue: VenueName, *, home: str, away: str, source_id: str) -> CanonicalEvent:
        return _event(
            venue,
            home=home,
            away=away,
            competition="NBA",
            sport=NBA_SPORT,
            source_id=source_id,
        )

    kalshi = event(VenueName.KALSHI, home="Boston Celtics", away="Los Angeles Lakers", source_id="nba-k")
    polymarket = event(
        VenueName.POLYMARKET,
        home="Boston Celtics",
        away="Los Angeles Lakers",
        source_id="nba-pm",
    )
    other = event(VenueName.POLYMARKET, home="Chicago Bulls", away="Denver Nuggets", source_id="nba-other")
    matchbook = event(
        VenueName.MATCHBOOK,
        home="Boston Celtics",
        away="Los Angeles Lakers",
        source_id="nba-mb",
    )
    left: list[CanonicalMarket | None] = [
        _two_way(kalshi, "k-ml", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-ml-dup", MarketFamily.GAME_WINNER),
        None,
        _two_way(kalshi, "k-spread", MarketFamily.POINT_SPREAD, "-3.5"),
        _two_way(kalshi, "k-spread-dup", MarketFamily.POINT_SPREAD, "-3.5"),
        _two_way(kalshi, "k-spread-other", MarketFamily.POINT_SPREAD, "-7.5"),
        _two_way(kalshi, "k-spread-int", MarketFamily.POINT_SPREAD, "-3"),
        _two_way(kalshi, "k-total", MarketFamily.TOTAL_POINTS, "220.5"),
        _two_way(kalshi, "k-total-int", MarketFamily.TOTAL_POINTS, "220"),
    ]
    right: list[CanonicalMarket | None] = [
        _two_way(polymarket, "pm-ml", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-ml-dup2", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-spread", MarketFamily.POINT_SPREAD, "-3.5"),
        _two_way(polymarket, "pm-total", MarketFamily.TOTAL_POINTS, "220.5"),
        _two_way(polymarket, "pm-total-other", MarketFamily.TOTAL_POINTS, "215.5"),
        _two_way(other, "pm-other", MarketFamily.GAME_WINNER),
        _two_way(matchbook, "mb-ml", MarketFamily.GAME_WINNER),
    ]
    return left, right


def _ncaab_case() -> tuple[list[CanonicalMarket | None], list[CanonicalMarket | None]]:
    def event(venue: VenueName, *, home: str, away: str, source_id: str) -> CanonicalEvent:
        return _event(
            venue,
            home=home,
            away=away,
            competition=NCAAB_COMPETITION,
            sport=NCAAB_SPORT,
            source_id=source_id,
        )

    kalshi = event(VenueName.KALSHI, home="Duke Blue Devils", away="Kansas Jayhawks", source_id="ncaab-k")
    polymarket = event(
        VenueName.POLYMARKET,
        home="Duke Blue Devils",
        away="Kansas Jayhawks",
        source_id="ncaab-pm",
    )
    other = event(VenueName.KALSHI, home="Duke Blue Devils", away="Gonzaga Bulldogs", source_id="ncaab-other")
    left: list[CanonicalMarket | None] = [
        _two_way(kalshi, "k-ml", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-spread", MarketFamily.POINT_SPREAD, "-5.5"),
        _two_way(kalshi, "k-spread-int", MarketFamily.POINT_SPREAD, "-5"),
        _two_way(kalshi, "k-total", MarketFamily.TOTAL_POINTS, "145.5"),
        _two_way(other, "k-other", MarketFamily.GAME_WINNER),
    ]
    right: list[CanonicalMarket | None] = [
        _two_way(polymarket, "pm-ml", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-spread", MarketFamily.POINT_SPREAD, "-5.5"),
        _two_way(polymarket, "pm-spread-other", MarketFamily.POINT_SPREAD, "-2.5"),
        _two_way(polymarket, "pm-total", MarketFamily.TOTAL_POINTS, "145.5"),
        _two_way(polymarket, "pm-total-int", MarketFamily.TOTAL_POINTS, "145"),
    ]
    return left, right


def _mlb_case() -> tuple[list[CanonicalMarket | None], list[CanonicalMarket | None]]:
    def event(
        venue: VenueName,
        *,
        source_id: str,
        game_key: str = "2026-09-20T18:00|game-1",
        away: str = "New York Yankees",
    ) -> CanonicalEvent:
        return _event(
            venue,
            home="Boston Red Sox",
            away=away,
            competition="MLB",
            sport=MLB_SPORT,
            source_id=source_id,
            scheduled_game_key=game_key,
        )

    kalshi = event(VenueName.KALSHI, source_id="mlb-k")
    polymarket = event(VenueName.POLYMARKET, source_id="mlb-pm")
    game_two = event(VenueName.POLYMARKET, source_id="mlb-game2", game_key="2026-09-20T18:00|game-2")
    left: list[CanonicalMarket | None] = [
        _two_way(kalshi, "k-ml", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-runs", MarketFamily.TOTAL_RUNS, "8.5"),
        _two_way(kalshi, "k-runs-dup", MarketFamily.TOTAL_RUNS, "8.5"),
        _two_way(kalshi, "k-runs-other", MarketFamily.TOTAL_RUNS, "7.5"),
        _two_way(kalshi, "k-runs-int", MarketFamily.TOTAL_RUNS, "8"),
    ]
    right: list[CanonicalMarket | None] = [
        _two_way(polymarket, "pm-ml", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-runs", MarketFamily.TOTAL_RUNS, "8.5"),
        _two_way(polymarket, "pm-runs-other", MarketFamily.TOTAL_RUNS, "9.5"),
        _two_way(game_two, "pm-game2", MarketFamily.GAME_WINNER),
    ]
    return left, right


def _tennis_event(
    venue: VenueName,
    *,
    source_id: str,
    home: str = "Jannik Sinner",
    away: str = "Carlos Alcaraz",
    tournament: str = "US Open",
    round_label: str = "Final",
    event_type: str = TENNIS_EVENT_SINGLES,
) -> CanonicalEvent:
    return _event(
        venue,
        home=home,
        away=away,
        competition="ATP",
        sport=TENNIS_SPORT,
        source_id=source_id,
        tournament=tournament,
        round_label=round_label,
        event_type=event_type,
    )


def _tennis_case() -> tuple[list[CanonicalMarket | None], list[CanonicalMarket | None]]:
    kalshi = _tennis_event(VenueName.KALSHI, source_id="tennis-k")
    polymarket = _tennis_event(VenueName.POLYMARKET, source_id="tennis-pm")
    reversed_order = _tennis_event(
        VenueName.POLYMARKET,
        source_id="tennis-reversed",
        home="Carlos Alcaraz",
        away="Jannik Sinner",
    )
    other_round = _tennis_event(VenueName.POLYMARKET, source_id="tennis-sf", round_label="Semifinal")
    left: list[CanonicalMarket | None] = [
        _two_way(kalshi, "k-ml", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(kalshi, "k-lined", MarketFamily.GAME_WINNER, "1.5"),
    ]
    right: list[CanonicalMarket | None] = [
        _two_way(polymarket, "pm-ml", MarketFamily.GAME_WINNER),
        _two_way(polymarket, "pm-ml-dup", MarketFamily.GAME_WINNER),
        _two_way(reversed_order, "pm-reversed", MarketFamily.GAME_WINNER),
        _two_way(other_round, "pm-sf", MarketFamily.GAME_WINNER),
    ]
    return left, right


def test_nba_pairing_matches_cartesian_oracle() -> None:
    left, right = _nba_case()
    oracle = _prove_pairing(left, right)
    matcher = MarketMatcher()
    assert matcher.match(left[0], right[0]).matched is True
    assert matcher.match(left[3], right[3]).matched is False
    assert matcher.match(left[0], right[7]).matched is False
    assert matcher.match(left[7], right[4]).matched is False
    selected = [(item[0], item[1]) for item in oracle]
    assert selected == [(0, 0), (1, 1)]
    assert canonical_key_for_market(left[3]) == canonical_key_for_market(right[3])
    assert canonical_key_for_market(left[6]) is None
    assert canonical_key_for_market(left[8]) is None


def test_ncaab_pairing_matches_cartesian_oracle() -> None:
    left, right = _ncaab_case()
    oracle = _prove_pairing(left, right)
    matcher = MarketMatcher()
    assert canonical_key_for_market(left[0]) is not None
    assert canonical_key_for_market(left[0]) == canonical_key_for_market(right[0])
    assert canonical_key_for_market(left[2]) == canonical_key_for_market(right[2])
    assert matcher.match(left[0], right[0]).matched is False
    assert matcher.match(left[2], right[2]).matched is False
    assert matcher.match(left[4], right[4]).matched is False
    assert oracle == []
    assert canonical_key_for_market(left[3]) is None


def test_mlb_pairing_matches_cartesian_oracle() -> None:
    """Structural MLB Game Winner and exact x.5 totals pair. Mismatches stay out."""

    left, right = _mlb_case()
    oracle = _prove_pairing(left, right)
    matcher = MarketMatcher()
    assert canonical_key_for_market(left[0]) is not None
    assert canonical_key_for_market(left[0]) == canonical_key_for_market(right[0])
    assert canonical_key_for_market(left[2]) == canonical_key_for_market(right[2])
    assert canonical_key_for_market(left[2]) != canonical_key_for_market(left[4])
    assert matcher.match(left[0], right[0]).matched is True
    assert matcher.match(left[2], right[2]).matched is True
    assert matcher.match(left[0], right[4]).matched is False
    assert matcher.match(left[4], right[3]).matched is False
    assert [(item[0], item[1]) for item in oracle] == [(0, 0), (1, 1), (2, 2)]
    assert canonical_key_for_market(left[5]) is None


def test_tennis_pairing_matches_cartesian_oracle() -> None:
    left, right = _tennis_case()
    oracle = _prove_pairing(left, right)
    matcher = MarketMatcher()
    direct = matcher.match(left[0], right[0])
    assert direct.matched is True
    reversed_pair = matcher.match(left[0], right[2])
    assert reversed_pair.matched is True
    assert matcher.match(left[0], right[3]).matched is False
    assert canonical_key_for_market(left[2]) is None
    selected = [(item[0], item[1]) for item in oracle]
    assert (0, 0) in selected
    assert (1, 1) in selected
    assert all(left_index != 2 for left_index, _right_index in selected)
    assert all(right_index != 3 for _left_index, right_index in selected)


def test_nfl_pairing_matches_cartesian_oracle() -> None:
    left, right, polymarket = _nfl_case()
    _prove_pairing(left, right)
    _prove_pairing(left, polymarket)
    _prove_pairing(polymarket, right)
    matcher = MarketMatcher()
    chosen = greedy_unique_market_matches(left, polymarket, matcher)
    paired = {
        (left[i].source_market_id, polymarket[j].source_market_id) for i, j, _match in chosen
    }
    assert ("mb-ml", "pm-ml") in paired
    assert ("mb-spread", "pm-spread") in paired
    assert ("mb-total", "pm-total") in paired
    assert matcher.match(left[3], right[1]).matched is False


def test_duplicate_indexes_keep_left_major_diagonal() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    kalshi = _event(VenueName.KALSHI)
    left = [_hda(matchbook, "a"), _hda(matchbook, "b")]
    right = [_hda(kalshi, "c"), _hda(kalshi, "d")]
    chosen = greedy_unique_market_matches(left, right, MarketMatcher())
    assert [(item[0], item[1]) for item in chosen] == [(0, 0), (1, 1)]


def test_kalshi_attachment_skips_an_earlier_non_equivalent_leg() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    other = _event(VenueName.KALSHI, home="Arsenal", away="Chelsea", source_id="other")
    kalshi = _event(VenueName.KALSHI)
    rows = assemble_fixture_inventory(
        [_inventory(_hda(matchbook, "mb-1x2"), name="1X2", source_id="mb-1x2", venue=VenueName.MATCHBOOK)],
        [],
        kalshi_markets=[
            _inventory(
                _hda(other, "k-other"),
                name="other",
                source_id="k-other",
                venue=VenueName.KALSHI,
            ),
            _inventory(
                _hda(kalshi, "k-1x2"),
                name="1X2",
                source_id="k-1x2",
                venue=VenueName.KALSHI,
            ),
        ],
    )
    attached = [row for row in rows if row.matchbook and row.matchbook.source_market_id == "mb-1x2"]
    assert len(attached) == 1
    assert attached[0].kalshi is not None
    assert attached[0].kalshi.source_market_id == "k-1x2"


def test_priority_baseline_wins_the_shared_slot() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    kalshi = _event(VenueName.KALSHI)
    non_priority = _settlement(
        scope=SettlementScope.UNKNOWN,
        extra_time=None,
        penalties=None,
        period=FootballPeriod.UNKNOWN,
        unknown_reason="listed extra time wording",
    )
    left = [_hda(matchbook, "mb-baseline")]
    right = [
        _hda(kalshi, "k-non-priority", settlement=non_priority),
        _hda(kalshi, "k-baseline"),
    ]
    chosen = greedy_unique_market_matches(left, right, MarketMatcher(), priority_pair=PRIORITY)
    assert len(chosen) == 1
    assert chosen[0][1] == 1
    assert right[chosen[0][1]].source_market_id == "k-baseline"


def test_collector_wrapper_matches_cartesian_oracle() -> None:
    left_markets, right_markets, _polymarket = _football_case()
    left = [_NormalizedMarket({}, market) for market in left_markets]
    right = [_NormalizedMarket({}, market) for market in right_markets]
    matcher = MarketMatcher()
    wrapped = _greedy_unique_market_pairs(left, right, matcher=matcher)
    oracle = _cartesian_unique(
        left_markets,
        right_markets,
        matcher,
        priority_pair=PRIORITY,
    )
    assert [
        (item[0].canonical.source_market_id, item[1].canonical.source_market_id, item[2].model_dump())
        for item in wrapped
    ] == [
        (left_markets[i].source_market_id, right_markets[j].source_market_id, match.model_dump())
        for i, j, match in oracle
    ]


def test_memo_matches_uncached_results_and_isolates_callers() -> None:
    left, right, _polymarket = _football_case()
    matcher = MarketMatcher()
    pairs = [(left[index % len(left)], right[index % len(right)]) for index in range(len(left) * len(right))]
    plain = [matcher.match(item_left, item_right).model_dump() for item_left, item_right in pairs]
    with memoize_market_matches():
        cached = [matcher.match(item_left, item_right).model_dump() for item_left, item_right in pairs]
        mutated = matcher.match(left[0], right[1])
        mutated.reasons.append("caller_mutation")
        mutated.provenance.applied_rule_ids.append("caller_mutation")
        again = matcher.match(left[0], right[1])
    assert cached == plain
    assert "caller_mutation" not in again.reasons
    assert "caller_mutation" not in again.provenance.applied_rule_ids
    assert again.model_dump() == matcher.match(left[0], right[1]).model_dump()


def test_inventory_rows_match_across_repeats_and_keep_absent_legs() -> None:
    left, right, polymarket = _football_case()
    matchbook = [
        _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
        for market in left
    ]
    kalshi = [
        _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
        for market in right
        if market.event.home_team == "Tottenham"
    ]
    polymarket_rows = [
        _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
        for market in polymarket
    ]
    polymarket_rows.append(
        _inventory(None, name="tombstone", source_id="pm-absent", venue=VenueName.POLYMARKET)
    )
    first = assemble_fixture_inventory(matchbook, polymarket_rows, kalshi_markets=kalshi)
    second = assemble_fixture_inventory(matchbook, polymarket_rows, kalshi_markets=kalshi)
    assert [_row_signature(row) for row in first] == [_row_signature(row) for row in second]
    absent = [row for row in first if row.polymarket and row.polymarket.source_market_id == "pm-absent"]
    assert len(absent) == 1
    assert absent[0].comparison_status is InventoryComparisonStatus.UNSUPPORTED_FAMILY
    paired = {
        (
            None if row.matchbook is None else row.matchbook.source_market_id,
            None if row.kalshi is None else row.kalshi.source_market_id,
            row.comparison_status,
        )
        for row in first
    }
    assert ("mb-1x2", "k-1x2", InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT) in paired or (
        "mb-1x2",
        "k-1x2",
        InventoryComparisonStatus.MATCHED_EQUIVALENT,
    ) in paired
    assert any(row.kalshi and row.kalshi.source_market_id == "k-btts" for row in first)
    assert any(row.kalshi and row.kalshi.source_market_id == "k-ftts" for row in first)
    assert any(row.kalshi and row.kalshi.source_market_id == "k-tg-25" for row in first)
    integer_rows = [
        row
        for row in first
        if row.matchbook is not None and row.matchbook.source_market_id == "mb-tg-int"
    ]
    assert integer_rows
    assert all(
        not inventory_is_comparable_opportunity(row.comparison_status) for row in integer_rows
    )


def _heavy_football(count: int) -> tuple[list[CanonicalMarket], list[CanonicalMarket]]:
    matchbook = _event(VenueName.MATCHBOOK, source_id="heavy-mb")
    kalshi = _event(VenueName.KALSHI, source_id="heavy-k")
    left: list[CanonicalMarket] = []
    right: list[CanonicalMarket] = []
    families = (
        (_hda, None),
        (_btts, None),
        (_ftts, None),
    )
    for index in range(count):
        for builder, _line in families:
            left.append(builder(matchbook, f"mb-{builder.__name__}-{index}"))
            right.append(builder(kalshi, f"k-{builder.__name__}-{index}"))
        line = Decimal(index) + Decimal("0.5")
        left.append(_total(matchbook, f"mb-tg-{index}", str(line)))
        right.append(_total(kalshi, f"k-tg-{index}", str(line)))
        left.append(
            _total(matchbook, f"mb-int-{index}", str(Decimal(index)))
        )
        right.append(_hda(
            _event(VenueName.KALSHI, home=f"Home {index}", away=f"Away {index}", source_id=f"other-{index}"),
            f"k-other-{index}",
        ))
    return left, right


def _heavy_nfl(count: int) -> tuple[list[CanonicalMarket], list[CanonicalMarket]]:
    matchbook = _event(
        VenueName.MATCHBOOK,
        home="Buffalo Bills",
        away="Kansas City Chiefs",
        competition="NFL",
        sport=NFL_SPORT,
        source_id="nfl-matchbook",
    )
    kalshi = _event(
        VenueName.KALSHI,
        home="Buffalo Bills",
        away="Kansas City Chiefs",
        competition="NFL",
        sport=NFL_SPORT,
        source_id="nfl-kalshi",
    )
    left: list[CanonicalMarket] = []
    right: list[CanonicalMarket] = []
    for index in range(count):
        left.append(_moneyline(matchbook, f"mb-ml-{index}"))
        right.append(_moneyline(kalshi, f"k-ml-{index}"))
        line = Decimal(index) + Decimal("0.5")
        left.append(_spread(matchbook, f"mb-sp-{index}", str(line)))
        right.append(_spread(kalshi, f"k-sp-{index}", str(line)))
        left.append(_points(matchbook, f"mb-tp-{index}", str(line + 20)))
        right.append(_points(kalshi, f"k-tp-{index}", str(line + 20)))
    return left, right


def _legacy_kalshi_index(
    row: object,
    kalshi_markets: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    exclude: set[int],
) -> int | None:
    """Pre-index scan. The production index must choose these same indexes."""

    if row.family is None:  # type: ignore[attr-defined]
        return None
    comparable = inventory_is_comparable_opportunity(row.comparison_status)  # type: ignore[attr-defined]
    first_related: int | None = None
    for index, item in enumerate(kalshi_markets):
        if index in exclude:
            continue
        if not _kalshi_related_to_row(row, item):  # type: ignore[arg-type]
            continue
        matches = _kalshi_match_results(
            row,  # type: ignore[arg-type]
            item,
            matcher,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
        )
        if any(match.matched for match in matches):
            return index
        if comparable:
            continue
        if first_related is None:
            first_related = index
    return first_related


def _attachment_choices(
    rows: list[object],
    kalshi_markets: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    chooser: object,
) -> list[int | None]:
    exclude: set[int] = set()
    chosen: list[int | None] = []
    for row in rows:
        index = chooser(  # type: ignore[operator]
            row,
            kalshi_markets,
            matcher,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
            exclude=exclude,
        )
        chosen.append(index)
        if index is not None:
            exclude.add(index)
    return chosen


def _assert_index_matches_scan(
    rows: list[object],
    kalshi_markets: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
) -> list[int | None]:
    shared = _KalshiAttachmentIndex(
        kalshi_markets,
        matcher,
        _InventorySources(matchbook_markets, polymarket_markets),
    )

    def _choose(
        row: object,
        markets: list[InventoryMarket],
        chooser_matcher: MarketMatcher,
        *,
        matchbook_markets: list[InventoryMarket],
        polymarket_markets: list[InventoryMarket],
        exclude: set[int],
    ) -> int | None:
        del markets, chooser_matcher, matchbook_markets, polymarket_markets
        return shared.select(row, exclude)  # type: ignore[arg-type]

    legacy = _attachment_choices(
        rows,
        kalshi_markets,
        matcher,
        matchbook_markets=matchbook_markets,
        polymarket_markets=polymarket_markets,
        chooser=_legacy_kalshi_index,
    )
    indexed = _attachment_choices(
        rows,
        kalshi_markets,
        matcher,
        matchbook_markets=matchbook_markets,
        polymarket_markets=polymarket_markets,
        chooser=_choose,
    )
    assert indexed == legacy
    return indexed


def _replay_matchbook_inventory(
    matchbook: list[InventoryMarket],
    kalshi: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    pair_decisions: dict[tuple[str, str, str, str], PaperScanDecision] | None = None,
) -> list[object]:
    """Full-scan attachment for canonical Matchbook rows, then the same sort."""

    rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook
        if item.canonical is not None
    ]
    unmatched = [item for item in kalshi if item.canonical is not None]
    attached: set[int] = set()
    for row in rows:
        index = _legacy_kalshi_index(
            row,
            unmatched,
            matcher,
            matchbook_markets=matchbook,
            polymarket_markets=[],
            exclude=attached,
        )
        if index is None:
            continue
        attached.add(index)
        _attach_kalshi(
            row,
            unmatched[index],
            matcher=matcher,
            matchbook_markets=matchbook,
            polymarket_markets=[],
            pair_decisions=pair_decisions or {},
            venue_costs=None,
            fx_snapshots=None,
            cost_resolver=None,
        )
    for index, item in enumerate(unmatched):
        if index not in attached:
            rows.append(
                _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
            )
    return _sort_rows(rows)


def _inventory_rows(
    markets: list[CanonicalMarket],
) -> list[InventoryMarket]:
    return [
        _inventory(
            market,
            name=market.source_market_id,
            source_id=market.source_market_id,
            venue=market.source_venue,
        )
        for market in markets
    ]


def _nfl_event(venue: VenueName, *, source_id: str, home: str = "Buffalo Bills", away: str = "Kansas City Chiefs") -> CanonicalEvent:
    return _event(
        venue,
        home=home,
        away=away,
        competition="NFL",
        sport=NFL_SPORT,
        source_id=source_id,
    )


def test_source_lookup_returns_the_first_duplicate_source_id() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    first = _inventory(_hda(matchbook, "same"), name="first", source_id="same", venue=VenueName.MATCHBOOK)
    second = _inventory(_btts(matchbook, "same"), name="second", source_id="same", venue=VenueName.MATCHBOOK)
    facts = VenueMarketFacts(
        venue=VenueName.MATCHBOOK,
        source_event_id=matchbook.source_event_id,
        source_market_id="same",
    )
    sources = _InventorySources([first, second], [])
    assert sources.item_for(facts, VenueName.MATCHBOOK, sources.matchbook_markets) is first
    assert _inventory_from_facts(facts, VenueName.MATCHBOOK, [first, second]) is first


def test_later_equivalent_kalshi_beats_an_earlier_related_leg() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    other = _event(VenueName.KALSHI, home="Arsenal", away="Chelsea", source_id="other")
    kalshi = _event(VenueName.KALSHI)
    left = _hda(matchbook, "mb-1x2")
    earlier = _hda(other, "k-related")
    later = _hda(kalshi, "k-equivalent")
    duplicate = _hda(kalshi, "k-duplicate")
    matchbook_rows = _inventory_rows([left])
    kalshi_rows = _inventory_rows([earlier, later, duplicate])
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook_rows
    ]
    chosen = _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        MarketMatcher(),
        matchbook_markets=matchbook_rows,
        polymarket_markets=[],
    )
    assert chosen == [1]
    matcher = MarketMatcher()
    proven = matcher.match(left, later)
    decision = PaperScanDecision(
        market_match=proven,
        solver_model="simple_complete_set",
        eligible_for_paper_simulation=True,
        minimum_net_edge=Decimal("0.01"),
    )
    decisions = {
        (
            VenueName.MATCHBOOK.value,
            "mb-1x2",
            VenueName.KALSHI.value,
            "k-equivalent",
        ): decision
    }
    assembled = assemble_fixture_inventory(
        matchbook_rows,
        [],
        kalshi_markets=kalshi_rows,
        matcher=matcher,
        decisions_by_pair=decisions,
    )
    replayed = _replay_matchbook_inventory(
        matchbook_rows,
        kalshi_rows,
        matcher,
        pair_decisions=decisions,
    )
    assert [_row_signature(row) for row in assembled] == [_row_signature(row) for row in replayed]
    attached = next(row for row in assembled if row.matchbook and row.matchbook.source_market_id == "mb-1x2")
    assert attached.kalshi is not None
    assert attached.kalshi.source_market_id == "k-equivalent"
    assert inventory_is_comparable_opportunity(attached.comparison_status)
    assert attached.entered_solver is True
    assert attached.solver_model == "simple_complete_set"
    assert attached.trigger_net_edge == Decimal("0.01")
    assert attached.pair_results
    assert proven.reasons
    assert all(reason in attached.match_reasons for reason in proven.reasons)
    leftover_ids = [
        row.kalshi.source_market_id
        for row in assembled
        if row.matchbook is None and row.kalshi is not None
    ]
    assert leftover_ids == ["k-related", "k-duplicate"]


def test_duplicate_equivalent_kalshi_legs_stay_in_list_order() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    kalshi = _event(VenueName.KALSHI)
    matchbook_rows = _inventory_rows([
        _hda(matchbook, "mb-a"),
        _hda(matchbook, "mb-b"),
    ])
    kalshi_rows = _inventory_rows([
        _hda(kalshi, "k-0"),
        _hda(kalshi, "k-1"),
        _hda(kalshi, "k-2"),
    ])
    matcher = MarketMatcher()
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook_rows
    ]
    assert _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=[],
    ) == [0, 1]
    assembled = assemble_fixture_inventory(matchbook_rows, [], kalshi_markets=kalshi_rows, matcher=matcher)
    replayed = _replay_matchbook_inventory(matchbook_rows, kalshi_rows, matcher)
    assert [_row_signature(row) for row in assembled] == [_row_signature(row) for row in replayed]
    attached = {
        row.matchbook.source_market_id: row.kalshi.source_market_id
        for row in assembled
        if row.matchbook is not None and row.kalshi is not None
    }
    assert attached == {"mb-a": "k-0", "mb-b": "k-1"}


def test_exact_line_match_accepts_equivalent_decimal_text() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    other = _event(VenueName.KALSHI, home="Arsenal", away="Chelsea", source_id="other-total")
    kalshi = _event(VenueName.KALSHI)
    matchbook_rows = _inventory_rows([_total(matchbook, "mb-tg", "2.5")])
    kalshi_rows = _inventory_rows([
        _total(other, "k-related", "2.50"),
        _total(kalshi, "k-equivalent", "2.50"),
        _total(kalshi, "k-other-line", "3.5"),
    ])
    matcher = MarketMatcher()
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook_rows
    ]
    assert _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=[],
    ) == [1]
    assembled = assemble_fixture_inventory(matchbook_rows, [], kalshi_markets=kalshi_rows, matcher=matcher)
    attached = next(row for row in assembled if row.matchbook is not None)
    assert attached.kalshi is not None
    assert attached.kalshi.source_market_id == "k-equivalent"
    assert attached.line == Decimal("2.5")


def test_already_comparable_matchbook_polymarket_row_keeps_only_equivalent_kalshi() -> None:
    matchbook = _nfl_event(VenueName.MATCHBOOK, source_id="mb-event")
    polymarket = _nfl_event(VenueName.POLYMARKET, source_id="pm-event")
    other = _nfl_event(
        VenueName.KALSHI,
        source_id="other-event",
        home="Dallas Cowboys",
        away="Green Bay Packers",
    )
    kalshi = _nfl_event(VenueName.KALSHI, source_id="k-event")
    mb = _moneyline(matchbook, "mb-ml")
    pm = _moneyline(polymarket, "pm-ml")
    related = _moneyline(other, "k-related")
    equivalent = _moneyline(kalshi, "k-equivalent")
    matcher = MarketMatcher()
    direct = matcher.match(mb, pm)
    assert direct.matched is True
    assert matcher.match(mb, related).matched is False
    assert matcher.match(mb, equivalent).matched is True
    matchbook_rows = _inventory_rows([mb])
    polymarket_rows = _inventory_rows([pm])
    related_only = _inventory_rows([related])
    blocked = assemble_fixture_inventory(
        matchbook_rows,
        polymarket_rows,
        kalshi_markets=related_only,
        matcher=matcher,
    )
    paired = next(row for row in blocked if row.matchbook is not None and row.polymarket is not None)
    assert inventory_is_comparable_opportunity(paired.comparison_status)
    assert paired.kalshi is None
    assert any(row.kalshi is not None and row.kalshi.source_market_id == "k-related" for row in blocked)
    pre_rows = [
        row
        for row in assemble_fixture_inventory(matchbook_rows, polymarket_rows, matcher=matcher)
        if row.matchbook is not None
    ]
    assert _assert_index_matches_scan(
        pre_rows,
        related_only,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=polymarket_rows,
    ) == [None]
    both = _inventory_rows([related, equivalent])
    assert _assert_index_matches_scan(
        pre_rows,
        both,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=polymarket_rows,
    ) == [1]
    attached_rows = assemble_fixture_inventory(
        matchbook_rows,
        polymarket_rows,
        kalshi_markets=both,
        matcher=matcher,
    )
    attached = next(
        row for row in attached_rows if row.matchbook is not None and row.polymarket is not None
    )
    assert attached.kalshi is not None
    assert attached.kalshi.source_market_id == "k-equivalent"
    assert attached.comparison_status == paired.comparison_status
    assert direct.confidence == matcher.match(mb, equivalent).confidence
    assert all(reason in attached.match_reasons for reason in matcher.match(mb, equivalent).reasons)
    assert any(
        row.matchbook is None and row.kalshi is not None and row.kalshi.source_market_id == "k-related"
        for row in attached_rows
    )


def test_polymarket_only_row_pairs_with_the_first_equivalent_kalshi() -> None:
    polymarket = _nfl_event(VenueName.POLYMARKET, source_id="pm-event")
    other = _nfl_event(
        VenueName.KALSHI,
        source_id="other-event",
        home="Dallas Cowboys",
        away="Green Bay Packers",
    )
    kalshi = _nfl_event(VenueName.KALSHI, source_id="k-event")
    pm = _moneyline(polymarket, "pm-ml")
    related = _moneyline(other, "k-related")
    equivalent = _moneyline(kalshi, "k-equivalent")
    polymarket_rows = _inventory_rows([pm])
    kalshi_rows = _inventory_rows([related, equivalent])
    matcher = MarketMatcher()
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in polymarket_rows
    ]
    assert _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        matcher,
        matchbook_markets=[],
        polymarket_markets=polymarket_rows,
    ) == [1]
    rows = assemble_fixture_inventory([], polymarket_rows, kalshi_markets=kalshi_rows, matcher=matcher)
    attached = next(row for row in rows if row.polymarket and row.polymarket.source_market_id == "pm-ml")
    assert attached.matchbook is None
    assert attached.kalshi is not None
    assert attached.kalshi.source_market_id == "k-equivalent"
    proven = matcher.match(pm, equivalent)
    assert proven.matched is True
    assert inventory_is_comparable_opportunity(attached.comparison_status)
    assert attached.match_reasons
    assert all(reason in attached.match_reasons for reason in proven.reasons)
    assert any(
        row.polymarket is None and row.kalshi is not None and row.kalshi.source_market_id == "k-related"
        for row in rows
    )


def test_no_valid_kalshi_attachment_leaves_the_row_and_leg_separate() -> None:
    matchbook = _event(VenueName.MATCHBOOK)
    kalshi = _event(VenueName.KALSHI)
    matchbook_rows = _inventory_rows([_total(matchbook, "mb-int", "2")])
    kalshi_rows = _inventory_rows([
        _total(kalshi, "k-half", "2.5"),
        _hda(kalshi, "k-1x2"),
    ])
    matcher = MarketMatcher()
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook_rows
    ]
    assert _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=[],
    ) == [None]
    assembled = assemble_fixture_inventory(matchbook_rows, [], kalshi_markets=kalshi_rows, matcher=matcher)
    replayed = _replay_matchbook_inventory(matchbook_rows, kalshi_rows, matcher)
    assert [_row_signature(row) for row in assembled] == [_row_signature(row) for row in replayed]
    integer_row = next(row for row in assembled if row.matchbook is not None)
    assert integer_row.kalshi is None
    assert {row.kalshi.source_market_id for row in assembled if row.kalshi is not None} == {
        "k-half",
        "k-1x2",
    }


def test_custom_matcher_does_not_reuse_equivalent_fingerprints() -> None:
    class _OnlyNamedLeg(MarketMatcher):
        def match(self, left: CanonicalMarket, right: CanonicalMarket) -> MarketMatchResult:
            result = super().match(left, right)
            if right.source_market_id != "k-keep":
                return result.model_copy(update={"matched": False})
            return result

    matchbook = _event(VenueName.MATCHBOOK)
    kalshi = _event(VenueName.KALSHI)
    matchbook_rows = _inventory_rows([_hda(matchbook, "mb-1x2")])
    kalshi_rows = _inventory_rows([
        _hda(kalshi, "k-skip"),
        _hda(kalshi, "k-keep"),
    ])
    matcher = _OnlyNamedLeg()
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook_rows
    ]
    assert _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=[],
    ) == [1]
    assembled = assemble_fixture_inventory(matchbook_rows, [], kalshi_markets=kalshi_rows, matcher=matcher)
    replayed = _replay_matchbook_inventory(matchbook_rows, kalshi_rows, matcher)
    assert [_row_signature(row) for row in assembled] == [_row_signature(row) for row in replayed]
    attached = next(row for row in assembled if row.matchbook is not None)
    assert attached.kalshi is not None
    assert attached.kalshi.source_market_id == "k-keep"


def test_dense_kalshi_attachment_matches_full_scan_order() -> None:
    left, right = _heavy_football(80)
    matchbook_rows = _inventory_rows(left)
    kalshi_rows = _inventory_rows(right)
    matcher = MarketMatcher()
    pre_rows = [
        _venue_only_row(item, venue_costs=None, fx_snapshots=None, cost_resolver=None)
        for item in matchbook_rows
    ]
    chosen = _assert_index_matches_scan(
        pre_rows,
        kalshi_rows,
        matcher,
        matchbook_markets=matchbook_rows,
        polymarket_markets=[],
    )
    assert any(index is not None for index in chosen)
    assert chosen.count(None) == 80
    assembled = assemble_fixture_inventory(
        matchbook_rows,
        [],
        kalshi_markets=kalshi_rows,
        matcher=matcher,
    )
    replayed = _replay_matchbook_inventory(matchbook_rows, kalshi_rows, matcher)
    assert [_row_signature(row) for row in assembled] == [_row_signature(row) for row in replayed]
    order = [
        (
            None if row.matchbook is None else row.matchbook.source_market_id,
            None if row.kalshi is None else row.kalshi.source_market_id,
            row.comparison_status,
        )
        for row in assembled
    ]
    assert order == [
        (
            None if row.matchbook is None else row.matchbook.source_market_id,
            None if row.kalshi is None else row.kalshi.source_market_id,
            row.comparison_status,
        )
        for row in replayed
    ]


def _subphase_max_ms(name: str) -> int:
    snapshot = loop_subphase_snapshot()
    rows = snapshot["subphases"]
    assert isinstance(rows, list)
    found = [row for row in rows if isinstance(row, dict) and row.get("subphase") == name]
    if not found:
        return 0
    return max(int(row["max_ms"]) for row in found)


async def _profiled(work) -> CallbackProfiler:  # type: ignore[no-untyped-def]
    """Time ``work`` as its own callback.

    The profiler patches ``Handle._run`` when the context opens. Work that
    runs inside the already-started test callback is invisible to it, so the
    measured coroutine starts on the next turn. The 250ms failure line is
    unchanged. Callbacks of at least 1ms are retained so a miss cannot report
    a longest slice of zero.
    """

    reset_loop_activity()
    profiler = CallbackProfiler(record_over_s=0.001, sample_after_s=0.2)
    with profiler:
        await asyncio.sleep(0)
        await asyncio.create_task(work())
    assert profiler.profile.callbacks > 0
    return profiler


def _assert_slice(profiler: CallbackProfiler) -> None:
    profile = profiler.profile
    assert profile.longest_non_gc_s < SLICE_BOUND_S, (
        f"non-gc slice {profile.longest_non_gc_s:.3f}s "
        f"longest {profile.longest_s:.3f}s iteration {profile.longest_iteration_s:.3f}s"
    )


@pytest.mark.asyncio
async def test_heavy_football_fixture_stays_under_slice_sla() -> None:
    left, right = _heavy_football(40)
    assert len(left) >= 200
    assert len(right) >= 200

    async def work() -> None:
        matcher = MarketMatcher()
        with sync_subphase("universe", "market_relationships", events=1, candidates=len(left) + len(right)):
            greedy_unique_market_matches(left, right, matcher, priority_pair=PRIORITY)
        matchbook = [
            _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
            for market in left
        ]
        kalshi = [
            _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
            for market in right
        ]
        with sync_subphase(
            "universe",
            "inventory_composition",
            events=1,
            candidates=len(matchbook) + len(kalshi),
        ):
            assemble_fixture_inventory(matchbook, [], kalshi_markets=kalshi, matcher=matcher)

    profiler = await _profiled(work)
    _assert_slice(profiler)
    assert _subphase_max_ms("market_relationships") < 250
    assert _subphase_max_ms("inventory_composition") < 250


@pytest.mark.asyncio
async def test_heavy_nfl_and_dense_equivalence_stay_under_slice_sla() -> None:
    nfl_left, nfl_right = _heavy_nfl(80)
    dense_left, dense_right = _heavy_football(80)
    assert len(dense_left) >= 400

    async def work() -> None:
        matcher = MarketMatcher()
        with sync_subphase("universe", "market_relationships", events=1, candidates=len(nfl_left)):
            greedy_unique_market_matches(nfl_left, nfl_right, matcher, priority_pair=PRIORITY)
        with sync_subphase("universe", "market_relationships", events=1, candidates=len(dense_left)):
            greedy_unique_market_matches(dense_left, dense_right, matcher, priority_pair=PRIORITY)
        rows = [
            _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
            for market in dense_left
        ]
        other = [
            _inventory(market, name=market.source_market_id, source_id=market.source_market_id, venue=market.source_venue)
            for market in dense_right
        ]
        with sync_subphase("universe", "inventory_composition", events=1, candidates=len(rows) + len(other)):
            assemble_fixture_inventory(rows, [], kalshi_markets=other, matcher=matcher)

    profiler = await _profiled(work)
    _assert_slice(profiler)
    assert _subphase_max_ms("market_relationships") < 250
    assert _subphase_max_ms("inventory_composition") < 250


@pytest.mark.asyncio
async def test_representative_275_fixtures_keep_heartbeat_under_250ms() -> None:
    fixtures = []
    for index in range(275):
        matchbook = _event(VenueName.MATCHBOOK, source_id=f"mb-{index}")
        kalshi = _event(VenueName.KALSHI, source_id=f"k-{index}")
        left = [
            _hda(matchbook, f"mb-1x2-{index}"),
            _btts(matchbook, f"mb-btts-{index}"),
            _ftts(matchbook, f"mb-ftts-{index}"),
            _total(matchbook, f"mb-tg-{index}", "2.5"),
        ]
        right = [
            _hda(kalshi, f"k-1x2-{index}"),
            _btts(kalshi, f"k-btts-{index}"),
            _ftts(kalshi, f"k-ftts-{index}"),
            _total(kalshi, f"k-tg-{index}", "2.5"),
        ]
        fixtures.append((left, right))
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        previous = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append(now - previous)
            previous = now

    async def work() -> None:
        pulse = asyncio.create_task(heartbeat())
        matcher = MarketMatcher()
        try:
            for left, right in fixtures:
                with sync_subphase(
                    "universe",
                    "market_relationships",
                    events=1,
                    candidates=len(left) + len(right),
                ):
                    greedy_unique_market_matches(left, right, matcher, priority_pair=PRIORITY)
                matchbook = [
                    _inventory(
                        market,
                        name=market.source_market_id,
                        source_id=market.source_market_id,
                        venue=market.source_venue,
                    )
                    for market in left
                ]
                kalshi = [
                    _inventory(
                        market,
                        name=market.source_market_id,
                        source_id=market.source_market_id,
                        venue=market.source_venue,
                    )
                    for market in right
                ]
                with sync_subphase(
                    "universe",
                    "inventory_composition",
                    events=1,
                    candidates=len(matchbook) + len(kalshi),
                ):
                    assemble_fixture_inventory(matchbook, [], kalshi_markets=kalshi, matcher=matcher)
                await asyncio.sleep(0)
        finally:
            stop.set()
            await pulse

    profiler = await _profiled(work)
    _assert_slice(profiler)
    assert gaps
    assert max(gaps) < SLICE_BOUND_S
    assert _subphase_max_ms("market_relationships") < 250
    assert _subphase_max_ms("inventory_composition") < 250
