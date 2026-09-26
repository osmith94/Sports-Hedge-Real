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
absent Matchbook rule were captured as different exceptional contracts. Owner
decision 2026-09-26: those differences do not block PAPER comparison of a
structurally identical singles Match Winner. Live execution stays disabled.
The historical reason string remains readable on old audit rows.
"""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.tennis.constants import TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT


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
    """Retirement/walkover differences do not block PAPER comparison.

    Structural identity, singles, tournament, round, and two-outcome Match
    Winner checks stay in the tennis register. Live execution stays disabled.
    The historical reason string remains defined for old audit rows.
    """

    del left, right
    return None
