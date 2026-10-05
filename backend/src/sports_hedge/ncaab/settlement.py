"""NCAAB settlement. Empty register; every participating venue required.

Do not copy NBA PR #506: automatic settlement must not ignore Polymarket, and
family support must never be broader than the Approved Match Register. No
NCAAB venue-pair/family cell is admitted until NCAAB-specific evidence
exists, so automatic settlement stays disabled. The block is missing ordinary
contract evidence, not a paper-only label.
"""

from __future__ import annotations

from typing import Any, Iterable

from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.ncaab.constants import (
    NCAAB_AUTO_SETTLEMENT_DISABLED_REASON,
    NCAAB_COMPETITION,
    NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NCAAB_MISSING_VENUE_EVIDENCE_REASON,
    NCAAB_NOT_LIVE_EXECUTION_REASON,
    NCAAB_PAIR_UNAPPROVED_REASON,
)
from sports_hedge.ncaab.detect import is_ncaab_canonical_event, is_ncaab_competition_label

# LEGACY readable set, including the historical paper-only token.
NCAAB_PAPER_AUDIT_REASONS: tuple[str, ...] = (
    NCAAB_PAIR_UNAPPROVED_REASON,
    NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NCAAB_NOT_LIVE_EXECUTION_REASON,
    NCAAB_AUTO_SETTLEMENT_DISABLED_REASON,
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
        "mecnet",
        "lfmp",
    }
)


def ncaab_paper_settlement(
    *,
    family: MarketFamily,
    line=None,
) -> SettlementFingerprint:
    """Incomplete fingerprint. The empty register does not admit NCAAB."""

    del family
    push = False if line is None else line_push_possible(line)
    return SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FULL_TIME,
        line=line,
        push_possible=False if push is False else push,
        penalties_included=False,
        extra_time_included=None,
        unknown_reason=NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    )


def ncaab_paper_audit_reasons() -> list[str]:
    """New rows keep the ordinary-contract block, not a paper-only veto.

    ``ncaab_paper_not_live_execution_equivalent`` may still appear on old rows.
    """

    return [
        NCAAB_PAIR_UNAPPROVED_REASON,
        NCAAB_AUTO_SETTLEMENT_DISABLED_REASON,
    ]


def is_ncaab_paper_trade(trade) -> bool:
    """Require NCAAB competition identity. Family-only GAME_WINNER is not enough."""

    competition = str(getattr(trade, "competition", "") or "")
    if is_ncaab_competition_label(competition) or competition.strip() == NCAAB_COMPETITION:
        return True
    sport = str(getattr(trade, "sport", "") or "")
    if sport == "basketball" and is_ncaab_competition_label(competition):
        return True
    return False


def ncaab_exceptional_status_blocker(*values: object) -> str | None:
    """Exceptional lifecycle tokens are not the NCAAB PAPER block."""

    del values
    return None


def ncaab_market_uses_paper_caveat(market: CanonicalMarket) -> bool:
    return is_ncaab_canonical_event(market.event)


def ncaab_automatic_settlement_blocker(trade: Any, *current_tokens: object) -> str | None:
    """Always fail closed: no NCAAB cell is PAPER-admitted.

    Missing provider evidence is never treated as normal completion. Elapsed
    tipoff is never completion. Settlement support is never broader than the
    empty register.
    """

    del current_tokens
    if not is_ncaab_paper_trade(trade):
        return None
    return NCAAB_PAIR_UNAPPROVED_REASON


def ncaab_unfetched_participating_venues(trade: Any, fetched: Iterable[VenueName]) -> frozenset[VenueName]:
    """Venues on the trade that were not fetched for automatic settlement."""

    fetched_set = frozenset(fetched)
    missing: set[VenueName] = set()
    for leg in getattr(trade, "legs", ()) or ():
        venue = getattr(leg, "venue", None)
        if venue is None:
            continue
        if venue not in fetched_set:
            missing.add(venue)
    return frozenset(missing)


def ncaab_missing_venue_evidence_blocker(
    trade: Any,
    fetched: Iterable[VenueName],
) -> str | None:
    if not is_ncaab_paper_trade(trade):
        return None
    if ncaab_unfetched_participating_venues(trade, fetched):
        return NCAAB_MISSING_VENUE_EVIDENCE_REASON
    return None


def ncaab_family_settlement_blocker(trade: Any) -> str | None:
    """Register is empty, so even GAME_WINNER automatic settlement is blocked."""

    if not is_ncaab_paper_trade(trade):
        return None
    return NCAAB_PAIR_UNAPPROVED_REASON


def collect_ncaab_lifecycle_tokens(*payloads: object) -> list[str]:
    tokens: list[str] = []
    for payload in payloads:
        tokens.extend(_status_tokens_from_payload(payload))
    return tokens


def _status_tokens_from_payload(payload: object) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        return [text] if text else []
    if isinstance(payload, dict):
        tokens: list[str] = []
        for key in (
            "status",
            "venue_status",
            "market_status",
            "event_status",
            "umaResolutionStatus",
            "result",
            "settlement_result",
            "period",
            "collateral_return_type",
        ):
            value = payload.get(key)
            if value not in (None, ""):
                tokens.append(str(value))
        nested = payload.get("event")
        if isinstance(nested, dict):
            tokens.extend(_status_tokens_from_payload(nested))
        return tokens
    if isinstance(payload, (list, tuple)):
        tokens: list[str] = []
        for item in payload:
            tokens.extend(_status_tokens_from_payload(item))
        return tokens
    return []
