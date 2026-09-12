from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, Sequence
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.arbitrage.watchlist.economics import net_edge_from_implied_sum


class PaperScanRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    scanned_at: datetime
    canonical_event_id: str
    canonical_market_id: str
    competition: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    market_family: MarketFamily
    period: FootballPeriod
    line: Decimal | None = None
    venues: list[VenueName]
    source_market_ids: list[str]
    mapping_confidence: float = Field(ge=0.0, le=1.0)
    is_arbitrage: bool
    eligible_for_paper_simulation: bool
    gross_edge: Decimal | None = None
    net_edge: Decimal | None = None
    executable_stake_gbp: Decimal | None = Field(default=None, ge=0)
    guaranteed_profit_gbp: Decimal | None = None
    execution_risk_score: int | None = Field(default=None, ge=0, le=100)
    execution_risk_band: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    decision_json: str

    @model_validator(mode="after")
    def ensure_timezones(self) -> "PaperScanRecord":
        if self.scanned_at.tzinfo is None:
            self.scanned_at = self.scanned_at.replace(tzinfo=UTC)
        if self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        return self


class PaperScanSummary(BaseModel):
    since: datetime
    scan_count: int = Field(ge=0)
    arbitrage_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    rejection_count: int = Field(ge=0)
    top_net_edge: Decimal | None = None
    top_guaranteed_profit_gbp: Decimal | None = None
    latest_scan_at: datetime | None = None


class PaperScanAuditSink(Protocol):
    def append_scan(self, record: PaperScanRecord) -> None: ...


def build_paper_scan_record(
    decision: PaperScanDecision,
    history: Sequence[MarketSnapshot],
) -> PaperScanRecord:
    if not decision.canonical_event_id or not decision.canonical_market_id:
        raise ValueError("matched paper decision requires canonical ids for audit persistence")
    if not history:
        raise ValueError("market history is required to build a paper scan record")

    snapshot = history[-1]
    if not snapshot.competition or not snapshot.home_team or not snapshot.away_team:
        raise ValueError("market history lacks event labels required for paper read model")
    if snapshot.kickoff_utc is None:
        raise ValueError("market history lacks kickoff required for paper read model")

    depth_scan = decision.depth_scan
    payoff_scan = decision.payoff_scan
    allocation = decision.allocation
    solution = depth_scan.solution if depth_scan is not None else None
    gross_edge: Decimal | None = None
    net_edge: Decimal | None = None
    if depth_scan is not None and depth_scan.selected_quotes:
        gross_implied = sum(
            (Decimal("1") / quote.gross_weighted_odds for quote in depth_scan.selected_quotes),
            Decimal("0"),
        )
        if gross_implied > 0:
            gross_edge = Decimal("1") / gross_implied - Decimal("1")
    if solution is not None and solution.implied_probability_sum > 0:
        net_edge = net_edge_from_implied_sum(solution.implied_probability_sum)
    elif payoff_scan is not None and payoff_scan.solution.is_arbitrage:
        net_edge = payoff_scan.solution.roi

    is_arb = bool(solution and solution.is_arbitrage) or bool(
        payoff_scan is not None and payoff_scan.solution.is_arbitrage
    )
    executable = None
    profit = None
    if allocation is not None and allocation.accepted:
        executable = allocation.recommended_committed_capital
        profit = allocation.guaranteed_profit
    elif solution is not None and solution.is_arbitrage:
        executable = solution.total_stake
        profit = solution.guaranteed_profit
    elif payoff_scan is not None and payoff_scan.solution.is_arbitrage:
        executable = payoff_scan.solution.total_capital_used
        profit = payoff_scan.solution.minimum_state_pnl

    source_market_ids = sorted(
        {
            value
            for value in (item.source_market_id for item in history)
            if value is not None
        }
    )
    venues = sorted({item.venue for item in history}, key=lambda venue: venue.value)

    return PaperScanRecord(
        scanned_at=decision.scanned_at,
        canonical_event_id=decision.canonical_event_id,
        canonical_market_id=decision.canonical_market_id,
        competition=snapshot.competition,
        home_team=snapshot.home_team,
        away_team=snapshot.away_team,
        kickoff_utc=snapshot.kickoff_utc,
        market_family=snapshot.market_family,
        period=snapshot.period,
        line=snapshot.market_line,
        venues=venues,
        source_market_ids=source_market_ids,
        mapping_confidence=decision.market_match.confidence,
        is_arbitrage=is_arb,
        eligible_for_paper_simulation=decision.eligible_for_paper_simulation,
        gross_edge=gross_edge,
        net_edge=net_edge,
        executable_stake_gbp=executable,
        guaranteed_profit_gbp=profit,
        execution_risk_score=(decision.execution_risk.score if decision.execution_risk else None),
        execution_risk_band=(decision.execution_risk.band if decision.execution_risk else None),
        rejection_reasons=decision.rejection_reasons,
        decision_json=decision.model_dump_json(),
    )
