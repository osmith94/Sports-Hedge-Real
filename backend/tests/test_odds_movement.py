from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint, SettlementScope
from sports_hedge.odds.mapping import map_raw_record
from sports_hedge.odds.models import QuoteType, RawOddsRecord
from sports_hedge.odds.movement import classify_open_close, pair_open_close

RETRIEVED = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2025, 8, 16, 17, 30, tzinfo=UTC)


def _ah_record(*, quote_type: QuoteType, line: Decimal, odds: str, selection: str = "home") -> RawOddsRecord:
    return RawOddsRecord(
        source="synthetic",
        source_market_id=f"ah:{quote_type.value}:{line}:{selection}",
        source_reference=f"ah:{quote_type.value}:{line}:{selection}",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        bookmaker="b365",
        venue="b365",
        market_family=MarketFamily.ASIAN_HANDICAP,
        period=FootballPeriod.FULL_TIME,
        line=line,
        selection=selection,
        decimal_odds=Decimal(odds),
        quote_type=quote_type,
        retrieved_at=RETRIEVED,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=line,
            extra_time_included=False,
            penalties_included=False,
            push_possible=True,
        ),
        semantics_complete=True,
        raw_payload={"research_only": False},
    )


def test_same_line_asian_handicap_emits_price_logit_move() -> None:
    observations = [
        map_raw_record(
            _ah_record(quote_type=QuoteType.OPENING, line=Decimal("-0.5"), odds="1.90")
        ),
        map_raw_record(
            _ah_record(quote_type=QuoteType.CLOSING, line=Decimal("-0.5"), odds="2.05")
        ),
    ]
    classified = classify_open_close(observations)
    assert len(classified.price_moves) == 1
    assert classified.line_shifts == ()
    move = classified.price_moves[0]
    assert move.line == Decimal("-0.5")
    assert move.implied_logit_delta is not None
    assert move.implied_probability_delta != 0
    assert pair_open_close(observations) == list(classified.price_moves)


def test_changed_line_asian_handicap_is_line_shift_not_price_move() -> None:
    observations = [
        map_raw_record(
            _ah_record(quote_type=QuoteType.OPENING, line=Decimal("-0.5"), odds="1.90")
        ),
        map_raw_record(
            _ah_record(quote_type=QuoteType.CLOSING, line=Decimal("-0.75"), odds="1.95")
        ),
    ]
    classified = classify_open_close(observations)
    assert classified.price_moves == ()
    assert pair_open_close(observations) == []
    assert len(classified.line_shifts) == 1
    shift = classified.line_shifts[0]
    assert shift.opening_line == Decimal("-0.5")
    assert shift.closing_line == Decimal("-0.75")
    assert not hasattr(shift, "implied_probability_delta")
    assert not hasattr(shift, "implied_logit_delta")
