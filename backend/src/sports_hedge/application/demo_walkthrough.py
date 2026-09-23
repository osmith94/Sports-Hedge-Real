"""Step 9 operator demo orchestration.

Wires existing 8C–8F paper lifecycle into an explicit start/reset and a
labelled DEMO / FIXTURE REPLAY path. Does not place venue orders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.paper_journal import DataProvenance, gbp_is_balanced
from sports_hedge.application.demo_fixtures import (
    DEMO_DATA_KIND,
    DEMO_FIXTURE_LABEL,
    DEMO_FX,
    LIVE_DATA_KIND,
    SolverKind,
    VenuePair,
    fixture_pair,
    fixture_venue_costs,
    reverse_quotes_from_observations,
    tighten_reverse_quotes,
)
from sports_hedge.application.live_refresh import LiveRefreshStatus, get_live_refresh_coordinator
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.paper.preparation import PreparablePaperOpportunity
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.watchlist.models import NearOpportunity
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.liquidity import default_pools
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import (
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeBookSummary,
    PaperTradeDetail,
    PaperTradeState,
)
from sports_hedge.paper.unwind.models import ReverseQuote, UnwindDecision, UnwindPolicy
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import PaperTreasurySnapshot
from sports_hedge.treasury.service import DEMO_SEED_GBP, DEFAULT_PAPER_FX_USD, PaperTreasuryError

CloseVia = Literal["hold", "unwind", "settlement"]


class DemoResetRequest(BaseModel):
    """Start or reset the paper demo store.

    Without ``reinitialize_store``, open locks/trades fail closed (same as
    treasury reset). With it, remaining locks are returned at zero betting
    P&L and identities are archived so a fresh seed can proceed.
    """

    reinitialize_store: bool = False
    reason: str = "explicit operator demo reset"
    seed_gbp: Decimal | None = Field(default=None, gt=0)
    usd_gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    fx_source: str | None = None
    include_kalshi: bool = True


class FixtureReplayRequest(BaseModel):
    venue_pair: VenuePair = "matchbook_kalshi"
    solver: SolverKind = "simple"
    close_via: CloseVia = "settlement"
    winning_outcome: str | None = None
    qualify_only: bool = False
    paper_only: bool = True
    places_orders: bool = False

    @model_validator(mode="after")
    def reject_live_execution(self) -> FixtureReplayRequest:
        if self.places_orders:
            raise ValueError("fixture replay cannot place orders")
        self.paper_only = True
        return self


class DemoCloseRequest(BaseModel):
    close_via: Literal["unwind", "settlement"] = "settlement"
    winning_outcome: str | None = None
    paper_only: bool = True
    places_orders: bool = False

    @model_validator(mode="after")
    def reject_live_execution(self) -> DemoCloseRequest:
        if self.places_orders:
            raise ValueError("demo close cannot place orders")
        self.paper_only = True
        return self


class DemoPoolCheck(BaseModel):
    venue: VenueName
    native_currency: str
    seed_native: Decimal
    available_cash: Decimal
    locked_capital: Decimal
    gbp_carrying_value: Decimal | None = None
    gbp_carrying_status: str
    fx_source: str | None = None


class DemoWalkthroughSnapshot(BaseModel):
    data_kind: str
    label: str
    paper_only: bool = True
    execution_enabled: bool = False
    mode: str = "paper"
    treasury: PaperTreasurySnapshot
    pools: list[DemoPoolCheck] = Field(default_factory=list)
    book: PaperTradeBookSummary
    active_trades: list[PaperTrade] = Field(default_factory=list)
    closed_trades: list[PaperTrade] = Field(default_factory=list)
    live_triggered: list[NearOpportunity] = Field(default_factory=list)
    live_near: list[NearOpportunity] = Field(default_factory=list)
    discovery: LiveRefreshStatus | None = None
    hold_vs_unwind: UnwindDecision | None = None
    replay: "FixtureReplayResult | None" = None
    notes: list[str] = Field(default_factory=list)


class FixtureReplayResult(BaseModel):
    data_kind: str = DEMO_DATA_KIND
    label: str = DEMO_FIXTURE_LABEL
    paper_only: bool = True
    execution_enabled: bool = False
    venue_pair: VenuePair
    solver: SolverKind
    close_via: CloseVia
    venues: list[VenueName] = Field(default_factory=list)
    fill_kinds: list[str] = Field(default_factory=list)
    decision: PaperScanDecision | None = None
    trade: PaperTradeDetail | None = None
    unwind: UnwindDecision | None = None
    quotes: list[ReverseQuote] = Field(default_factory=list)
    treasury: PaperTreasurySnapshot
    journal_balanced: bool = False
    opportunity_id: str | None = None
    qualify_only: bool = False
    preparable_opportunities: list[PreparablePaperOpportunity] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DemoWalkthroughService:
    def __init__(
        self,
        *,
        operations: PaperOperationsService,
        scan: PaperScanService,
        watchlist: WatchlistService,
        ledger: SqlitePaperLedger,
        liquidity: SqlitePaperLiquidityRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.operations = operations
        self.scan = scan
        self.watchlist = watchlist
        self.ledger = ledger
        self.liquidity = liquidity
        self.settings = settings or get_settings()
        self._last_replay: FixtureReplayResult | None = None
        self._quotes_by_trade: dict[str, list[ReverseQuote]] = {}
        self._quotes_by_opportunity: dict[str, list[ReverseQuote]] = {}

    def snapshot(self) -> DemoWalkthroughSnapshot:
        treasury = self.ledger.treasury.snapshot()
        live_near = self.watchlist.top_near(limit=25)
        live_triggered = self.watchlist.triggered(limit=25)
        replay = self._last_replay
        notes = [
            "PAPER MODE. execution_enabled=false. No venue place/cancel/sign path.",
            "Matchbook GBP, Polymarket USD and Kalshi USD are separate native pools.",
            "GBP figures are carrying values, not spendable native cash.",
            "Clock estimates never release capital.",
            "Empty live watchlists stay empty; DEMO / FIXTURE REPLAY is never substituted into them.",
        ]
        if replay is not None:
            notes.append(f"{DEMO_FIXTURE_LABEL} is labelled separately from live watchlists.")
        return DemoWalkthroughSnapshot(
            data_kind=LIVE_DATA_KIND,
            label="PAPER MODE operator demo",
            execution_enabled=False,
            treasury=treasury,
            pools=_pool_checks(treasury),
            book=self.operations.book_summary(),
            active_trades=self.operations.list_active_trades(),
            closed_trades=self.operations.list_closed_trades(),
            live_triggered=live_triggered,
            live_near=live_near,
            discovery=get_live_refresh_coordinator().status,
            hold_vs_unwind=self._current_unwind(),
            replay=replay,
            notes=notes,
        )

    def reset(self, request: DemoResetRequest) -> DemoWalkthroughSnapshot:
        settings = self.settings
        seed = request.seed_gbp or Decimal(str(settings.paper_treasury_seed_gbp or DEMO_SEED_GBP))
        rate = request.usd_gbp_per_unit or Decimal(
            str(settings.paper_treasury_demo_usd_gbp_per_unit or DEFAULT_PAPER_FX_USD)
        )
        source = request.fx_source or settings.paper_treasury_demo_fx_source
        now = datetime.now(UTC)
        if request.reinitialize_store:
            self._reinitialize_store(
                seed_gbp=seed,
                usd_gbp_per_unit=rate,
                fx_source=source,
                include_kalshi=request.include_kalshi,
                reason=request.reason,
                now=now,
            )
        else:
            try:
                self.ledger.treasury.reset_demo_session(
                    seed_gbp=seed,
                    usd_gbp_per_unit=rate,
                    fx_source=source,
                    include_kalshi=request.include_kalshi,
                    reason=request.reason,
                    now=now,
                )
            except PaperTreasuryError as exc:
                raise PaperOperationsError(str(exc)) from exc
        self._align_liquidity()
        self.operations._plans.clear()
        self.operations._preparations.clear()
        self.operations._latest_preparation_by_opportunity.clear()
        self.operations._external_confirmations.clear()
        self.operations._entry_rejections.clear()
        self._last_replay = None
        self._quotes_by_trade.clear()
        self._quotes_by_opportunity.clear()
        return self.snapshot()

    def replay(self, request: FixtureReplayRequest) -> FixtureReplayResult:
        if request.places_orders:
            raise PaperOperationsError("fixture replay cannot place orders")
        left, right = fixture_pair(request.venue_pair, solver=request.solver)
        replay_suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        costs = fixture_venue_costs(left, right)
        usd_native = self._usd_seed_native()
        liquidity = default_pools(
            matchbook_gbp=Decimal(str(self.settings.paper_treasury_seed_gbp)),
            polymarket_usd=usd_native,
            kalshi_usd=usd_native,
        )
        from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
        from sports_hedge.paper.liquidity import PaperLiquiditySnapshot

        decision = self.scan.scan_pair(
            left,
            right,
            venue_costs=costs,
            fx_snapshots=list(DEMO_FX),
            maximum_execution_risk=100,
            liquidity_snapshot=PaperLiquiditySnapshot(
                pools=liquidity, updated_at=datetime.now(UTC)
            ),
        )
        if not decision.eligible_for_paper_simulation:
            raise PaperOperationsError(
                "fixture_replay_not_paper_eligible:"
                + ",".join(decision.rejection_reasons or ["unknown"])
            )
        if not decision.canonical_market_id:
            raise PaperOperationsError("fixture_replay_missing_market_id")
        fixture_captured = [
            observation.observed_at
            for observation in (left, right)
            if observation.observed_at is not None
        ]
        if fixture_captured:
            decision = decision.model_copy(update={"scanned_at": max(fixture_captured)})
        history = self.scan.market_intelligence.market_history(
            canonical_market_id=decision.canonical_market_id
        )
        unique_id = f"{decision.canonical_market_id}:replay:{replay_suffix}"
        decision = decision.model_copy(update={"canonical_market_id": unique_id})
        history = [
            item.model_copy(update={"canonical_market_id": unique_id}) for item in history
        ]
        mapped = observation_from_paper_decision(decision, history)
        if mapped is None:
            raise PaperOperationsError("fixture_replay_watchlist_rejected")
        mapped = mapped.model_copy(update={"data_kind": DEMO_DATA_KIND})
        observation = self.watchlist.observe(mapped)
        self.operations.persist_triggered_chain(
            decision,
            provenance=DataProvenance.FIXTURE_DEMO,
            autofill=False,
        )
        opportunity_id = observation.opportunity_id
        quotes = reverse_quotes_from_observations([left, right])
        self._quotes_by_opportunity[opportunity_id] = quotes
        if request.qualify_only:
            preparable = [
                item
                for item in self.operations.list_preparable()
                if item.opportunity_id == opportunity_id
            ]
            notes = [
                DEMO_FIXTURE_LABEL,
                "Qualified for operator-chosen size. Preview mutates nothing. Confirm revalidates before lock.",
                "Not mixed into empty live watchlists.",
            ]
            result = self._replay_result(
                request=request,
                decision=decision,
                trade=None,
                unwind=None,
                quotes=quotes,
                notes=notes,
                opportunity_id=opportunity_id,
                preparable=preparable,
            )
            self._last_replay = result
            return result
        existing = self.operations._get_trade_by_opportunity(opportunity_id)
        if existing is None or existing.state is not PaperTradeState.OPEN:
            self.operations.simulate_fill(
                opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.FIXTURE_DEMO,
                operator_note="DEMO / FIXTURE REPLAY paper autofill; no venue order placed",
            )
        trade = self.operations._get_trade_by_opportunity(opportunity_id)
        if trade is None or trade.state is not PaperTradeState.OPEN:
            raise PaperOperationsError(
                self.operations._entry_rejections.get(opportunity_id, "fixture_replay_entry_failed")
            )
        quotes = reverse_quotes_from_observations([left, right])
        self._quotes_by_trade[trade.trade_id] = quotes
        self._quotes_by_opportunity[opportunity_id] = quotes
        unwind = None
        if quotes:
            try:
                unwind = self.operations.evaluate_unwind(
                    trade.trade_id, quotes=quotes, fx=list(DEMO_FX)
                )
            except PaperOperationsError:
                unwind = None
        notes = [
            DEMO_FIXTURE_LABEL,
            "Same 8F allocator-sized autofill and 8E treasury path as live paper.",
            "Not mixed into empty live watchlists.",
        ]
        closed: PaperTradeDetail | None = None
        if request.close_via == "hold":
            closed = self.operations.trade_detail(trade.trade_id)
            notes.append("Left OPEN for operator inspection. Clock estimates do not release capital.")
        elif request.close_via == "unwind":
            closed = self._complete_unwind(trade.trade_id, quotes, unwind)
            notes.append("Close path: validated paper unwind posted through 8E.")
        else:
            closed = self._complete_settlement(
                trade.trade_id, request.venue_pair, request.winning_outcome
            )
            notes.append("Close path: labelled paper settlement posted through 8E.")
        result = self._replay_result(
            request=request,
            decision=decision,
            trade=closed,
            unwind=unwind,
            quotes=quotes,
            notes=notes,
            opportunity_id=opportunity_id,
        )
        self._last_replay = result
        return result

    def close_open_trade(self, trade_id: str, request: DemoCloseRequest) -> FixtureReplayResult:
        if request.places_orders:
            raise PaperOperationsError("demo close cannot place orders")
        trade = self.operations.trades.get(trade_id) if self.operations.trades is not None else None
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        quotes = self._quotes_by_trade.get(trade_id) or self._quotes_for_trade(trade)
        unwind = None
        if quotes:
            try:
                unwind = self.operations.evaluate_unwind(
                    trade.trade_id, quotes=quotes, fx=list(DEMO_FX)
                )
            except PaperOperationsError:
                unwind = None
        notes = [DEMO_FIXTURE_LABEL, "Close uses stored labelled reverse-side quotes; no venue orders."]
        if request.close_via == "unwind":
            closed = self._complete_unwind(trade.trade_id, quotes, unwind)
            notes.append("Close path: validated paper unwind posted through 8E.")
        else:
            pair = self._last_replay.venue_pair if self._last_replay is not None else "matchbook_polymarket"
            closed = self._complete_settlement(trade.trade_id, pair, request.winning_outcome)
            notes.append("Close path: labelled paper settlement posted through 8E.")
        result = self._replay_result(
            request=FixtureReplayRequest(
                venue_pair=self._last_replay.venue_pair if self._last_replay else "matchbook_polymarket",
                solver=self._last_replay.solver if self._last_replay else "simple",
                close_via=request.close_via,
            ),
            decision=self._last_replay.decision if self._last_replay else None,
            trade=closed,
            unwind=unwind,
            quotes=quotes,
            notes=notes,
        )
        self._last_replay = result
        return result

    def _complete_unwind(
        self,
        trade_id: str,
        quotes: list[ReverseQuote],
        unwind: UnwindDecision | None,
    ) -> PaperTradeDetail:
        if not quotes:
            raise PaperOperationsError("fixture_replay_unwind_unavailable")
        return self.operations.complete_validated_unwind(
            trade_id,
            quotes=tighten_reverse_quotes(quotes),
            fx=list(DEMO_FX),
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("1000")),
        )

    def _complete_settlement(
        self,
        trade_id: str,
        venue_pair: VenuePair,
        winning_outcome: str | None,
    ) -> PaperTradeDetail:
        trade = self.operations.trades.get(trade_id) if self.operations.trades is not None else None
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        outcome = winning_outcome or _default_winning_outcome(trade)
        return self.operations.settle(
            trade_id,
            PaperSettlementRequest(
                winning_outcome=outcome,
                source="demo_fixture_replay",
                source_id=f"fixture-replay:{venue_pair}:{trade_id}",
                detail="DEMO / FIXTURE REPLAY explicit paper settlement; not inferred from kickoff",
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )

    def _replay_result(
        self,
        *,
        request: FixtureReplayRequest,
        decision: PaperScanDecision | None,
        trade: PaperTradeDetail | None,
        unwind: UnwindDecision | None,
        quotes: list[ReverseQuote],
        notes: list[str],
        opportunity_id: str | None = None,
        preparable: list[PreparablePaperOpportunity] | None = None,
    ) -> FixtureReplayResult:
        resolved_opportunity = opportunity_id or (trade.opportunity_id if trade is not None else None)
        postings = (
            self.operations.journal.postings(opportunity_id=resolved_opportunity)
            if resolved_opportunity
            else []
        )
        fill_kinds = sorted({leg.fill_kind.value for leg in (trade.legs if trade else [])})
        venues = sorted(
            {leg.venue for leg in (trade.legs if trade else [])},
            key=lambda item: item.value,
        )
        return FixtureReplayResult(
            venue_pair=request.venue_pair,
            solver=request.solver,
            close_via=request.close_via,
            venues=venues,
            fill_kinds=fill_kinds,
            decision=decision,
            trade=trade,
            unwind=unwind,
            quotes=quotes,
            treasury=self.ledger.treasury.snapshot(),
            journal_balanced=gbp_is_balanced(postings) if postings else True,
            opportunity_id=resolved_opportunity,
            qualify_only=request.qualify_only,
            preparable_opportunities=preparable or [],
            notes=notes,
        )

    def _quotes_for_trade(self, trade: PaperTrade) -> list[ReverseQuote]:
        stored = self._quotes_by_trade.get(trade.trade_id)
        if stored:
            return stored
        return self._quotes_by_opportunity.get(trade.opportunity_id, [])

    def _current_unwind(self) -> UnwindDecision | None:
        if self._last_replay is not None and self._last_replay.unwind is not None:
            if self._last_replay.trade is None or self._last_replay.trade.state is PaperTradeState.OPEN:
                return self._last_replay.unwind
        for trade in self.operations.list_active_trades():
            quotes = self._quotes_for_trade(trade)
            if not quotes:
                continue
            try:
                return self.operations.evaluate_unwind(
                    trade.trade_id, quotes=quotes, fx=list(DEMO_FX)
                )
            except PaperOperationsError:
                continue
        return self._last_replay.unwind if self._last_replay is not None else None

    def _reinitialize_store(
        self,
        *,
        seed_gbp: Decimal,
        usd_gbp_per_unit: Decimal,
        fx_source: str,
        include_kalshi: bool,
        reason: str,
        now: datetime,
    ) -> None:
        session = self.ledger.treasury.active_session()
        session_id = session.session_id if session is not None else f"demo-{now.isoformat()}"
        self.ledger.treasury.release_open_locks_for_demo_reset(reason=reason, now=now)
        self.operations.abandon_open_trades_for_demo_reset(reason=reason, now=now)
        self.ledger.trades.archive_identities(session_id)
        try:
            self.ledger.treasury.reset_demo_session(
                seed_gbp=seed_gbp,
                usd_gbp_per_unit=usd_gbp_per_unit,
                fx_source=fx_source,
                include_kalshi=include_kalshi,
                reason=reason,
                now=now,
            )
        except PaperTreasuryError as exc:
            raise PaperOperationsError(str(exc)) from exc

    def _align_liquidity(self) -> None:
        if self.liquidity is None:
            return
        self.liquidity.sync_from_treasury_pools(self.ledger.treasury.snapshot().pools)

    def _usd_seed_native(self) -> Decimal:
        seed = Decimal(str(self.settings.paper_treasury_seed_gbp))
        rate = Decimal(str(self.settings.paper_treasury_demo_usd_gbp_per_unit))
        return (seed / rate).quantize(Decimal("0.00000001"))


def _pool_checks(snapshot: PaperTreasurySnapshot) -> list[DemoPoolCheck]:
    return [
        DemoPoolCheck(
            venue=pool.venue,
            native_currency=pool.native_currency,
            seed_native=pool.seed_native,
            available_cash=pool.available_cash,
            locked_capital=pool.locked_capital,
            gbp_carrying_value=pool.gbp_carrying_value,
            gbp_carrying_status=pool.gbp_carrying_status,
            fx_source=pool.fx_source,
        )
        for pool in snapshot.pools
    ]


def _default_winning_outcome(trade: PaperTrade) -> str:
    for leg in trade.legs:
        if leg.filled_stake > 0:
            return leg.outcome
    raise PaperOperationsError("fixture_replay_missing_winning_outcome")
