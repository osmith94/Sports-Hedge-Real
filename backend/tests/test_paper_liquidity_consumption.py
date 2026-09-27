"""Conservative PAPER depth: a new read is not new liquidity.

Fixture numbers only. No venue client is treated as a replenishment epoch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.active_trade_recovery import (
    consumed_native_by_level,
    odds_token,
    residual_book_levels,
    residual_native,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.trades import (
    OPENING_TRANCHE_ID,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeLeg,
    PaperTradeState,
    PaperTradeTranche,
    PaperTradeTrancheKind,
)


def _level(odds: str, stake: str) -> BookLevel:
    return BookLevel(decimal_odds=Decimal(odds), available_stake=Decimal(stake))


def test_identical_book_keeps_only_unconsumed_residual() -> None:
    assert residual_native(Decimal("200"), Decimal("150")) == Decimal("50")


def test_smaller_displayed_depth_clamps_at_zero() -> None:
    assert residual_native(Decimal("100"), Decimal("150")) == Decimal("0")
    assert residual_native(Decimal("200"), Decimal("200")) == Decimal("0")


def test_larger_displayed_depth_exposes_only_the_increment() -> None:
    assert residual_native(Decimal("300"), Decimal("150")) == Decimal("150")


def test_different_price_levels_are_independent() -> None:
    identity = ("matchbook", "mkt", "runner", "yes")
    consumed = {(*identity, odds_token("2.10")): Decimal("150")}
    levels = residual_book_levels(
        [_level("2.10", "200"), _level("2.40", "80")],
        identity,
        consumed,
    )
    by_odds = {odds_token(level.decimal_odds): level.available_stake for level in levels}
    assert by_odds[odds_token("2.10")] == Decimal("50")
    assert by_odds[odds_token("2.40")] == Decimal("80")


def test_venues_track_consumed_depth_independently() -> None:
    matchbook = ("matchbook", "mkt", "runner", "yes")
    kalshi = ("kalshi", "ticker", "yes", "yes")
    consumed = {(*matchbook, odds_token("2.10")): Decimal("150")}
    left = residual_book_levels([_level("2.10", "200")], matchbook, consumed)
    right = residual_book_levels([_level("2.10", "200")], kalshi, consumed)
    assert left[0].available_stake == Decimal("50")
    assert right[0].available_stake == Decimal("200")


def test_snapshot_replay_attributes_partial_fill_to_that_price() -> None:
    when = datetime(2026, 9, 27, tzinfo=UTC)
    snapshot = (
        '{"snapshot_id":"snap-1","legs":[{"venue":"matchbook","outcome":"yes",'
        '"native_market_id":"mkt","native_runner_id":"runner",'
        '"levels":[{"odds":"2.10","depth":"200"}]}]}'
    )
    trade = PaperTrade(
        trade_id="trade-1",
        opportunity_id="opp-1",
        state=PaperTradeState.OPEN,
        opened_at=when,
        last_updated_at=when,
        legs=[
            PaperTradeLeg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                currency="GBP",
                requested_stake=Decimal("150"),
                filled_stake=Decimal("150"),
                displayed_odds=Decimal("2.10"),
                filled_odds=Decimal("2.10"),
                source_market_id="mkt",
                source_runner_id="runner",
                tranche_id=OPENING_TRANCHE_ID,
            )
        ],
        tranches=[
            PaperTradeTranche(
                tranche_id=OPENING_TRANCHE_ID,
                sequence=1,
                kind=PaperTradeTrancheKind.OPENING,
                occurred_at=when,
                capital_locked_gbp=Decimal("150"),
                idempotency_key="snap-1",
                execution_snapshot_id="snap-1",
            )
        ],
        audit=[
            PaperTradeAuditEvent(
                occurred_at=when,
                event_type=PaperTradeAuditEventType.EXECUTION_SNAPSHOT,
                detail=snapshot,
            )
        ],
    )
    consumed = consumed_native_by_level(trade)
    key = ("matchbook", "mkt", "runner", "yes", odds_token("2.10"))
    assert consumed[key] == Decimal("150")
    again = residual_book_levels([_level("2.10", "200")], key[:4], consumed)
    assert again[0].available_stake == Decimal("50")
    grown = residual_book_levels([_level("2.10", "300")], key[:4], consumed)
    assert grown[0].available_stake == Decimal("150")
    exhausted = residual_book_levels([_level("2.10", "200")], key[:4], consumed)
    # second identical read after the residual 50 is still only the residual
    assert exhausted[0].available_stake == Decimal("50")
