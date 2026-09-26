"""MLB fail-closed labels for pairs the structural register does not admit.

Owner-approved PAPER comparison of GAME_WINNER and exact x.5 TOTAL_RUNS is the
register gate. This module still names family, line, and unsupported-shape
rejections. It does not add a second settlement model.
"""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.mlb.constants import MLB_COMPETITION, MLB_SETTLEMENT_NOT_EXECUTABLE, MLB_SPORT
from sports_hedge.mlb.detect import is_mlb_canonical_event, is_mlb_market_family

# 2026-09-24 read-only census. See docs/mlb/01_PROVIDER_EVIDENCE.md.
# Kalshi BASEBALLGAMEWIN.pdf includes extra innings and a 48-hour fair-price
# postpone/cancel path. Polymarket moneyline/total text leaves extra innings
# unstated, keeps postponements open until completion, and uses 50-50 on cancel.
# Matchbook market payloads have no settlement text. PAPER comparison of a
# structurally registered pair is an owner decision in the register, not a
# claim that these census texts are independently proven.
MLB_SETTLEMENT_EVIDENCE_REASON = MLB_SETTLEMENT_NOT_EXECUTABLE


def mlb_structural_settlement(*, family: MarketFamily, line=None) -> SettlementFingerprint:
    """Fingerprint recorded for audit. Economically incomplete on purpose."""

    push = None if line is None else line_push_possible(line)
    return SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FULL_TIME,
        line=line,
        push_possible=False if push is False else push,
        penalties_included=None,
        extra_time_included=None,
        postponement_rule=None,
        abandonment_rule=None,
        unknown_reason=MLB_SETTLEMENT_EVIDENCE_REASON,
    )


def mlb_market_settlement_executable(market: CanonicalMarket) -> bool:
    """Single-market settlement proof. PAPER admission is the register, not this flag."""

    del market
    return False


def mlb_pair_non_executable_reason(left: CanonicalMarket, right: CanonicalMarket) -> str | None:
    if not is_mlb_canonical_event(left.event) and not is_mlb_canonical_event(right.event):
        return None
    if not is_mlb_market_family(left.family) or not is_mlb_market_family(right.family):
        return "mlb_market_family_not_stage1"
    if left.family != right.family:
        return "market_family_mismatch"
    if left.line != right.line:
        return "mlb_total_line_mismatch"
    return MLB_SETTLEMENT_EVIDENCE_REASON


def is_mlb_paper_trade(trade) -> bool:
    sport = str(getattr(trade, "sport", "") or "").strip()
    if sport == MLB_SPORT:
        return True
    competition = str(getattr(trade, "competition", "") or "").strip().casefold()
    if competition in {MLB_COMPETITION, "major league baseball"}:
        return True
    settlement_key = str(getattr(trade, "settlement_key", "") or "")
    return settlement_key.startswith("MLB_")
