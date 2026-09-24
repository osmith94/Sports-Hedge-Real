"""Operator labels for MLB fixtures. Not soccer 1X2 wording."""

from __future__ import annotations

from sports_hedge.domain.football import CanonicalMarket, CanonicalOutcome, MarketFamily
from sports_hedge.mlb.teams import franchise_short_name


def mlb_fixture_label(home: str, away: str) -> str:
    return f"{franchise_short_name(away)} at {franchise_short_name(home)}"


def mlb_operator_market_label(market: CanonicalMarket) -> str:
    if market.family is MarketFamily.GAME_WINNER:
        return "Game winner"
    if market.family is MarketFamily.TOTAL_RUNS and market.line is not None:
        return f"Total runs {market.line}"
    return market.family.value


def mlb_operator_side_label(market: CanonicalMarket, outcome: CanonicalOutcome) -> str:
    if market.family is MarketFamily.TOTAL_RUNS and market.line is not None:
        if outcome is CanonicalOutcome.OVER:
            return f"{market.line} or more combined runs is not the contract; over {market.line}"
        if outcome is CanonicalOutcome.UNDER:
            return f"under {market.line} combined runs"
    if outcome is CanonicalOutcome.HOME:
        return franchise_short_name(market.event.home_team)
    if outcome is CanonicalOutcome.AWAY:
        return franchise_short_name(market.event.away_team)
    return outcome.value
