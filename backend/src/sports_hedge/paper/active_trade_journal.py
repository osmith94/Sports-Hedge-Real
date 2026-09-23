"""Append-only ACTIVE TRADE lifecycle journal.

Observability/accounting only. Never performs provider I/O. Records facts already
produced by pricing, treasury, and paper fill paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from sports_hedge.application.serving_build import get_serving_build_info
from sports_hedge.domain.models import VenueName

ACTIVE_TRADE_EVENT_SCHEMA = "sports_hedge.active_trade_event.v1"
ACTIVE_TRADE_TIMELINE_LIMIT = 12


class ActiveTradeEventType(StrEnum):
    PROMOTED_TO_ACTIVE = "promoted_to_active"
    ACTIVE_REFRESH_STARTED = "active_refresh_started"
    ACTIVE_REFRESH_RESULT = "active_refresh_result"
    ENTRY_DECISION = "entry_decision"
    ENTRY_ATTEMPT = "entry_attempt"
    ENTRY_FILL = "entry_fill"
    ENTRY_PARTIAL_FILL = "entry_partial_fill"
    ENTRY_NO_FILL = "entry_no_fill"
    ENTRY_BLOCKED = "entry_blocked"
    ENTRY_RECOVERY_DECISION = "entry_recovery_decision"
    ENTRY_RECOVERY_FILL = "entry_recovery_fill"
    ENTRY_RECOVERY_RESIDUAL = "entry_recovery_residual"
    TOPUP_DECISION = "topup_decision"
    TOPUP_ATTEMPT = "topup_attempt"
    TOPUP_FILL = "topup_fill"
    TOPUP_PARTIAL_FILL = "topup_partial_fill"
    CAP_REACHED = "cap_reached"
    BELOW_MIN_NET_ARB = "below_min_net_arb"
    NO_ACTION = "no_action"
    UNWIND_DECISION = "unwind_decision"
    SETTLED = "settled"
    SETTLEMENT_BLOCKED = "settlement_blocked"
    SETTLEMENT_INCOMPLETE = "settlement_incomplete"
    CLOSED = "closed"
    JOURNAL_WRITE_FAILED = "journal_write_failed"


class ActiveTradeReasonCode(StrEnum):
    PROMOTED = "promoted"
    REFRESH_STARTED = "refresh_started"
    REFRESH_EVALUATED = "refresh_evaluated"
    REFRESH_RETRY_WAIT = "refresh_retry_wait"
    REFRESH_REVALIDATION_NEEDED = "refresh_revalidation_needed"
    REFRESH_MISSING_IDENTITY = "refresh_missing_identity"
    REFRESH_CAPTURE_FAILED = "refresh_capture_failed"
    REFRESH_DEFERRED = "refresh_deferred"
    REFRESH_FAILED = "refresh_failed"
    NO_FRESH_DECISION = "no_fresh_decision"
    NO_ACTION_STALE_REFRESH = "no_action_stale_refresh"
    NO_ACTION_CAP_REACHED = "no_action_cap_reached"
    NO_ACTION_BELOW_MIN_NET = "no_action_below_min_net"
    NO_ACTION_NO_ROOM = "no_action_no_room"
    NO_ACTION_RECOVERING = "no_action_recovering"
    NO_ACTION_INCOMPLETE_EMPTY = "no_action_incomplete_empty"
    QUALIFYING_HOT = "qualifying_hot"
    QUALIFYING_BACKGROUND = "qualifying_background"
    QUALIFYING_PRICING_CYCLE = "qualifying_pricing_cycle"
    ENTRY_ATTEMPTED = "entry_attempted"
    ENTRY_COMMITTED = "entry_committed"
    ENTRY_PARTIAL = "entry_partial"
    ENTRY_NO_FILL = "entry_no_fill"
    ENTRY_BLOCKED = "entry_blocked"
    TOPUP_COMMITTED = "topup_committed"
    TOPUP_PARTIAL = "topup_partial"
    RECOVERY_BELOW_MIN_NET = "recovery_below_min_net"
    RECOVERY_DEPTH_BOUNDED = "recovery_depth_bounded"
    RECOVERY_TREASURY_BOUNDED = "recovery_treasury_bounded"
    RECOVERY_COMMITTED = "recovery_committed"
    RECOVERY_RESIDUAL = "recovery_residual"
    UNWIND_LOGGED = "unwind_logged"
    SETTLED = "settled"
    SETTLEMENT_BLOCKED = "settlement_blocked"
    SETTLEMENT_INCOMPLETE = "settlement_incomplete"
    CLOSED = "closed"
    JOURNAL_PERSIST_FAILED = "journal_persist_failed"


class ActiveTradeEvent(BaseModel):
    """One append-only ACTIVE TRADE journal fact."""

    event_id: str
    sequence: int = Field(ge=0)
    occurred_at: datetime
    schema_version: str = ACTIVE_TRADE_EVENT_SCHEMA
    serving_git_sha: str | None = None
    trade_id: str
    opportunity_id: str | None = None
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    competition: str | None = None
    market_family: str | None = None
    line: str | None = None
    active_phase: str | None = None
    event_type: ActiveTradeEventType
    reason_code: ActiveTradeReasonCode
    operator_copy: str
    venue: str | None = None
    cycle_id: str | None = None
    tranche_id: str | None = None
    fill_id: str | None = None
    dedupe_key: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_timezone(self) -> ActiveTradeEvent:
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        return self

    @field_validator("venue", mode="before")
    @classmethod
    def normalize_venue(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, VenueName):
            return value.value
        return str(value)


class ActiveTradeTimelineItem(BaseModel):
    """Compact operator read model. Not a full analytics product."""

    event_id: str
    occurred_at: datetime
    event_type: str
    reason_code: str
    operator_copy: str
    trade_id: str
    cycle_id: str | None = None
    venue: str | None = None
    data_kind: str = "persisted_active_trade_journal"


def serving_git_sha() -> str | None:
    return get_serving_build_info().git_sha


def json_safe(value: Any) -> Any:
    """Compact JSON-safe values. Never dumps unbounded raw books."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, VenueName):
        return value.value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def compact_executable_books(legs: list[Any] | None, *, max_levels: int = 3) -> list[dict[str, Any]]:
    """Structured executable prices/depth. First N levels only."""

    compact: list[dict[str, Any]] = []
    for leg in legs or []:
        levels = []
        for level in list(getattr(leg, "levels", None) or [])[:max_levels]:
            levels.append(
                {
                    "decimal_odds": json_safe(getattr(level, "decimal_odds", None)),
                    "available_stake": json_safe(getattr(level, "available_stake", None)),
                }
            )
        compact.append(
            {
                "venue": json_safe(getattr(leg, "venue", None)),
                "outcome": getattr(leg, "outcome", None),
                "source_event_id": getattr(leg, "source_event_id", None),
                "source_market_id": getattr(leg, "source_market_id", None),
                "source_runner_id": getattr(leg, "source_runner_id", None),
                "source_contract_id": getattr(leg, "source_contract_id", None),
                "displayed_odds": json_safe(getattr(leg, "displayed_odds", None)),
                "requested_stake": json_safe(getattr(leg, "requested_stake", None)),
                "depth_levels": levels,
            }
        )
    return compact


def compact_native_ids(trade: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for leg in getattr(trade, "legs", None) or []:
        key = (
            getattr(leg, "venue", None),
            getattr(leg, "source_event_id", None),
            getattr(leg, "source_market_id", None),
            getattr(leg, "source_runner_id", None),
            getattr(leg, "source_contract_id", None),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "venue": json_safe(leg.venue),
                "source_event_id": leg.source_event_id,
                "source_market_id": leg.source_market_id,
                "source_runner_id": leg.source_runner_id,
                "source_contract_id": leg.source_contract_id,
                "outcome": leg.outcome,
            }
        )
    return rows


def timeline_item_from_event(event: ActiveTradeEvent) -> ActiveTradeTimelineItem:
    return ActiveTradeTimelineItem(
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        event_type=event.event_type.value,
        reason_code=event.reason_code.value,
        operator_copy=event.operator_copy,
        trade_id=event.trade_id,
        cycle_id=event.cycle_id,
        venue=event.venue,
    )
