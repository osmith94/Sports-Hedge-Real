from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from sports_hedge.arbitrage.priority_alerts.models import (
    ExternalCounterpartyPlan,
    ExternalHedgeRevalidation,
    ExternalLegConfirmation,
    ManualOverrideRecommendation,
    PriorityAlert,
    PriorityAlertCandidate,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.config import get_settings


router = APIRouter(prefix="/priority-alerts", tags=["priority-alerts"])


class PrepareManualOverrideRequest(BaseModel):
    requested_size: Decimal = Field(gt=0)


class ConfirmExternalLegRequest(BaseModel):
    confirmation: ExternalLegConfirmation
    fresh_candidate: PriorityAlertCandidate


def thresholds_from_settings() -> PriorityAlertThresholds:
    settings = get_settings()
    return PriorityAlertThresholds(
        minimum_net_edge=Decimal(str(settings.priority_min_net_edge)),
        minimum_expected_profit=Decimal(str(settings.priority_min_expected_profit)),
        minimum_executable_depth=Decimal(str(settings.priority_min_executable_depth)),
        maximum_quote_age_ms=settings.priority_max_quote_age_ms,
        maximum_execution_risk=settings.priority_max_execution_risk,
        minimum_depth_coverage=Decimal(str(settings.priority_min_depth_coverage)),
        minimum_capital_efficiency=Decimal(str(settings.priority_min_capital_efficiency)),
        safety_haircut=Decimal(str(settings.priority_safety_haircut)),
        operator_manual_cap=Decimal(str(settings.priority_operator_manual_cap)),
        risk_limit=Decimal(str(settings.priority_risk_limit)),
    )


@lru_cache
def get_priority_alert_service() -> PriorityAlertService:
    return PriorityAlertService(
        thresholds=thresholds_from_settings(),
        settings=get_settings(),
    )


@router.get("", response_model=list[PriorityAlert])
def list_priority_alerts(
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> list[PriorityAlert]:
    return service.current_alerts()


@router.get("/{alert_id}", response_model=PriorityAlert)
def get_priority_alert(
    alert_id: str,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> PriorityAlert:
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Priority alert not found")
    return alert


@router.post(
    "/evaluate",
    response_model=PriorityAlert | None,
    status_code=status.HTTP_200_OK,
)
def evaluate_priority_alert(
    candidate: PriorityAlertCandidate,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> PriorityAlert | None:
    """Paper-only qualification. Does not place or cancel venue orders."""
    return service.ingest(candidate)


@router.post(
    "/{alert_id}/manual-override",
    response_model=ManualOverrideRecommendation,
)
def prepare_manual_override(
    alert_id: str,
    request: PrepareManualOverrideRequest,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> ManualOverrideRecommendation:
    try:
        ticket = service.prepare_manual_override(alert_id, request.requested_size)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Priority alert not found") from exc
    return ticket


@router.post(
    "/{alert_id}/manual-override/cancel",
    response_model=PriorityAlert,
)
def cancel_manual_override(
    alert_id: str,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> PriorityAlert:
    try:
        return service.cancel_manual_override(alert_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Priority alert not found") from exc


@router.post(
    "/{alert_id}/external-counterparty",
    response_model=ExternalCounterpartyPlan,
)
def prepare_external_counterparty(
    alert_id: str,
    request: PrepareManualOverrideRequest,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> ExternalCounterpartyPlan:
    """Prepare an external-operator ticket. Does not place a bet or commit the hedge."""
    try:
        return service.prepare_external_counterparty(alert_id, request.requested_size)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Priority alert not found") from exc


@router.post(
    "/{alert_id}/external-counterparty/confirm",
    response_model=ExternalHedgeRevalidation,
)
def confirm_external_leg(
    alert_id: str,
    request: ConfirmExternalLegRequest,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> ExternalHedgeRevalidation:
    try:
        return service.confirm_external_leg(
            alert_id,
            request.confirmation,
            request.fresh_candidate,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Priority alert not found") from exc


@router.post(
    "/{alert_id}/external-counterparty/cancel",
    response_model=PriorityAlert,
)
def cancel_external_counterparty(
    alert_id: str,
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> PriorityAlert:
    try:
        return service.cancel_external_counterparty(alert_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Priority alert not found") from exc


@router.get("/{alert_id}/events")
def alert_events(
    alert_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    service: PriorityAlertService = Depends(get_priority_alert_service),
) -> list:
    alert = service.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Priority alert not found")
    return alert.history[-limit:]
