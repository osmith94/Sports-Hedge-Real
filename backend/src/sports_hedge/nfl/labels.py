"""Operator-facing NFL market language. Raw provider identity stays in audit."""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.domain.football import CanonicalMarket, CanonicalOutcome, MarketFamily
from sports_hedge.nfl.constants import NFL_COMPETITION
from sports_hedge.nfl.teams import franchise_short_name

NFL_SETTLEMENT_CAVEAT_OPERATOR_TEXT = (
    "PAPER comparison is for a normal completed NFL game. Cancellation, "
    "suspension, and final-tie handling can differ across venues and must be "
    "resolved before any live execution. Automatic settlement fails closed on "
    "those exceptional cases."
)


def format_half_line(line: Decimal) -> str:
    text = format(line, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def signed_line_text(line: Decimal) -> str:
    magnitude = format_half_line(abs(line))
    if line > 0:
        return f"+{magnitude}"
    if line < 0:
        return f"-{magnitude}"
    return magnitude


def cover_explanation(*, team: str, signed_line: Decimal) -> str:
    """Plain-English cover contract for a half-point spread."""

    nickname = _short_name(team)
    if signed_line < 0:
        needed = int(abs(signed_line) + Decimal("0.5"))
        return f"{nickname} {signed_line_text(signed_line)} · must win by {needed}+"
    if signed_line > 0:
        allowed = int(signed_line - Decimal("0.5"))
        return f"{nickname} {signed_line_text(signed_line)} · may lose by up to {allowed}, or win"
    return f"{nickname} {signed_line_text(signed_line)}"


def total_explanation(*, side: CanonicalOutcome, line: Decimal) -> str:
    ceiling = int(line + Decimal("0.5"))
    floor = int(line - Decimal("0.5"))
    if side is CanonicalOutcome.OVER:
        return f"Over {format_half_line(line)} · {ceiling}+ combined points"
    return f"Under {format_half_line(line)} · {floor} or fewer combined points"


def nfl_operator_market_label(market: CanonicalMarket) -> str:
    if market.family is MarketFamily.GAME_WINNER:
        return "Game winner"
    if market.family is MarketFamily.POINT_SPREAD and market.line is not None:
        home = market.event.home_team
        return f"{_short_name(home)} {signed_line_text(market.line)}"
    if market.family is MarketFamily.TOTAL_POINTS and market.line is not None:
        return f"Total {format_half_line(market.line)}"
    return market.family.value.replace("_", " ")


def nfl_operator_side_label(
    market: CanonicalMarket,
    outcome: CanonicalOutcome,
) -> str:
    home = market.event.home_team
    away = market.event.away_team
    if market.family is MarketFamily.GAME_WINNER:
        if outcome is CanonicalOutcome.HOME:
            return f"{_short_name(home)} · Game winner"
        if outcome is CanonicalOutcome.AWAY:
            return f"{_short_name(away)} · Game winner"
        return outcome.value
    if market.family is MarketFamily.POINT_SPREAD and market.line is not None:
        if outcome is CanonicalOutcome.HOME:
            return cover_explanation(team=home, signed_line=market.line)
        if outcome is CanonicalOutcome.AWAY:
            return cover_explanation(team=away, signed_line=-market.line)
        return outcome.value
    if market.family is MarketFamily.TOTAL_POINTS and market.line is not None:
        if outcome in {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
            return total_explanation(side=outcome, line=market.line)
    return outcome.value


def nfl_fixture_label(*, home_team: str, away_team: str) -> str:
    return f"{NFL_COMPETITION} · {away_team} at {home_team}"


def _short_name(team: str) -> str:
    return franchise_short_name(team)
