"""Tennis Match Winner settlement. Non-executable until rules are equivalent.

Captured 2026-09-24, read-only, no credentials:

Kalshi ``KXATPMATCH`` / ``KXWTAMATCH`` ``rules_secondary``:
winner resolution requires that a ball has been played. A pre-start walkover,
injury, forfeiture or cancellation resolves to a fair price. A postponement
stays open and closes after the rescheduled match within two weeks.

Polymarket ATP/WTA moneyline descriptions (Gamma events in series 10365/10366):
retirement, default or disqualification after the match begins resolves to the
player who advances. A walkover before the start resolves 50-50. Cancel, tie,
or delay resolves 50-50. The delay window is not stable: one captured market
uses 7 days and another uses 14 days.

Matchbook tennis Moneyline (sport-id 9) event/market payloads contain no
retirement, walkover, void or postponement rule text.

Fair-price versus explicit 50-50, the unstable postponement window, and the
absent Matchbook rule are not the same settlement contract. Match Winner stays
non-executable.
"""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.tennis.constants import TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT
from sports_hedge.tennis.detect import is_tennis_canonical_event, is_tennis_market_family


def tennis_match_winner_settlement() -> SettlementFingerprint:
    return SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FULL_TIME,
        line=None,
        push_possible=False,
        penalties_included=None,
        extra_time_included=None,
        unknown_reason=TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT,
    )


def tennis_executable_block_reason(
    left: CanonicalMarket, right: CanonicalMarket
) -> str | None:
    """Explicit non-executable reason for a tennis match-winner pair."""

    if not is_tennis_canonical_event(left.event) or not is_tennis_canonical_event(right.event):
        return None
    if not is_tennis_market_family(left.family) or not is_tennis_market_family(right.family):
        return None
    if left.family is not MarketFamily.GAME_WINNER or right.family is not MarketFamily.GAME_WINNER:
        return None
    return TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT
