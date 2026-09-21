"""PAPER-only NFL settlement caveat and automatic-settlement fail-closed rules."""

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
from sports_hedge.nfl.constants import (
    NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NFL_LIFECYCLE_AUDIT_KIND,
    NFL_NORMAL_COMPLETION_NOT_PROVEN,
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

NFL_LIFECYCLE_PRE_RESULT = "pre_result_normal"
NFL_LIFECYCLE_GRADED = "graded_normal"
NFL_LIFECYCLE_EXCEPTIONAL = "exceptional"
NFL_LIFECYCLE_UNKNOWN = "unknown"

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
    }
)


@dataclass(frozen=True)
class NflLifecycleObservation:
    """One append-only provider-status snapshot for an NFL PAPER trade/game."""

    phase: str
    tokens: tuple[str, ...]
    observed_at: str | None = None


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


def classify_nfl_lifecycle_tokens(tokens: Iterable[object]) -> str:
    """Classify one observation's provider status tokens."""

    normalized = tuple(
        str(token or "").strip().casefold().replace("_", "-")
        for token in tokens
        if str(token or "").strip()
    )
    if nfl_exceptional_status_blocker(*normalized) is not None:
        return NFL_LIFECYCLE_EXCEPTIONAL
    compact = {item.replace("-", "_") for item in normalized}
    compact |= set(normalized)
    if compact & _EXCEPTIONAL_STATUS_TOKENS:
        return NFL_LIFECYCLE_EXCEPTIONAL
    if compact & _GRADED_NORMAL_TOKENS:
        return NFL_LIFECYCLE_GRADED
    if compact & _PRE_RESULT_NORMAL_TOKENS:
        return NFL_LIFECYCLE_PRE_RESULT
    return NFL_LIFECYCLE_UNKNOWN


def nfl_lifecycle_observation(
    tokens: Iterable[object],
    *,
    observed_at: datetime | str | None = None,
) -> NflLifecycleObservation:
    cleaned = tuple(str(token).strip() for token in tokens if str(token or "").strip())
    when = observed_at.isoformat() if isinstance(observed_at, datetime) else observed_at
    return NflLifecycleObservation(
        phase=classify_nfl_lifecycle_tokens(cleaned),
        tokens=cleaned,
        observed_at=when,
    )


def nfl_lifecycle_audit_detail(observation: NflLifecycleObservation) -> str:
    return json.dumps(
        {
            "kind": NFL_LIFECYCLE_AUDIT_KIND,
            "phase": observation.phase,
            "tokens": list(observation.tokens),
            "observed_at": observation.observed_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def nfl_lifecycle_observations_from_trade(trade: Any) -> list[NflLifecycleObservation]:
    observations: list[NflLifecycleObservation] = []
    for event in getattr(trade, "audit", None) or ():
        parsed = _parse_lifecycle_detail(getattr(event, "detail", None))
        if parsed is not None:
            observations.append(parsed)
    return observations


def nfl_automatic_settlement_lifecycle_blocker(
    trade: Any,
    *current_tokens: object,
) -> str | None:
    """Require durable proof that this NFL game completed without exceptional history.

    A lone final score plus current closed/settled state is not proof. Automatic
    settlement stays fail-closed until append-only history includes a pre-result
    normal observation and never records postpone/suspend/cancel/tie/50-50.
    """

    history = list(nfl_lifecycle_observations_from_trade(trade))
    current = nfl_lifecycle_observation(current_tokens)
    all_observations = [*history, current]
    if any(item.phase == NFL_LIFECYCLE_EXCEPTIONAL for item in all_observations):
        return NFL_SETTLEMENT_FAIL_CLOSED_REASON
    if not any(item.phase == NFL_LIFECYCLE_PRE_RESULT for item in history):
        return NFL_NORMAL_COMPLETION_NOT_PROVEN
    return None


def collect_nfl_lifecycle_tokens(*payloads: object) -> list[str]:
    tokens: list[str] = []
    for payload in payloads:
        tokens.extend(_status_tokens_from_payload(payload))
    return tokens


def _parse_lifecycle_detail(detail: object) -> NflLifecycleObservation | None:
    text = str(detail or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("kind") or "") != NFL_LIFECYCLE_AUDIT_KIND:
        return None
    tokens = payload.get("tokens") or []
    if not isinstance(tokens, list):
        tokens = [tokens]
    return NflLifecycleObservation(
        phase=str(payload.get("phase") or NFL_LIFECYCLE_UNKNOWN),
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
