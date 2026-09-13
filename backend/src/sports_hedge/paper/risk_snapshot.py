"""Immutable paper execution-risk provenance.

Entry snapshots are recorded at OPEN and must not be recomputed from later books.
Unwind/close assessments are separate append-only events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskResult


class PaperRiskSnapshotKind(StrEnum):
    ENTRY = "entry"
    UNWIND = "unwind"
    CLOSE = "close"


class PaperExecutionRiskSnapshot(BaseModel):
    kind: PaperRiskSnapshotKind
    recorded_at: datetime
    opportunity_id: str | None = None
    trade_id: str | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    band: str | None = None
    reasons: list[str] = Field(default_factory=list)
    maximum_execution_risk: int | None = Field(default=None, ge=0, le=100)
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    size_to_depth_ratio: float | None = Field(default=None, ge=0)
    hedge_liquidity_ratio: float | None = Field(default=None, ge=0)
    spread_bps: float | None = Field(default=None, ge=0)
    recent_volatility_bps: float | None = Field(default=None, ge=0)
    assumed_latency_ms: int | None = Field(default=None, ge=0)
    fill_confidence_score: float | None = None
    fill_confidence_reasons: list[str] = Field(default_factory=list)
    net_edge: Decimal | None = None
    trigger_net_edge: Decimal | None = None
    solver_model: str | None = None
    eligible_for_paper_simulation: bool | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PaperExecutionRiskSnapshot":
        if self.recorded_at.tzinfo is None:
            self.recorded_at = self.recorded_at.replace(tzinfo=UTC)
        return self

    def display_label(self) -> str | None:
        if self.score is None:
            return None
        band = self.band.replace("_", " ").title() if self.band else None
        return f"{self.score} · {band}" if band else str(self.score)


def snapshot_from_scan_decision(
    decision: PaperScanDecision,
    *,
    kind: PaperRiskSnapshotKind,
    recorded_at: datetime,
    opportunity_id: str | None = None,
    trade_id: str | None = None,
) -> PaperExecutionRiskSnapshot | None:
    risk = decision.execution_risk
    inputs = decision.execution_risk_inputs or (risk.inputs if risk is not None else None)
    if risk is None and inputs is None:
        return None
    fill_score, fill_reasons = _fill_confidence_from_decision(decision)
    return PaperExecutionRiskSnapshot(
        kind=kind,
        recorded_at=recorded_at,
        opportunity_id=opportunity_id,
        trade_id=trade_id,
        score=risk.score if risk is not None else None,
        band=risk.band if risk is not None else None,
        reasons=list(risk.reasons) if risk is not None else [],
        maximum_execution_risk=decision.maximum_execution_risk,
        quote_age_ms=decision.quote_age_ms,
        quote_age_basis=decision.quote_age_basis,
        size_to_depth_ratio=_input_float(inputs, "size_to_depth_ratio"),
        hedge_liquidity_ratio=_input_float(inputs, "hedge_liquidity_ratio"),
        spread_bps=_input_float(inputs, "spread_bps"),
        recent_volatility_bps=_input_float(inputs, "recent_volatility_bps"),
        assumed_latency_ms=inputs.assumed_latency_ms if inputs is not None else None,
        fill_confidence_score=fill_score,
        fill_confidence_reasons=fill_reasons,
        net_edge=decision_net_edge(decision),
        trigger_net_edge=decision.minimum_net_edge,
        solver_model=decision.solver_model,
        eligible_for_paper_simulation=decision.eligible_for_paper_simulation,
    )


def snapshot_from_execution_risk(
    risk: ExecutionRiskResult | None,
    *,
    kind: PaperRiskSnapshotKind,
    recorded_at: datetime,
    opportunity_id: str | None = None,
    trade_id: str | None = None,
    maximum_execution_risk: int | None = None,
    quote_age_ms: int | None = None,
    quote_age_basis: str | None = None,
    net_edge: Decimal | None = None,
    trigger_net_edge: Decimal | None = None,
    solver_model: str | None = None,
) -> PaperExecutionRiskSnapshot | None:
    if risk is None:
        return None
    inputs = risk.inputs
    return PaperExecutionRiskSnapshot(
        kind=kind,
        recorded_at=recorded_at,
        opportunity_id=opportunity_id,
        trade_id=trade_id,
        score=risk.score,
        band=risk.band,
        reasons=list(risk.reasons),
        maximum_execution_risk=maximum_execution_risk,
        quote_age_ms=quote_age_ms,
        quote_age_basis=quote_age_basis,
        size_to_depth_ratio=_input_float(inputs, "size_to_depth_ratio"),
        hedge_liquidity_ratio=_input_float(inputs, "hedge_liquidity_ratio"),
        spread_bps=_input_float(inputs, "spread_bps"),
        recent_volatility_bps=_input_float(inputs, "recent_volatility_bps"),
        assumed_latency_ms=inputs.assumed_latency_ms if inputs is not None else None,
        net_edge=net_edge,
        trigger_net_edge=trigger_net_edge,
        solver_model=solver_model,
    )


def _input_float(inputs: ExecutionRiskInputs | None, field: str) -> float | None:
    if inputs is None:
        return None
    value = getattr(inputs, field, None)
    return None if value is None else float(value)


def _fill_confidence_from_decision(decision: PaperScanDecision) -> tuple[float | None, list[str]]:
    allocation = decision.allocation
    if allocation is None:
        return None, []
    fill = getattr(allocation, "fill_confidence", None)
    if fill is None:
        return None, []
    score = getattr(fill, "score", None)
    reasons = list(getattr(fill, "reasons", None) or [])
    if score is None and hasattr(fill, "band"):
        return None, reasons
    try:
        numeric = float(score) if score is not None else None
    except (TypeError, ValueError):
        numeric = None
    return numeric, reasons
