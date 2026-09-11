from __future__ import annotations

from decimal import Decimal

from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.arbitrage.depth import DepthAwareCompleteSetScanner, DepthQuoteSource
from sports_hedge.domain.football import CanonicalOutcome, SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.identity import (
    canonical_matched_event_id,
    canonical_matched_market_id,
    canonical_source_event_id,
    canonical_source_market_id,
)
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskScorer


class PaperScanService:
    """Orchestrate strict matching, snapshot capture and paper-only arb analysis."""

    def __init__(
        self,
        market_intelligence: MarketIntelligenceService,
        *,
        market_matcher: MarketMatcher | None = None,
        depth_scanner: DepthAwareCompleteSetScanner | None = None,
        risk_scorer: ExecutionRiskScorer | None = None,
    ) -> None:
        self.market_intelligence = market_intelligence
        self.market_matcher = market_matcher or MarketMatcher()
        self.depth_scanner = depth_scanner or DepthAwareCompleteSetScanner()
        self.risk_scorer = risk_scorer or ExecutionRiskScorer()

    def record_observation(self, observation: VenueMarketObservation) -> int:
        event_id = canonical_source_event_id(observation.market.event)
        market_id = canonical_source_market_id(event_id, observation.market)
        return self._record_with_ids(observation, event_id=event_id, market_id=market_id)

    def scan_pair(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        fee_snapshots: list[FeeSnapshot] | None = None,
        fx_snapshots: list[FxRateSnapshot] | None = None,
        capital_limit_gbp: Decimal | None = None,
        minimum_net_edge: Decimal = Decimal("0.005"),
        maximum_execution_risk: int = 60,
        minimum_mapping_confidence: float = 0.98,
        assumed_latency_ms: int = 500,
        recent_volatility_bps: float = 0.0,
    ) -> PaperScanDecision:
        if minimum_net_edge < 0:
            raise ValueError("minimum_net_edge must be non-negative")
        if not 0 <= maximum_execution_risk <= 100:
            raise ValueError("maximum_execution_risk must be between 0 and 100")
        if not 0 <= minimum_mapping_confidence <= 1:
            raise ValueError("minimum_mapping_confidence must be between 0 and 1")

        match = self.market_matcher.match(left.market, right.market)
        fees = list(fee_snapshots or [])
        fx = _with_gbp_rate(list(fx_snapshots or []))
        rejections: list[str] = []

        if not match.matched:
            recorded = self.record_observation(left) + self.record_observation(right)
            return PaperScanDecision(
                market_match=match,
                snapshots_recorded=recorded,
                rejection_reasons=["market_not_equivalent", *match.reasons],
                fee_snapshots=fees,
                fx_snapshots=fx,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
            )

        event_id = canonical_matched_event_id([left.market.event, right.market.event])
        market_id = canonical_matched_market_id(event_id, [left.market, right.market])
        recorded = self._record_with_ids(left, event_id=event_id, market_id=market_id)
        recorded += self._record_with_ids(right, event_id=event_id, market_id=market_id)

        if left.venue == right.venue:
            rejections.append("same_venue_pair")
        if match.confidence < minimum_mapping_confidence:
            rejections.append("mapping_confidence_below_threshold")
        if (
            left.market.settlement.scope == SettlementScope.UNKNOWN
            or right.market.settlement.scope == SettlementScope.UNKNOWN
        ):
            rejections.append("unknown_settlement_scope")

        expected_outcomes = [runner.outcome for runner in left.market.runners]
        if any(outcome == CanonicalOutcome.OTHER for outcome in expected_outcomes):
            rejections.append("noncanonical_outcome_space")

        fee_map = {snapshot.venue: snapshot for snapshot in fees}
        scan_fees: dict[VenueName, FeeSnapshot] = {}
        for observation in (left, right):
            fee = fee_map.get(observation.venue)
            if fee is None:
                rejections.append(f"missing_fee_snapshot:{observation.venue.value}")
                fee = FeeSnapshot(
                    venue=observation.venue,
                    profit_haircut_rate=Decimal("0"),
                    source="diagnostic_zero_fallback",
                    detail="Not eligible for paper simulation until an explicit fee assumption is supplied",
                )
            scan_fees[observation.venue] = fee

        fx_map = {snapshot.currency: snapshot for snapshot in fx}
        missing_fx = sorted(
            {
                observation.native_currency
                for observation in (left, right)
                if observation.native_currency not in fx_map
            }
        )
        if missing_fx:
            rejections.extend(f"missing_fx_rate:{currency}" for currency in missing_fx)
            return PaperScanDecision(
                market_match=match,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                snapshots_recorded=recorded,
                rejection_reasons=_dedupe(rejections),
                fee_snapshots=list(scan_fees.values()),
                fx_snapshots=fx,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
            )

        sources: list[DepthQuoteSource] = []
        for observation in (left, right):
            rate = fx_map[observation.native_currency].gbp_per_unit
            fee = scan_fees[observation.venue]
            for book in observation.outcome_books:
                if not book.back_levels:
                    continue
                sources.append(
                    DepthQuoteSource(
                        outcome=book.outcome.value,
                        venue=observation.venue,
                        source_market_id=observation.market.source_market_id,
                        source_runner_id=book.source_runner_id,
                        levels=[
                            BookLevel(
                                decimal_odds=level.decimal_odds,
                                available_stake=level.available_stake * rate,
                            )
                            for level in book.back_levels
                        ],
                        fee_snapshot=fee,
                    )
                )

        depth_scan = self.depth_scanner.scan(
            sources,
            expected_outcomes=[outcome.value for outcome in expected_outcomes],
            capital_limit=capital_limit_gbp,
        )
        solution = depth_scan.solution
        if not solution.is_arbitrage:
            rejections.append(solution.rejection_reason or "no_arbitrage")
            return PaperScanDecision(
                market_match=match,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                snapshots_recorded=recorded,
                depth_scan=depth_scan,
                rejection_reasons=_dedupe(rejections),
                fee_snapshots=list(scan_fees.values()),
                fx_snapshots=fx,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
            )

        if solution.roi < minimum_net_edge:
            rejections.append("net_edge_below_threshold")

        risk = self.risk_scorer.score(
            self._risk_inputs(
                left,
                right,
                depth_scan=depth_scan,
                assumed_latency_ms=assumed_latency_ms,
                recent_volatility_bps=recent_volatility_bps,
            )
        )
        if risk.score > maximum_execution_risk:
            rejections.append("execution_risk_above_threshold")

        return PaperScanDecision(
            market_match=match,
            canonical_event_id=event_id,
            canonical_market_id=market_id,
            snapshots_recorded=recorded,
            depth_scan=depth_scan,
            execution_risk=risk,
            eligible_for_paper_simulation=not rejections,
            rejection_reasons=_dedupe(rejections),
            fee_snapshots=list(scan_fees.values()),
            fx_snapshots=fx,
            minimum_net_edge=minimum_net_edge,
            maximum_execution_risk=maximum_execution_risk,
        )

    def _record_with_ids(
        self,
        observation: VenueMarketObservation,
        *,
        event_id: str,
        market_id: str,
    ) -> int:
        snapshots = observation.snapshots(
            canonical_event_id=event_id,
            canonical_market_id=market_id,
        )
        for snapshot in snapshots:
            self.market_intelligence.record_snapshot(snapshot)
        return len(snapshots)

    def _risk_inputs(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        depth_scan,
        assumed_latency_ms: int,
        recent_volatility_bps: float,
    ) -> ExecutionRiskInputs:
        observation_by_venue = {left.venue: left, right.venue: right}
        spread_bps = 0.0
        quote_age_ms = 0
        size_to_depth_ratio = 0.0
        hedge_liquidity_ratio = 1.0

        stakes = {stake.outcome: stake for stake in depth_scan.solution.stakes}
        for selected in depth_scan.selected_quotes:
            observation = observation_by_venue[selected.venue]
            book = observation.book_for(CanonicalOutcome(selected.outcome))
            if book is not None:
                spread_bps = max(spread_bps, book.probability_spread_bps)
            quote_age_ms = max(quote_age_ms, observation.quote_age_ms)
            stake = stakes.get(selected.outcome)
            if stake is not None and selected.cumulative_depth > 0:
                ratio = float(stake.stake / selected.cumulative_depth)
                size_to_depth_ratio = max(size_to_depth_ratio, ratio)
                hedge_liquidity_ratio = min(
                    hedge_liquidity_ratio,
                    min(float(selected.cumulative_depth / stake.stake), 1.0),
                )

        observed_at = max(left.observed_at, right.observed_at)
        minutes_to_kickoff = max(
            0.0,
            (left.market.event.kickoff_utc - observed_at).total_seconds() / 60.0,
        )
        return ExecutionRiskInputs(
            spread_bps=spread_bps,
            size_to_depth_ratio=size_to_depth_ratio,
            quote_age_ms=quote_age_ms,
            recent_volatility_bps=recent_volatility_bps,
            leg_count=len(depth_scan.solution.stakes),
            minutes_to_kickoff=minutes_to_kickoff,
            assumed_latency_ms=assumed_latency_ms,
            hedge_liquidity_ratio=hedge_liquidity_ratio,
        )


def _with_gbp_rate(rates: list[FxRateSnapshot]) -> list[FxRateSnapshot]:
    if not any(rate.currency.upper() == "GBP" for rate in rates):
        rates.append(
            FxRateSnapshot(
                currency="GBP",
                gbp_per_unit=Decimal("1"),
                source="functional_currency",
            )
        )
    return rates


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
