from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PRIORITY_SEVERITIES = ("PRIORITY", "HIGH_PRIORITY", "CRITICAL")
PRIORITY_ALERT_STATES = (
    "OPEN",
    "AWAITING_EXTERNAL_LEG_CONFIRMATION",
    "EXTERNAL_LEG_CONFIRMED",
    "HEDGE_REVALIDATED",
    "HEDGE_REVALIDATION_FAILED",
    "EXPIRED",
)
OPERATOR_ACTIONS = (
    "PREPARE_MANUAL_OVERRIDE",
    "PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY",
)

PrioritySeverityName = Literal["PRIORITY", "HIGH_PRIORITY", "CRITICAL"]
PriorityAlertStateName = Literal[
    "OPEN",
    "AWAITING_EXTERNAL_LEG_CONFIRMATION",
    "EXTERNAL_LEG_CONFIRMED",
    "HEDGE_REVALIDATED",
    "HEDGE_REVALIDATION_FAILED",
    "EXPIRED",
]
OperatorActionName = Literal[
    "PREPARE_MANUAL_OVERRIDE",
    "PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY",
]


def require_aware_utc(value: datetime, *, field: str) -> datetime:
    """Fail closed on naive timestamps instead of silently assuming UTC."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def priority_alert_deep_link(alert_id: str) -> str:
    """Canonical operator UI path owned by the Priority Alerts dashboard."""

    return f"/arbitrage/priority-alerts/{alert_id}"


class CanonicalRecommendationAdapter(BaseModel):
    """1:1 slice of RecommendedManualSize used by notifications."""

    recommended_size: Decimal
    guaranteed_profit: Decimal
    limiting_leg_outcome: str
    limiting_leg_venue: str
    quote_age_ms: int = Field(ge=0)


class CanonicalHedgeRevalidationAdapter(BaseModel):
    """1:1 slice of ExternalHedgeRevalidation used by notifications."""

    accepted: bool
    lifecycle_state: PriorityAlertStateName
    reasons: list[str] = Field(default_factory=list)


class PriorityAlertNotificationAdapter(BaseModel):
    """Narrow 1:1 adapter for the canonical Priority Alert read model.

    Field names match `sports_hedge.arbitrage.priority_alerts.models.PriorityAlert`.
    This package does not own a second alert truth model and does not import the
    arb solver or venue execution stack.
    """

    alert_id: str
    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    severity: PrioritySeverityName
    lifecycle_state: PriorityAlertStateName = "OPEN"
    operator_action: OperatorActionName = "PREPARE_MANUAL_OVERRIDE"
    net_guaranteed_edge: Decimal
    recommendation: CanonicalRecommendationAdapter
    opened_at: datetime
    expired_at: datetime | None = None
    event_summary: str | None = None
    market_summary: str | None = None
    paper_mode: bool = True
    places_orders: bool = False
    commits_automated_legs: bool = False
    hedge_revalidation: CanonicalHedgeRevalidationAdapter | None = None

    @model_validator(mode="after")
    def fail_closed(self) -> "PriorityAlertNotificationAdapter":
        self.opened_at = require_aware_utc(self.opened_at, field="opened_at")
        if self.expired_at is not None:
            self.expired_at = require_aware_utc(self.expired_at, field="expired_at")
        if not self.paper_mode:
            raise ValueError("Priority Alert notifications are paper-mode only")
        if self.places_orders:
            raise ValueError("Priority Alert notifications cannot imply order placement")
        if self.commits_automated_legs:
            raise ValueError("Priority Alert notifications cannot commit automated legs")
        return self


def notification_adapter_from_priority_alert(alert: object) -> PriorityAlertNotificationAdapter:
    """Project a canonical PriorityAlert (or compatible object) onto the adapter."""

    if isinstance(alert, PriorityAlertNotificationAdapter):
        return alert
    recommendation = getattr(alert, "recommendation", None)
    if recommendation is None:
        raise TypeError("canonical Priority Alert must expose recommendation")
    hedge = getattr(alert, "hedge_revalidation", None)
    hedge_adapter = None
    if hedge is not None:
        hedge_adapter = CanonicalHedgeRevalidationAdapter(
            accepted=bool(hedge.accepted),
            lifecycle_state=_enum_value(hedge.lifecycle_state),
            reasons=list(getattr(hedge, "reasons", []) or []),
        )
    return PriorityAlertNotificationAdapter(
        alert_id=str(alert.alert_id),
        opportunity_id=str(alert.opportunity_id),
        canonical_event_id=_optional_str(getattr(alert, "canonical_event_id", None)),
        canonical_market_id=_optional_str(getattr(alert, "canonical_market_id", None)),
        severity=_enum_value(alert.severity),
        lifecycle_state=_enum_value(getattr(alert, "lifecycle_state", "OPEN")),
        operator_action=_enum_value(
            getattr(alert, "operator_action", "PREPARE_MANUAL_OVERRIDE")
        ),
        net_guaranteed_edge=Decimal(str(alert.net_guaranteed_edge)),
        recommendation=CanonicalRecommendationAdapter(
            recommended_size=Decimal(str(recommendation.recommended_size)),
            guaranteed_profit=Decimal(str(recommendation.guaranteed_profit)),
            limiting_leg_outcome=str(recommendation.limiting_leg_outcome),
            limiting_leg_venue=str(recommendation.limiting_leg_venue),
            quote_age_ms=int(recommendation.quote_age_ms),
        ),
        opened_at=alert.opened_at,
        expired_at=getattr(alert, "expired_at", None),
        event_summary=_optional_str(getattr(alert, "event_summary", None)),
        market_summary=_optional_str(getattr(alert, "market_summary", None)),
        paper_mode=bool(getattr(alert, "paper_mode", True)),
        places_orders=bool(getattr(alert, "places_orders", False)),
        commits_automated_legs=bool(getattr(alert, "commits_automated_legs", False)),
        hedge_revalidation=hedge_adapter,
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
