"""Canonical market-result choices for PAPER settlement.

Operator failsafe and auto-settlement share this family → outcome space.
Never infers a winner from elapsed kickoff time. Unsupported families fail closed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.football import (
    CanonicalOutcome,
    MarketFamily,
    NFL_PAPER_MARKET_FAMILIES,
    format_stored_line,
    line_push_possible,
)
from sports_hedge.matching.paper_assumed import LOCKED_PAPER_FAMILIES
from sports_hedge.paper.trades import PaperTrade


PAPER_MANUAL_SETTLEMENT_SOURCE = "manual_operator_settlement"
NFL_EXCEPTIONAL_TIE_BLOCKER = "nfl_exceptional_tie_fail_closed"
UNSUPPORTED_MANUAL_FAMILY = "unsupported_manual_result_family"
UNSUPPORTED_TOTAL_LINE = "unsupported_total_line"
UNSUPPORTED_SPREAD_LINE = "unsupported_spread_line"

_SOCCER_RESULT_FAMILIES = LOCKED_PAPER_FAMILIES
_MANUAL_FAMILIES = LOCKED_PAPER_FAMILIES | NFL_PAPER_MARKET_FAMILIES


class CanonicalResultChoice(BaseModel):
    """One operator-selectable canonical market result."""

    value: str
    label: str
    realised_pnl_gbp: Decimal | None = None


class CanonicalResultSpace(BaseModel):
    """Valid settlement results for a persisted PAPER trade's market family."""

    family: MarketFamily | None = None
    choices: list[CanonicalResultChoice] = Field(default_factory=list)
    unsupported_reason: str | None = None

    @property
    def values(self) -> set[str]:
        return {item.value for item in self.choices}


def manual_settlement_source_id(trade_id: str) -> str:
    return f"manual:{trade_id}"


def family_key(family: MarketFamily | str | None) -> str | None:
    if family is None:
        return None
    if isinstance(family, MarketFamily):
        return family.value
    text = str(family).strip()
    return text or None


def parse_market_family(family: MarketFamily | str | None) -> MarketFamily | None:
    if family is None:
        return None
    if isinstance(family, MarketFamily):
        return family
    try:
        return MarketFamily(str(family).strip())
    except ValueError:
        return None


def canonical_result_space(trade: PaperTrade) -> CanonicalResultSpace:
    """Derive HOME/DRAW/AWAY (etc.) from canonical family, not from trade legs."""

    family = parse_market_family(trade.market_family)
    if family is None or family not in _MANUAL_FAMILIES:
        return CanonicalResultSpace(
            family=family,
            unsupported_reason=UNSUPPORTED_MANUAL_FAMILY,
        )
    if family is MarketFamily.TOTAL_GOALS:
        line = trade.line
        if line is None or line_push_possible(line) is not False:
            return CanonicalResultSpace(family=family, unsupported_reason=UNSUPPORTED_TOTAL_LINE)
    if family in {MarketFamily.POINT_SPREAD, MarketFamily.TOTAL_POINTS}:
        line = trade.line
        if line is None or line_push_possible(line) is not False:
            reason = (
                UNSUPPORTED_SPREAD_LINE
                if family is MarketFamily.POINT_SPREAD
                else UNSUPPORTED_TOTAL_LINE
            )
            return CanonicalResultSpace(family=family, unsupported_reason=reason)
    outcomes = _outcomes_for_family(family)
    if not outcomes:
        return CanonicalResultSpace(family=family, unsupported_reason=UNSUPPORTED_MANUAL_FAMILY)
    home = (trade.home_team or "Home").strip() or "Home"
    away = (trade.away_team or "Away").strip() or "Away"
    choices = [
        CanonicalResultChoice(
            value=outcome.value,
            label=_label_for_outcome(family, outcome, home=home, away=away, line=trade.line),
        )
        for outcome in outcomes
    ]
    return CanonicalResultSpace(family=family, choices=choices)


def is_valid_canonical_settlement_outcome(trade: PaperTrade, winning_outcome: str) -> bool:
    """True when *winning_outcome* is a paying canonical result for the trade family.

    Traded-leg labels remain valid even when they already exist on the trade.
    Family-complete results (e.g. 1X2 DRAW) are valid even if no leg covers them.
    """

    text = str(winning_outcome or "").strip()
    if not text:
        return False
    if text in {leg.outcome for leg in trade.legs}:
        family = parse_market_family(trade.market_family)
        if family is MarketFamily.GAME_WINNER and text in {
            CanonicalOutcome.DRAW.value,
            "tie",
            "tied",
        }:
            return False
        return True
    space = canonical_result_space(trade)
    if space.unsupported_reason:
        return False
    return text in space.values


def validate_manual_settlement_outcome(trade: PaperTrade, winning_outcome: str) -> str | None:
    """Return a fail-closed blocker, or None when the operator result is admissible."""

    text = str(winning_outcome or "").strip()
    family = parse_market_family(trade.market_family)
    if family is MarketFamily.GAME_WINNER and text in {
        CanonicalOutcome.DRAW.value,
        "tie",
        "tied",
        "push",
    }:
        return NFL_EXCEPTIONAL_TIE_BLOCKER
    space = canonical_result_space(trade)
    if space.unsupported_reason:
        return space.unsupported_reason
    if text not in space.values:
        return "invalid_canonical_result"
    return None


def _outcomes_for_family(family: MarketFamily) -> tuple[CanonicalOutcome, ...]:
    if family is MarketFamily.MATCH_RESULT:
        return (CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY)
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        return (CanonicalOutcome.YES, CanonicalOutcome.NO)
    if family in {MarketFamily.TOTAL_GOALS, MarketFamily.TOTAL_POINTS}:
        return (CanonicalOutcome.OVER, CanonicalOutcome.UNDER)
    if family is MarketFamily.FIRST_TEAM_TO_SCORE:
        return (CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL)
    if family is MarketFamily.GAME_WINNER:
        return (CanonicalOutcome.HOME, CanonicalOutcome.AWAY)
    if family is MarketFamily.POINT_SPREAD:
        return (CanonicalOutcome.HOME, CanonicalOutcome.AWAY)
    return ()


def _signed_line_label(line: Decimal | None) -> str:
    """Display a signed spread line. Does not mutate the stored canonical home line."""

    text = format_stored_line(line)
    if text is None or line is None:
        return "n.a."
    if line > 0 and not text.startswith("+"):
        return f"+{text}"
    return text


def _label_for_outcome(
    family: MarketFamily,
    outcome: CanonicalOutcome,
    *,
    home: str,
    away: str,
    line: Decimal | None,
) -> str:
    if family is MarketFamily.MATCH_RESULT:
        if outcome is CanonicalOutcome.HOME:
            return f"{home} win"
        if outcome is CanonicalOutcome.AWAY:
            return f"{away} win"
        return "Draw"
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        return "Yes" if outcome is CanonicalOutcome.YES else "No"
    if family in {MarketFamily.TOTAL_GOALS, MarketFamily.TOTAL_POINTS}:
        line_text = format_stored_line(line) or "n.a."
        if outcome is CanonicalOutcome.OVER:
            return f"Over {line_text}"
        return f"Under {line_text}"
    if family is MarketFamily.FIRST_TEAM_TO_SCORE:
        if outcome is CanonicalOutcome.HOME:
            return f"{home} first team to score"
        if outcome is CanonicalOutcome.AWAY:
            return f"{away} first team to score"
        return "No goal"
    if family is MarketFamily.GAME_WINNER:
        if outcome is CanonicalOutcome.HOME:
            return f"{home} win"
        return f"{away} win"
    if family is MarketFamily.POINT_SPREAD:
        # Stored canonical line is the home signed line (#429). Away display
        # negates that Decimal for the operator label only.
        if outcome is CanonicalOutcome.HOME:
            return f"{home} covers {_signed_line_label(line)}"
        away_line = None if line is None else -line
        return f"{away} covers {_signed_line_label(away_line)}"
    return outcome.value


def soccer_or_nfl_family(value: Any) -> MarketFamily | None:
    return parse_market_family(value)
