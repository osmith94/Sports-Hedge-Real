"""PAPER-only settlement/reconciliation over open trades.

Exact-ID read-only Matchbook/Kalshi/Polymarket result evidence. Reuses
``PaperOperationsService.settle()`` / ``compute_paper_settlement()`` / treasury 8E.
Does not discover markets, place orders, or infer results from kickoff time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.provider_access import (
    PRICE_ENGINE_SETTLEMENT_LANE,
    ProviderAccessLayer,
    get_shared_provider_access,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.active_trade_journal import ActiveTradeEventType, ActiveTradeReasonCode
from sports_hedge.paper.provider_identity import (
    catalogue_rows_for_trade,
    recover_persisted_catalogue_identity,
)
from sports_hedge.paper.result_resolution import (
    PAPER_AUTO_SETTLEMENT_SOURCE,
    SettlementResolution,
    resolve_paper_trade_settlement,
)
from sports_hedge.lifecycle.paper import SETTLEABLE_TRADE_STATES, auto_settle_eligible
from sports_hedge.paper.trades import (
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeState,
    SettlementReconciliationStatus,
)
from sports_hedge.venues.matchbook import MatchbookMarketGoneError

LOGGER = logging.getLogger(__name__)

SETTLEABLE_STATES = SETTLEABLE_TRADE_STATES


@dataclass
class PaperSettlementTradeResult:
    trade_id: str
    settled: bool = False
    blocker: str | None = None
    winning_outcome: str | None = None
    source_id: str | None = None


@dataclass
class PaperSettlementCycleResult:
    examined: int = 0
    settled_trade_ids: list[str] = field(default_factory=list)
    blocked: list[PaperSettlementTradeResult] = field(default_factory=list)
    skipped: int = 0


@dataclass
class _FetchedSettlementEvidence:
    matchbook_market: dict[str, Any] | None = None
    matchbook_event: dict[str, Any] | None = None
    kalshi_markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    polymarket_market: dict[str, Any] | None = None
    polymarket_event: dict[str, Any] | None = None
    fetch_blocker: str | None = None


class PaperSettlementAgent:
    """Narrow reconciliation worker for persisted OPEN/PARTIAL PAPER trades."""

    def __init__(
        self,
        *,
        operations: PaperOperationsService,
        matchbook: Any | None = None,
        kalshi: Any | None = None,
        polymarket: Any | None = None,
        provider_access: ProviderAccessLayer | None = None,
        clock: Callable[[], datetime] | None = None,
        settings: Settings | None = None,
        provider_timeout_seconds: float | None = None,
        catalogue: Any | None = None,
    ) -> None:
        self.operations = operations
        self.matchbook = matchbook
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.catalogue = catalogue
        self.provider_access = (
            provider_access if provider_access is not None else get_shared_provider_access()
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        resolved = settings or get_settings()
        self._timeout = float(
            resolved.paper_scan_provider_timeout_seconds
            if provider_timeout_seconds is None
            else provider_timeout_seconds
        )

    def now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current

    async def run_cycle(self, *, now: datetime | None = None) -> PaperSettlementCycleResult:
        when = now or self.now()
        result = PaperSettlementCycleResult()
        from sports_hedge.application.event_loop_activity import (
            close_loop_slice,
            mark_loop_phase,
        )

        mark_loop_phase(lane="active_trade", phase="paper_settlement")
        trades = [
            trade
            for trade in self.operations.list_active_trades()
            if auto_settle_eligible(trade.state)
        ]
        result.examined = len(trades)
        close_loop_slice()
        for trade in trades:
            try:
                item = await self.reconcile_trade(trade, now=when)
            except Exception:
                LOGGER.exception("paper settlement cycle failed for %s", trade.trade_id)
                item = PaperSettlementTradeResult(
                    trade_id=trade.trade_id, blocker="settlement_cycle_error"
                )
                self._record_blocker(
                    trade,
                    "settlement_cycle_error",
                    SettlementResolution(
                        winning_outcome=None,
                        blocker="settlement_cycle_error",
                        source_id=f"cycle-error:{trade.trade_id}",
                        detail="settlement cycle exception",
                    ),
                    when,
                )
            if item.settled:
                result.settled_trade_ids.append(item.trade_id)
            elif item.blocker:
                result.blocked.append(item)
            else:
                result.skipped += 1
        return result

    async def reconcile_trade(
        self,
        trade: PaperTrade,
        *,
        now: datetime | None = None,
    ) -> PaperSettlementTradeResult:
        when = now or self.now()
        if not auto_settle_eligible(trade.state) and trade.state is not PaperTradeState.CLOSED:
            return PaperSettlementTradeResult(
                trade_id=trade.trade_id, blocker="illegal_auto_settle_state"
            )
        if trade.state is PaperTradeState.CLOSED:
            self.operations.record_settlement_reconciliation(
                trade,
                status=SettlementReconciliationStatus.SETTLED,
                blocker=None,
                detail=None,
                now=when,
            )
            return PaperSettlementTradeResult(trade_id=trade.trade_id)
        trade, recovered = recover_persisted_catalogue_identity(
            trade, catalogue_rows=self._catalogue_rows(trade), now=when
        )
        if recovered and self.operations.trades is not None:
            try:
                self.operations.trades.save(trade)
            except Exception:
                LOGGER.exception("failed to persist recovered provider identity for %s", trade.trade_id)
        evidence = await self._fetch_evidence(trade)
        matchbook_market = evidence.matchbook_market
        matchbook_event = evidence.matchbook_event
        kalshi_markets = evidence.kalshi_markets
        polymarket_market = evidence.polymarket_market
        polymarket_event = evidence.polymarket_event
        fetch_blocker = evidence.fetch_blocker
        from sports_hedge.ncaab.settlement import (
            is_ncaab_paper_trade,
            ncaab_automatic_settlement_blocker,
            ncaab_missing_venue_evidence_blocker,
        )

        if is_ncaab_paper_trade(trade):
            fetched: set[VenueName] = set()
            if matchbook_market is not None or matchbook_event is not None:
                fetched.add(VenueName.MATCHBOOK)
            if kalshi_markets:
                fetched.add(VenueName.KALSHI)
            if polymarket_market is not None or polymarket_event is not None:
                fetched.add(VenueName.POLYMARKET)
            ncaab_blocker = ncaab_missing_venue_evidence_blocker(
                trade, fetched
            ) or ncaab_automatic_settlement_blocker(trade)
            if ncaab_blocker:
                self._record_blocker(
                    trade,
                    ncaab_blocker,
                    SettlementResolution(
                        winning_outcome=None,
                        blocker=ncaab_blocker,
                        source_id=f"ncaab-fail-closed:{trade.trade_id}",
                        detail=ncaab_blocker,
                    ),
                    when,
                )
                return PaperSettlementTradeResult(
                    trade_id=trade.trade_id, blocker=ncaab_blocker
                )
        self._record_nfl_lifecycle(
            trade,
            matchbook_market=matchbook_market,
            matchbook_event=matchbook_event,
            kalshi_markets=kalshi_markets,
            when=when,
        )
        self._record_nba_lifecycle(
            trade,
            matchbook_market=matchbook_market,
            matchbook_event=matchbook_event,
            kalshi_markets=kalshi_markets,
            polymarket_market=polymarket_market,
            polymarket_event=polymarket_event,
            when=when,
        )
        resolution = resolve_paper_trade_settlement(
            trade,
            matchbook_market=matchbook_market,
            matchbook_event=matchbook_event,
            kalshi_markets=kalshi_markets,
            polymarket_market=polymarket_market,
            polymarket_event=polymarket_event,
        )
        if fetch_blocker and not resolution.is_ready:
            if resolution.blocker in {
                None,
                "incomplete_provider_result",
                "missing_durable_provider_identity",
            }:
                resolution = SettlementResolution(
                    winning_outcome=None,
                    blocker=fetch_blocker,
                    source_id=resolution.source_id,
                    detail=resolution.detail,
                    evidence={**resolution.evidence, "blocker": fetch_blocker},
                )
        if resolution.is_ready:
            self.operations.record_settlement_reconciliation(
                trade,
                status=SettlementReconciliationStatus.READY,
                blocker=None,
                detail=resolution.detail,
                now=when,
            )
            return self._settle(trade, resolution, when)
        if resolution.blocker:
            self._record_blocker(trade, resolution.blocker, resolution, when)
            return PaperSettlementTradeResult(
                trade_id=trade.trade_id,
                blocker=resolution.blocker,
                source_id=resolution.source_id,
            )
        self.operations.record_settlement_reconciliation(
            trade,
            status=SettlementReconciliationStatus.BLOCKED,
            blocker="incomplete_provider_result",
            detail=resolution.detail,
            now=when,
        )
        return PaperSettlementTradeResult(trade_id=trade.trade_id)

    def _record_nfl_lifecycle(
        self,
        trade: PaperTrade,
        *,
        matchbook_market: dict[str, Any] | None,
        matchbook_event: dict[str, Any] | None,
        kalshi_markets: dict[str, dict[str, Any]],
        when: datetime,
    ) -> None:
        from sports_hedge.nfl.settlement import (
            collect_nfl_lifecycle_tokens,
            is_nfl_paper_trade,
            nfl_lifecycle_audit_detail,
            nfl_lifecycle_observation,
        )

        if not is_nfl_paper_trade(trade):
            return
        tokens = collect_nfl_lifecycle_tokens(
            matchbook_market,
            matchbook_event,
            *list((kalshi_markets or {}).values()),
        )
        observation = nfl_lifecycle_observation(tokens, observed_at=when)
        detail = nfl_lifecycle_audit_detail(observation)
        if any(
            event.event_type is PaperTradeAuditEventType.NFL_LIFECYCLE_OBSERVED
            and event.detail == detail
            for event in trade.audit
        ):
            return
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=when,
                event_type=PaperTradeAuditEventType.NFL_LIFECYCLE_OBSERVED,
                detail=detail,
            )
        )
        if self.operations.trades is not None:
            try:
                self.operations.trades.save(trade)
            except Exception:
                LOGGER.exception("failed to persist NFL lifecycle evidence for %s", trade.trade_id)

    def _record_nba_lifecycle(
        self,
        trade: PaperTrade,
        *,
        matchbook_market: dict[str, Any] | None,
        matchbook_event: dict[str, Any] | None,
        kalshi_markets: dict[str, dict[str, Any]],
        polymarket_market: dict[str, Any] | None,
        polymarket_event: dict[str, Any] | None,
        when: datetime,
    ) -> None:
        from sports_hedge.nba.settlement import (
            collect_nba_lifecycle_tokens,
            is_nba_paper_trade,
            nba_lifecycle_audit_detail,
            nba_lifecycle_observation,
        )

        if not is_nba_paper_trade(trade):
            return
        tokens = collect_nba_lifecycle_tokens(
            matchbook_market,
            matchbook_event,
            polymarket_market,
            polymarket_event,
            *list((kalshi_markets or {}).values()),
        )
        observation = nba_lifecycle_observation(tokens, observed_at=when)
        detail = nba_lifecycle_audit_detail(observation)
        if any(
            event.event_type is PaperTradeAuditEventType.NBA_LIFECYCLE_OBSERVED
            and event.detail == detail
            for event in trade.audit
        ):
            return
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=when,
                event_type=PaperTradeAuditEventType.NBA_LIFECYCLE_OBSERVED,
                detail=detail,
            )
        )
        if self.operations.trades is not None:
            try:
                self.operations.trades.save(trade)
            except Exception:
                LOGGER.exception("failed to persist NBA lifecycle evidence for %s", trade.trade_id)

    def _settle(
        self,
        trade: PaperTrade,
        resolution: SettlementResolution,
        when: datetime,
    ) -> PaperSettlementTradeResult:
        request = PaperSettlementRequest(
            winning_outcome=str(resolution.winning_outcome),
            source=PAPER_AUTO_SETTLEMENT_SOURCE,
            source_id=resolution.source_id,
            settled_at=when,
            detail=resolution.detail,
            provenance=DataProvenance.LIVE_PAPER,
        )
        try:
            settled = self.operations.settle(trade.trade_id, request, now=when)
        except PaperOperationsError as exc:
            reason = str(exc)
            if reason == "conflicting_settlement":
                self._record_blocker(trade, "conflicting_settlement", resolution, when)
                return PaperSettlementTradeResult(
                    trade_id=trade.trade_id, blocker="conflicting_settlement"
                )
            if reason in {"already_unwound", "unknown_trade"}:
                return PaperSettlementTradeResult(trade_id=trade.trade_id, blocker=reason)
            self._record_blocker(trade, reason, resolution, when)
            return PaperSettlementTradeResult(trade_id=trade.trade_id, blocker=reason)
        try:
            from sports_hedge.application.active_trade_lane import get_active_trade_registry

            get_active_trade_registry().drop(settled.trade_id)
        except Exception:
            pass
        return PaperSettlementTradeResult(
            trade_id=settled.trade_id,
            settled=True,
            winning_outcome=settled.settlement_outcome,
            source_id=settled.settlement_source_id,
        )

    async def _fetch_evidence(self, trade: PaperTrade) -> _FetchedSettlementEvidence:
        matchbook_market = None
        matchbook_event = None
        kalshi_markets: dict[str, dict[str, Any]] = {}
        polymarket_market = None
        polymarket_event = None
        mb_event, mb_market = _matchbook_ids(trade)
        pm_event, pm_market = _polymarket_ids(trade)
        needed_matchbook = bool(mb_event and mb_market)
        needed_kalshi = bool(_kalshi_tickers(trade))
        needed_polymarket = bool(pm_market)
        if needed_matchbook and self.matchbook is None:
            return _FetchedSettlementEvidence(fetch_blocker="provider_unavailable")
        if needed_kalshi and self.kalshi is None:
            return _FetchedSettlementEvidence(fetch_blocker="provider_unavailable")
        if needed_polymarket and self.polymarket is None:
            return _FetchedSettlementEvidence(fetch_blocker="provider_unavailable")
        if mb_event and mb_market and self.matchbook is not None:
            getter = getattr(self.matchbook, "get_market", None)
            if callable(getter):
                payload, status = await self._provider_call(
                    VenueName.MATCHBOOK,
                    stage="get_market",
                    source_id=str(mb_market),
                    factory=lambda: getter(mb_event, mb_market),
                )
                if status == "timeout":
                    return _FetchedSettlementEvidence(fetch_blocker="incomplete_provider_result")
                if status == "unavailable":
                    return _FetchedSettlementEvidence(fetch_blocker="provider_unavailable")
                if payload is not None:
                    matchbook_market = payload
            event_getter = getattr(self.matchbook, "get_event", None)
            if callable(event_getter):
                payload, status = await self._provider_call(
                    VenueName.MATCHBOOK,
                    stage="get_event",
                    source_id=str(mb_event),
                    factory=lambda: event_getter(mb_event),
                )
                if status == "timeout":
                    return _FetchedSettlementEvidence(
                        matchbook_market=matchbook_market,
                        fetch_blocker="incomplete_provider_result",
                    )
                if status == "unavailable":
                    return _FetchedSettlementEvidence(
                        matchbook_market=matchbook_market,
                        fetch_blocker="provider_unavailable",
                    )
                if payload is not None:
                    matchbook_event = payload
        for ticker in _kalshi_tickers(trade):
            if self.kalshi is None:
                break
            getter = getattr(self.kalshi, "get_market", None)
            if not callable(getter):
                break
            payload, status = await self._provider_call(
                VenueName.KALSHI,
                stage="get_market",
                source_id=ticker,
                factory=lambda ticker=ticker: _kalshi_get_market(getter, ticker),
            )
            if status == "timeout":
                return _FetchedSettlementEvidence(
                    matchbook_market=matchbook_market,
                    matchbook_event=matchbook_event,
                    kalshi_markets=kalshi_markets,
                    fetch_blocker="incomplete_provider_result",
                )
            if status == "unavailable":
                return _FetchedSettlementEvidence(
                    matchbook_market=matchbook_market,
                    matchbook_event=matchbook_event,
                    kalshi_markets=kalshi_markets,
                    fetch_blocker="provider_unavailable",
                )
            if payload is not None:
                kalshi_markets[ticker] = payload
        if pm_market and self.polymarket is not None:
            getter = getattr(self.polymarket, "get_market", None)
            if not callable(getter):
                return _FetchedSettlementEvidence(
                    matchbook_market=matchbook_market,
                    matchbook_event=matchbook_event,
                    kalshi_markets=kalshi_markets,
                    fetch_blocker="provider_unavailable",
                )
            payload, status = await self._provider_call(
                VenueName.POLYMARKET,
                stage="get_market",
                source_id=str(pm_market),
                factory=lambda: getter(pm_market),
            )
            if status == "timeout":
                return _FetchedSettlementEvidence(
                    matchbook_market=matchbook_market,
                    matchbook_event=matchbook_event,
                    kalshi_markets=kalshi_markets,
                    fetch_blocker="incomplete_provider_result",
                )
            if status == "unavailable":
                return _FetchedSettlementEvidence(
                    matchbook_market=matchbook_market,
                    matchbook_event=matchbook_event,
                    kalshi_markets=kalshi_markets,
                    fetch_blocker="provider_unavailable",
                )
            if payload is not None:
                polymarket_market = payload
            event_getter = getattr(self.polymarket, "get_event", None)
            if callable(event_getter) and pm_event:
                payload, status = await self._provider_call(
                    VenueName.POLYMARKET,
                    stage="get_event",
                    source_id=str(pm_event),
                    factory=lambda: event_getter(pm_event),
                )
                if status == "timeout":
                    return _FetchedSettlementEvidence(
                        matchbook_market=matchbook_market,
                        matchbook_event=matchbook_event,
                        kalshi_markets=kalshi_markets,
                        polymarket_market=polymarket_market,
                        fetch_blocker="incomplete_provider_result",
                    )
                if status == "unavailable":
                    return _FetchedSettlementEvidence(
                        matchbook_market=matchbook_market,
                        matchbook_event=matchbook_event,
                        kalshi_markets=kalshi_markets,
                        polymarket_market=polymarket_market,
                        fetch_blocker="provider_unavailable",
                    )
                if payload is not None:
                    polymarket_event = payload
        return _FetchedSettlementEvidence(
            matchbook_market=matchbook_market,
            matchbook_event=matchbook_event,
            kalshi_markets=kalshi_markets,
            polymarket_market=polymarket_market,
            polymarket_event=polymarket_event,
        )

    async def _provider_call(
        self,
        venue: VenueName,
        *,
        stage: str,
        source_id: str,
        factory: Callable[[], Any],
    ) -> tuple[Any, str | None]:
        del source_id
        access = self.provider_access
        timeout = self._timeout
        try:
            if access is None:
                payload = await asyncio.wait_for(factory(), timeout=timeout)
                return payload, None
            async with access.acquire(
                venue, lane=PRICE_ENGINE_SETTLEMENT_LANE, stage=stage
            ):
                payload = await asyncio.wait_for(factory(), timeout=timeout)
                return payload, None
        except TimeoutError:
            return None, "timeout"
        except MatchbookMarketGoneError:
            return None, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("paper settlement %s %s unavailable: %s", venue.value, stage, exc)
            return None, "unavailable"

    def _record_blocker(
        self,
        trade: PaperTrade,
        reason: str,
        resolution: SettlementResolution,
        when: datetime,
    ) -> None:
        self.operations.record_settlement_reconciliation(
            trade,
            status=SettlementReconciliationStatus.BLOCKED,
            blocker=reason,
            detail=resolution.detail,
            now=when,
            persist=False,
        )
        last = _last_blocker(trade)
        if last == reason:
            if self.operations.trades is not None:
                try:
                    self.operations.trades.save(trade)
                except Exception:
                    LOGGER.exception("failed to persist settlement check for %s", trade.trade_id)
            return
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=when,
                event_type=PaperTradeAuditEventType.SETTLEMENT_BLOCKED,
                detail=f"{reason}: {resolution.detail}",
            )
        )
        if self.operations.trades is not None:
            try:
                self.operations.trades.save(trade)
            except Exception:
                LOGGER.exception("failed to persist settlement blocker for %s", trade.trade_id)
        event_type = (
            ActiveTradeEventType.SETTLEMENT_INCOMPLETE
            if reason == "incomplete_provider_result"
            else ActiveTradeEventType.SETTLEMENT_BLOCKED
        )
        reason_code = (
            ActiveTradeReasonCode.SETTLEMENT_INCOMPLETE
            if reason == "incomplete_provider_result"
            else ActiveTradeReasonCode.SETTLEMENT_BLOCKED
        )
        self.operations.record_active_lifecycle_event(
            trade,
            event_type=event_type,
            reason_code=reason_code,
            operator_copy=f"PAPER settlement blocked: {reason}",
            occurred_at=when,
            dedupe_key=f"settlement-block:{trade.trade_id}:{reason}",
            payload={
                "blocker": reason,
                "source_id": resolution.source_id,
                "evidence": resolution.evidence,
            },
        )

    def _catalogue_rows(self, trade: PaperTrade) -> list[Any]:
        return catalogue_rows_for_trade(self.catalogue, trade)


def _last_blocker(trade: PaperTrade) -> str | None:
    for event in reversed(trade.audit):
        if event.event_type is PaperTradeAuditEventType.SETTLEMENT_BLOCKED:
            detail = event.detail or ""
            return detail.split(":", 1)[0]
    return None


def _matchbook_ids(trade: PaperTrade) -> tuple[str | None, str | None]:
    event_id = None
    market_id = None
    for leg in trade.legs:
        if leg.venue is not VenueName.MATCHBOOK:
            continue
        event_id = event_id or (str(leg.source_event_id).strip() if leg.source_event_id else None)
        market_id = market_id or (str(leg.source_market_id).strip() if leg.source_market_id else None)
    return event_id or None, market_id or None


def _polymarket_ids(trade: PaperTrade) -> tuple[str | None, str | None]:
    event_id = None
    market_id = None
    for leg in trade.legs:
        if leg.venue is not VenueName.POLYMARKET:
            continue
        event_id = event_id or (str(leg.source_event_id).strip() if leg.source_event_id else None)
        market_id = market_id or (str(leg.source_market_id).strip() if leg.source_market_id else None)
    return event_id or None, market_id or None


def _kalshi_tickers(trade: PaperTrade) -> list[str]:
    tickers: list[str] = []
    for leg in trade.legs:
        if leg.venue is not VenueName.KALSHI:
            continue
        ticker = str(leg.source_contract_id or leg.source_market_id or "").strip()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def _kalshi_get_market(getter: Callable[..., Any], ticker: str) -> Any:
    try:
        return getter(ticker, use_cache=False)
    except TypeError:
        return getter(ticker)
