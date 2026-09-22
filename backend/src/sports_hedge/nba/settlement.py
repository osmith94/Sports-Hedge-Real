"""PAPER-only NBA settlement caveat and automatic-settlement fail-closed rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.nba.constants import (
    NBA_COMPETITION,
    NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NBA_LIFECYCLE_AUDIT_KIND,
    NBA_NORMAL_COMPLETION_NOT_PROVEN,
    NBA_NOT_LIVE_EXECUTION_REASON,
    NBA_PAPER_NORMAL_COMPLETION_REASON,
    NBA_SETTLEMENT_FAIL_CLOSED_REASON,
    NBA_SPORT,
)
from sports_hedge.nba.detect import is_nba_canonical_event, is_nba_market_family

NBA_PAPER_AUDIT_REASONS: tuple[str, ...] = (
    NBA_PAPER_NORMAL_COMPLETION_REASON,
    NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NBA_NOT_LIVE_EXECUTION_REASON,
)

NBA_LIFECYCLE_PRE_RESULT = "pre_result_normal"
NBA_LIFECYCLE_GRADED = "graded_normal"
NBA_LIFECYCLE_EXCEPTIONAL = "exceptional"
NBA_LIFECYCLE_UNKNOWN = "unknown"

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
        "lfmp",
    }
)

_PRE_RESULT_NORMAL_TOKENS = frozenset(
    {
        "open",
        "upcoming",
        "scheduled",
        "not_started",
        "not-started",
        "pre-game",
        "pregame",
        "pre_game",
        "in_play",
        "in-play",
        "inplay",
        "live",
        "active",
        "started",
        "ns",
    }
)

_GRADED_NORMAL_TOKENS = frozenset(
    {
        "graded",
        "settled",
        "determined",
        "finished",
        "final",
        "finalized",
        "completed",
        "complete",
        "paid",
        "settled-complete",
        "resolved",
    }
)


@dataclass(frozen=True)
class NbaLifecycleObservation:
    """One append-only provider-status snapshot for an NBA PAPER trade/game."""

    phase: str
    tokens: tuple[str, ...]
    observed_at: str | None = None


def nba_paper_settlement(
    *,
    family: MarketFamily,
    line=None,
) -> SettlementFingerprint:
    """Fingerprint for PAPER comparison of a completed NBA game.

    Extra-time/OT inclusion is not independently proven on every venue/family.
    The fingerprint stays economically incomplete so this path cannot be
    mistaken for live-execution-grade APPROVED_EQUIVALENT. The register, not
    this fingerprint, admits PAPER comparison.
    """

    push = False if line is None else line_push_possible(line)
    return SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FULL_TIME,
        line=line,
        push_possible=False if push is False else push,
        penalties_included=False,
        extra_time_included=None,
        unknown_reason=NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    )


def nba_paper_audit_reasons() -> list[str]:
    return list(NBA_PAPER_AUDIT_REASONS)


def is_nba_paper_trade(trade) -> bool:
    sport = str(getattr(trade, "sport", "") or "").strip()
    if sport == NBA_SPORT:
        return True
    competition = str(getattr(trade, "competition", "") or "")
    if competition.strip().upper() == NBA_COMPETITION:
        return True
    return False


def nba_exceptional_status_blocker(*values: object) -> str | None:
    """Fail closed when provider evidence names an exceptional lifecycle."""

    for value in values:
        text = str(value or "").strip().casefold()
        if not text:
            continue
        if text in _EXCEPTIONAL_STATUS_TOKENS:
            return NBA_SETTLEMENT_FAIL_CLOSED_REASON
        compact = text.replace("_", " ")
        for token in _EXCEPTIONAL_STATUS_TOKENS:
            if token in compact:
                return NBA_SETTLEMENT_FAIL_CLOSED_REASON
    return None


def nba_tied_score_blocker(home_score: int | None, away_score: int | None) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score == away_score:
        return NBA_SETTLEMENT_FAIL_CLOSED_REASON
    return None


def nba_market_uses_paper_caveat(market: CanonicalMarket) -> bool:
    return is_nba_canonical_event(market.event) and is_nba_market_family(market.family)


def classify_nba_lifecycle_tokens(tokens: Iterable[object]) -> str:
    """Classify one observation's provider status tokens."""

    normalized = tuple(
        str(token or "").strip().casefold().replace("_", "-")
        for token in tokens
        if str(token or "").strip()
    )
    if nba_exceptional_status_blocker(*normalized) is not None:
        return NBA_LIFECYCLE_EXCEPTIONAL
    compact = {item.replace("-", "_") for item in normalized}
    compact |= set(normalized)
    if compact & _EXCEPTIONAL_STATUS_TOKENS:
        return NBA_LIFECYCLE_EXCEPTIONAL
    if compact & _GRADED_NORMAL_TOKENS:
        return NBA_LIFECYCLE_GRADED
    if compact & _PRE_RESULT_NORMAL_TOKENS:
        return NBA_LIFECYCLE_PRE_RESULT
    return NBA_LIFECYCLE_UNKNOWN


def nba_lifecycle_observation(
    tokens: Iterable[object],
    *,
    observed_at: datetime | str | None = None,
) -> NbaLifecycleObservation:
    cleaned = tuple(str(token).strip() for token in tokens if str(token or "").strip())
    when = observed_at.isoformat() if isinstance(observed_at, datetime) else observed_at
    return NbaLifecycleObservation(
        phase=classify_nba_lifecycle_tokens(cleaned),
        tokens=cleaned,
        observed_at=when,
    )


def nba_lifecycle_audit_detail(observation: NbaLifecycleObservation) -> str:
    return json.dumps(
        {
            "kind": NBA_LIFECYCLE_AUDIT_KIND,
            "phase": observation.phase,
            "tokens": list(observation.tokens),
            "observed_at": observation.observed_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def nba_lifecycle_observations_from_trade(trade: Any) -> list[NbaLifecycleObservation]:
    observations: list[NbaLifecycleObservation] = []
    for event in getattr(trade, "audit", None) or ():
        parsed = _parse_lifecycle_detail(getattr(event, "detail", None))
        if parsed is not None:
            observations.append(parsed)
    return observations


def nba_automatic_settlement_lifecycle_blocker(
    trade: Any,
    *current_tokens: object,
) -> str | None:
    """Require durable proof that this NBA game completed without exceptional history.

    A lone final score plus current closed/settled state is not proof. Automatic
    settlement stays fail-closed until append-only history includes a pre-result
    normal observation and never records postpone/suspend/cancel/tie/50-50.
    """

    history = list(nba_lifecycle_observations_from_trade(trade))
    current = nba_lifecycle_observation(current_tokens)
    all_observations = [*history, current]
    if any(item.phase == NBA_LIFECYCLE_EXCEPTIONAL for item in all_observations):
        return NBA_SETTLEMENT_FAIL_CLOSED_REASON
    if not any(item.phase == NBA_LIFECYCLE_PRE_RESULT for item in history):
        return NBA_NORMAL_COMPLETION_NOT_PROVEN
    return None


def collect_nba_lifecycle_tokens(*payloads: object) -> list[str]:
    tokens: list[str] = []
    for payload in payloads:
        tokens.extend(_status_tokens_from_payload(payload))
    return tokens


def _parse_lifecycle_detail(detail: object) -> NbaLifecycleObservation | None:
    text = str(detail or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("kind") or "") != NBA_LIFECYCLE_AUDIT_KIND:
        return None
    tokens = payload.get("tokens") or []
    if not isinstance(tokens, list):
        tokens = [tokens]
    return NbaLifecycleObservation(
        phase=str(payload.get("phase") or NBA_LIFECYCLE_UNKNOWN),
        tokens=tuple(str(item) for item in tokens if str(item).strip()),
        observed_at=(
            None if payload.get("observed_at") in (None, "") else str(payload.get("observed_at"))
        ),
    )


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
