"""PAPER-only NFL settlement caveat and automatic-settlement fail-closed rules."""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.nfl.constants import (
    NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NFL_NOT_LIVE_EXECUTION_REASON,
    NFL_PAPER_NORMAL_COMPLETION_REASON,
    NFL_SETTLEMENT_FAIL_CLOSED_REASON,
)
from sports_hedge.nfl.detect import is_nfl_canonical_event, is_nfl_market_family

NFL_PAPER_AUDIT_REASONS: tuple[str, ...] = (
    NFL_PAPER_NORMAL_COMPLETION_REASON,
    NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NFL_NOT_LIVE_EXECUTION_REASON,
)

_EXCEPTIONAL_STATUS_TOKENS = frozenset(
    {
        "void",
        "voided",
        "cancelled",
        "canceled",
        "abandoned",
        "postponed",
        "delayed",
        "rescheduled",
        "suspended",
        "tie",
        "tied",
        "draw",
        "push",
        "pushed",
        "dead-heat",
        "deadheat",
        "fair price",
        "fair_price",
        "0.50",
        "50-50",
        "50/50",
    }
)


def nfl_paper_settlement(
    *,
    family: MarketFamily,
    line=None,
) -> SettlementFingerprint:
    """Fingerprint for owner-approved PAPER comparison of a completed NFL game.

    Extra-time/OT inclusion is not independently proven on every venue. The
    fingerprint stays economically incomplete so this path cannot be mistaken
    for live-execution-grade APPROVED_EQUIVALENT. The register, not this
    fingerprint, admits PAPER comparison.
    """

    push = False if line is None else line_push_possible(line)
    return SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FULL_TIME,
        line=line,
        push_possible=False if push is False else push,
        penalties_included=False,
        extra_time_included=None,
        unknown_reason=NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    )


def nfl_paper_audit_reasons() -> list[str]:
    return list(NFL_PAPER_AUDIT_REASONS)


def is_nfl_paper_trade(trade) -> bool:
    family = getattr(trade, "market_family", None)
    if is_nfl_market_family(family):
        return True
    competition = str(getattr(trade, "competition", "") or "")
    if competition.strip().upper() == "NFL":
        return True
    return False


def nfl_exceptional_status_blocker(*values: object) -> str | None:
    """Fail closed when provider evidence names an exceptional lifecycle."""

    for value in values:
        text = str(value or "").strip().casefold()
        if not text:
            continue
        if text in _EXCEPTIONAL_STATUS_TOKENS:
            return NFL_SETTLEMENT_FAIL_CLOSED_REASON
        compact = text.replace("_", " ")
        for token in _EXCEPTIONAL_STATUS_TOKENS:
            if token in compact:
                return NFL_SETTLEMENT_FAIL_CLOSED_REASON
    return None


def nfl_tied_score_blocker(home_score: int | None, away_score: int | None) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score == away_score:
        return NFL_SETTLEMENT_FAIL_CLOSED_REASON
    return None


def nfl_market_uses_paper_caveat(market: CanonicalMarket) -> bool:
    return is_nfl_canonical_event(market.event) and is_nfl_market_family(market.family)
