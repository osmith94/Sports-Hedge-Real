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
    assemble_fixture_inventory,
    inventory_is_comparable_opportunity,
)
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
from sports_hedge.nfl.constants import NFL_SPORT
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
) -> CanonicalEvent:
    return CanonicalEvent(
        sport=sport,
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id or f"{venue.value}-event",
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
    _assert_same_pairs(left, right, priority=True)
    _assert_same_pairs(left, right, priority=False)
    _assert_same_pairs(left, polymarket, priority=True)
    _assert_same_pairs(polymarket, right, priority=False)
    matcher = MarketMatcher()
    assert matcher.match(left[0], polymarket[0]).matched is False
    chosen = greedy_unique_market_matches(left, right, matcher, priority_pair=PRIORITY)
    paired = {(left[i].source_market_id, right[j].source_market_id) for i, j, _match in chosen}
    assert ("mb-1x2", "k-1x2") in paired
    assert ("mb-btts", "k-btts") in paired
    assert ("mb-ftts", "k-ftts") in paired
    assert ("mb-tg-25", "k-tg-25") in paired
    assert ("mb-tg-int", "k-tg-int") not in paired
    assert all("k-other" not in right_id for _left_id, right_id in paired)


def test_nfl_pairing_matches_cartesian_oracle() -> None:
    left, right, polymarket = _nfl_case()
    _assert_same_pairs(left, right, priority=True)
    _assert_same_pairs(left, polymarket, priority=False)
    _assert_same_pairs(polymarket, right, priority=False)
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


def _subphase_max_ms(name: str) -> int:
    snapshot = loop_subphase_snapshot()
    rows = snapshot["subphases"]
    assert isinstance(rows, list)
    found = [row for row in rows if isinstance(row, dict) and row.get("subphase") == name]
    if not found:
        return 0
    return max(int(row["max_ms"]) for row in found)


async def _profiled(work) -> CallbackProfiler:  # type: ignore[no-untyped-def]
    reset_loop_activity()
    profiler = CallbackProfiler(record_over_s=0.05, sample_after_s=0.2)
    with profiler:
        await work()
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
