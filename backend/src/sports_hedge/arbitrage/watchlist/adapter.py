from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sports_hedge.arbitrage.watchlist.economics import (
    gross_edge_from_quotes,
    net_edge_from_implied_sum,
    quantized_edge,
)
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.application.mapping_review import safe_mapping_review_candidate
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.paper.models import PaperScanDecision


def observation_from_paper_decision(
    decision: PaperScanDecision,
    history: Sequence[MarketSnapshot] = (),
    *,
    quote_age_ms: int | None = None,
    quote_age_basis: str | None = None,
) -> WatchObservation | None:
    """Map a paper scan onto the watchlist ingest contract without changing solver gates."""

    if not decision.canonical_event_id or not decision.canonical_market_id:
        return None

    snapshot = history[-1] if history else None
    fx_map = {item.currency: item.gbp_per_unit for item in decision.fx_snapshots}
    legs: list[WatchLeg] = []
    limiting_depth: Decimal | None = None
    limiting_leg: str | None = None
    implied: Decimal | None = None
    current_edge: Decimal | None = None
    gross_edge: Decimal | None = None
    capital: Decimal | None = None
    guaranteed_profit: Decimal | None = None
    solver_is_arbitrage = False
    extra_rejections: list[str] = []

    if decision.payoff_scan is not None:
        solution = decision.payoff_scan.solution
        solver_is_arbitrage = solution.is_arbitrage
        current_edge = quantized_edge(solution.roi)
        # generalized_payoff has no complete-set implied-probability sum.
        # Leave implied None rather than synthesizing 1/(1+roi).
        if solver_is_arbitrage:
            capital = solution.total_capital_used
            guaranteed_profit = solution.minimum_state_pnl
        stake_by_key = {
            (stake.runner_outcome or "", stake.venue): stake
            for stake in solution.selected_stakes
        }
        for quote in decision.payoff_scan.selected_quotes:
            venue_currency = _currency_for_venue(quote.venue, history)
            if venue_currency is None:
                extra_rejections.append(f"unknown_venue_currency:{quote.venue.value}")
                continue
            gbp_rate = fx_map.get(venue_currency)
            gbp_stake = None
            native_stake = None
            stake = stake_by_key.get((quote.outcome, quote.venue))
            if stake is not None:
                gbp_stake = stake.stake
                if gbp_rate is not None:
                    native_stake = stake.stake / gbp_rate
            legs.append(
                WatchLeg(
                    outcome=quote.outcome,
                    venue=quote.venue,
                    source_market_id=quote.source_market_id,
                    source_runner_id=quote.source_runner_id,
                    currency=venue_currency,
                    native_stake=native_stake,
                    gbp_per_unit=gbp_rate,
                    gbp_stake=gbp_stake,
                    net_decimal_odds=quote.net_decimal_odds,
                    cumulative_depth_gbp=quote.cumulative_depth,
                )
            )
            if limiting_depth is None or quote.cumulative_depth < limiting_depth:
                limiting_depth = quote.cumulative_depth
                limiting_leg = quote.outcome
    elif decision.depth_scan is not None:
        solution = decision.depth_scan.solution
        implied = solution.implied_probability_sum
        solver_is_arbitrage = solution.is_arbitrage
        if implied is not None and implied > 0:
            current_edge = net_edge_from_implied_sum(implied)
        gross_edge = gross_edge_from_quotes(decision.depth_scan.selected_quotes)
        stake_by_outcome = {stake.outcome: stake for stake in solution.stakes}
        for quote in decision.depth_scan.selected_quotes:
            venue_currency = _currency_for_venue(quote.venue, history)
            if venue_currency is None:
                extra_rejections.append(f"unknown_venue_currency:{quote.venue.value}")
                continue
            gbp_rate = fx_map.get(venue_currency)
            gbp_stake = None
            native_stake = None
            stake = stake_by_outcome.get(quote.outcome)
            if stake is not None:
                gbp_stake = stake.stake
                if gbp_rate is not None:
                    native_stake = stake.stake / gbp_rate
            legs.append(
                WatchLeg(
                    outcome=quote.outcome,
                    venue=quote.venue,
                    source_market_id=quote.source_market_id,
                    source_runner_id=quote.source_runner_id,
                    currency=venue_currency,
                    native_stake=native_stake,
                    gbp_per_unit=gbp_rate,
                    gbp_stake=gbp_stake,
                    net_decimal_odds=quote.net_decimal_odds,
                    cumulative_depth_gbp=quote.cumulative_depth,
                )
            )
            if limiting_depth is None or quote.cumulative_depth < limiting_depth:
                limiting_depth = quote.cumulative_depth
                limiting_leg = quote.outcome
        if solver_is_arbitrage:
            capital = solution.total_stake
            guaranteed_profit = solution.guaranteed_profit

    venues = list(dict.fromkeys(leg.venue for leg in legs))
    if snapshot is not None:
        venues = sorted({item.venue for item in history}, key=lambda venue: venue.value) or venues

    expected_lock = None
    if snapshot is not None and snapshot.kickoff_utc is not None:
        delta_minutes = (snapshot.kickoff_utc - decision.scanned_at).total_seconds() / 60.0
        if delta_minutes > 0:
            expected_lock = Decimal(str(delta_minutes))

    resolved_quote_age = quote_age_ms if quote_age_ms is not None else decision.quote_age_ms
    resolved_basis = quote_age_basis if quote_age_basis is not None else decision.quote_age_basis
    # Fail closed: Verify uses the current decision's candidate only. MI history may
    # contain older venue snapshots and must not invent a current-looking candidate.
    mapping_candidate = safe_mapping_review_candidate(decision.mapping_review_candidate)

    return WatchObservation(
        observed_at=decision.scanned_at,
        canonical_event_id=decision.canonical_event_id,
        canonical_market_id=decision.canonical_market_id,
        settlement_key=snapshot.settlement_key if snapshot is not None else None,
        competition=snapshot.competition if snapshot is not None else None,
        home_team=snapshot.home_team if snapshot is not None else None,
        away_team=snapshot.away_team if snapshot is not None else None,
        market_family=snapshot.market_family if snapshot is not None else MarketFamily.UNKNOWN,
        period=snapshot.period if snapshot is not None else None,
        venues=venues,
        legs=legs,
        trigger_net_edge=decision.minimum_net_edge,
        current_net_edge=current_edge,
        gross_edge=gross_edge,
        implied_probability_sum=implied,
        solver_is_arbitrage=solver_is_arbitrage,
        solver_model=decision.solver_model,
        eligible_for_paper_simulation=decision.eligible_for_paper_simulation,
        rejection_reasons=_dedupe([*decision.rejection_reasons, *extra_rejections]),
        execution_risk_score=(
            decision.execution_risk.score if decision.execution_risk is not None else None
        ),
        quote_age_ms=resolved_quote_age,
        quote_age_basis=resolved_basis,
        limiting_depth_gbp=limiting_depth,
        limiting_leg_outcome=limiting_leg,
        capital_required_gbp=capital,
        guaranteed_profit_gbp=guaranteed_profit,
        expected_lock_minutes=expected_lock,
        kickoff_utc=snapshot.kickoff_utc if snapshot is not None else None,
        fixture_discovery_source=decision.fixture_discovery_source,
        fixture_status=decision.fixture_status,
        in_running=decision.in_running,
        live_score_supported=decision.live_score_supported,
        home_score=decision.home_score if decision.live_score_supported else None,
        away_score=decision.away_score if decision.live_score_supported else None,
        mapping_confidence=decision.market_match.confidence,
        mapping_matched=decision.market_match.matched,
        mapping_reasons=list(decision.market_match.reasons),
        mapping_provenance=decision.market_match.provenance,
        mapping_review_candidate=mapping_candidate,
    )


def _currency_for_venue(
    venue: VenueName,
    history: Sequence[MarketSnapshot],
) -> str | None:
    """Return native currency only from explicit observation provenance.

    Unknown currency must not become GBP, USD, or any other invented default.
    """

    for snapshot in reversed(history):
        if snapshot.venue != venue:
            continue
        native = snapshot.metadata.get("native_currency") if snapshot.metadata else None
        if isinstance(native, str) and native.strip():
            return native.upper()
    return None


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
