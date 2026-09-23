from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.arbitrage.models import ExecutableQuote
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
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
from sports_hedge.liquidity.book import BookLevel, OrderBookWalker
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.text import AliasRegistry, normalize_text


def _event(venue: VenueName, home: str = "newcastle united", away: str = "chelsea") -> CanonicalEvent:
    return CanonicalEvent(
        competition="premier league",
        home_team=home,
        away_team=away,
        kickoff_utc=datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
        source_venue=venue,
        source_event_id=f"{venue}-event",
    )


def _market(venue: VenueName, *, extra_time: bool = False) -> CanonicalMarket:
    scope = SettlementScope.INCLUDING_EXTRA_TIME if extra_time else SettlementScope.REGULATION_TIME
    return CanonicalMarket(
        event=_event(venue),
        source_venue=venue,
        source_market_id=f"{venue}-market",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=SettlementFingerprint(
            scope=scope,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=extra_time,
            penalties_included=False,
        ),
        runners=[
            CanonicalRunner(source_runner_id="h", outcome=CanonicalOutcome.HOME, label="Home"),
            CanonicalRunner(source_runner_id="d", outcome=CanonicalOutcome.DRAW, label="Draw"),
            CanonicalRunner(source_runner_id="a", outcome=CanonicalOutcome.AWAY, label="Away"),
        ],
    )


def test_text_normalization_and_explicit_aliases() -> None:
    assert normalize_text("Atlético-Madrid") == "atletico madrid"
    aliases = AliasRegistry()
    aliases.add("Man Utd", "Manchester United")
    assert aliases.resolve("MAN UTD") == "manchester united"


def test_event_matcher_tolerates_small_kickoff_offset() -> None:
    left = _event(VenueName.MATCHBOOK)
    right = _event(VenueName.POLYMARKET)
    right.kickoff_utc += timedelta(minutes=1)

    result = EventMatcher().match(left, right)
    assert result.matched is True
    assert result.confidence >= 0.92


def test_market_matcher_rejects_different_settlement_semantics() -> None:
    regulation = _market(VenueName.MATCHBOOK, extra_time=False)
    including_extra_time = _market(VenueName.POLYMARKET, extra_time=True)

    result = MarketMatcher().match(regulation, including_extra_time)
    assert result.matched is False
    assert "settlement_mismatch" not in result.reasons
    assert "not_registered" in result.reasons


def test_two_way_arbitrage_is_depth_capped() -> None:
    quotes = [
        ExecutableQuote(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="m1",
            net_decimal_odds=Decimal("2.10"),
            max_stake=Decimal("100"),
        ),
        ExecutableQuote(
            outcome="no",
            venue=VenueName.POLYMARKET,
            source_market_id="m2",
            net_decimal_odds=Decimal("2.10"),
            max_stake=Decimal("100"),
        ),
    ]

    result = CompleteSetArbitrageSolver().solve(quotes)
    assert result.is_arbitrage is True
    assert result.total_stake == Decimal("200.0000000000000000000000000")
    assert result.guaranteed_profit == Decimal("10.0000000000000000000000000")
    assert result.roi == Decimal("0.05")


def test_three_way_arbitrage_respects_capital_limit() -> None:
    quotes = [
        ExecutableQuote(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="m1",
            net_decimal_odds=Decimal("3.2"),
            max_stake=Decimal("1000"),
        ),
        ExecutableQuote(
            outcome="draw",
            venue=VenueName.POLYMARKET,
            source_market_id="m2",
            net_decimal_odds=Decimal("3.5"),
            max_stake=Decimal("1000"),
        ),
        ExecutableQuote(
            outcome="away",
            venue=VenueName.MATCHBOOK,
            source_market_id="m1",
            net_decimal_odds=Decimal("3.8"),
            max_stake=Decimal("1000"),
        ),
    ]

    result = CompleteSetArbitrageSolver().solve(quotes, capital_limit=Decimal("300"))
    assert result.is_arbitrage is True
    assert result.total_stake == Decimal("300")
    assert result.guaranteed_profit > Decimal("48")
    assert len(result.stakes) == 3


def test_two_way_arbitrage_respects_matchbook_venue_capital() -> None:
    quotes = [
        ExecutableQuote(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="m1",
            net_decimal_odds=Decimal("2.10"),
            max_stake=Decimal("1000"),
        ),
        ExecutableQuote(
            outcome="no",
            venue=VenueName.POLYMARKET,
            source_market_id="m2",
            net_decimal_odds=Decimal("2.10"),
            max_stake=Decimal("1000"),
        ),
    ]
    unconstrained = CompleteSetArbitrageSolver().solve(quotes)
    capped = CompleteSetArbitrageSolver().solve(
        quotes,
        venue_capital_limits={VenueName.MATCHBOOK: Decimal("20"), VenueName.POLYMARKET: Decimal("1000")},
    )
    assert unconstrained.is_arbitrage is True
    assert capped.is_arbitrage is True
    matchbook_stake = next(stake.stake for stake in capped.stakes if stake.venue is VenueName.MATCHBOOK)
    assert matchbook_stake == Decimal("20")
    assert capped.total_stake < unconstrained.total_stake


def test_smarkets_standing_pool_does_not_cap_solver() -> None:
    quotes = [
        ExecutableQuote(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="m1",
            net_decimal_odds=Decimal("2.10"),
            max_stake=Decimal("100"),
        ),
        ExecutableQuote(
            outcome="no",
            venue=VenueName.SMARKETS,
            source_market_id="m2",
            net_decimal_odds=Decimal("2.10"),
            max_stake=Decimal("100"),
        ),
    ]
    unconstrained = CompleteSetArbitrageSolver().solve(quotes)
    ignored_pool = CompleteSetArbitrageSolver().solve(
        quotes,
        venue_capital_limits={VenueName.SMARKETS: Decimal("1")},
    )
    assert unconstrained.is_arbitrage is True
    assert ignored_pool.is_arbitrage is True
    assert ignored_pool.total_stake == unconstrained.total_stake


def test_order_book_walker_uses_multiple_levels() -> None:
    fill = OrderBookWalker().fill(
        [
            BookLevel(decimal_odds=Decimal("2.10"), available_stake=Decimal("50")),
            BookLevel(decimal_odds=Decimal("2.05"), available_stake=Decimal("100")),
        ],
        Decimal("120"),
    )

    assert fill.fully_filled is True
    assert fill.levels_consumed == 2
    assert fill.filled_stake == Decimal("120")
    assert fill.worst_odds == Decimal("2.05")
    assert fill.weighted_average_odds == Decimal("2.070833333333333333333333333")
