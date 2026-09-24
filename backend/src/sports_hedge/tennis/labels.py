"""Operator labels for tennis match winner. Not a live-execution claim."""

from __future__ import annotations

from sports_hedge.domain.football import CanonicalMarket, CanonicalOutcome
from sports_hedge.tennis.constants import TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT


def tennis_fixture_label(home: str, away: str, tournament: str, round_label: str) -> str:
    round_text = f" {round_label}" if round_label else ""
    return f"{home} vs {away} ({tournament}{round_text})"


def tennis_operator_market_label(market: CanonicalMarket) -> str:
    return f"Match Winner ({market.event.competition})"


def tennis_operator_side_label(market: CanonicalMarket, outcome: CanonicalOutcome) -> str:
    for runner in market.runners:
        if runner.outcome is outcome:
            return runner.label
    return outcome.value


def tennis_non_executable_reason() -> str:
    return TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT
