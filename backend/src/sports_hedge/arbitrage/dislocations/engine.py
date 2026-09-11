from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sports_hedge.arbitrage.dislocations.models import (
    BurstPriorityDecision,
    EVENT_MATERIALITY,
    MATERIAL_EVENT_CATEGORIES,
    EventAnnotationInput,
    NearArbSignal,
    PriorityFactors,
    QuoteEligibility,
    RateBudget,
    ScanCandidate,
    ScanPriority,
    VenueQuoteSnapshot,
)

DEFAULT_MAX_QUOTE_AGE_MS = 3000
MATERIAL_DISPERSION = Decimal("0.03")
DISLOCATION_DISPERSION = Decimal("0.02")
NEAR_ARB_CLOSE = Decimal("0.005")
HIGH_LIQUIDITY = Decimal("5000")
QUANT = Decimal("0.0001")

_WEIGHTS: dict[str, Decimal] = {
    "event_materiality": Decimal("18"),
    "cross_venue_dispersion": Decimal("20"),
    "market_liquidity": Decimal("14"),
    "quote_freshness": Decimal("10"),
    "equivalent_venues": Decimal("8"),
    "near_arb_proximity": Decimal("16"),
    "executable_depth": Decimal("10"),
    "execution_quality": Decimal("8"),
    "rate_budget_headroom": Decimal("4"),
}


def evaluate_candidate(
    candidate: ScanCandidate,
    *,
    budget: RateBudget | None = None,
    max_quote_age_ms: int = DEFAULT_MAX_QUOTE_AGE_MS,
) -> BurstPriorityDecision:
    """Decide scan priority for one canonical event/market. Paper-only."""

    if not candidate.paper_mode:
        raise ValueError("Event-driven dislocation scanning is paper-only")

    eligibility = [
        assess_quote_eligibility(quote, candidate.annotation, max_quote_age_ms=max_quote_age_ms)
        for quote in candidate.quotes
    ]
    executable_quotes = [
        quote
        for quote, item in zip(candidate.quotes, eligibility, strict=True)
        if item.executable
    ]
    fresh_venues = {quote.venue for quote in executable_quotes}
    dispersion = _cross_venue_dispersion(executable_quotes)
    min_depth = _min_executable_depth(executable_quotes)
    liquidity = candidate.market_liquidity
    if liquidity == 0 and candidate.quotes:
        liquidity = sum((quote.liquidity for quote in candidate.quotes), Decimal("0"))

    materiality = _materiality(candidate.annotation)
    freshness = _freshness_factor(eligibility)
    equivalent = _equivalent_venues_factor(
        candidate.event.economically_equivalent_venues or len(fresh_venues)
    )
    near_arb = _near_arb_proximity(candidate.near_arb)
    depth_factor = min(min_depth / Decimal("1000"), Decimal("1"))
    execution_quality = (Decimal("100") - Decimal(candidate.execution_risk_score)) / Decimal(
        "100"
    )
    budget_headroom = _budget_headroom(budget)
    liquidity_factor = min(liquidity / HIGH_LIQUIDITY, Decimal("1"))
    headline = min(candidate.headline_move / Decimal("0.10"), Decimal("1"))

    dispersion_factor = min(dispersion / Decimal("0.08"), Decimal("1"))
    factors = PriorityFactors(
        event_materiality=materiality,
        cross_venue_dispersion=dispersion_factor,
        market_liquidity=liquidity_factor,
        quote_freshness=freshness,
        equivalent_venues=equivalent,
        near_arb_proximity=near_arb,
        executable_depth=depth_factor,
        execution_quality=execution_quality,
        rate_budget_headroom=budget_headroom,
        headline_move=headline,
        headline_move_weight=Decimal("0"),
    )
    score = _composite_score(factors)
    executable_dislocation = (
        len(fresh_venues) >= 2
        and dispersion >= DISLOCATION_DISPERSION
        and min_depth > 0
        and all(quote.settlement_semantics_complete for quote in executable_quotes)
        and all(quote.costs_and_fx_complete for quote in executable_quotes)
    )
    reasons = _reasons(
        candidate=candidate,
        eligibility=eligibility,
        fresh_venues=len(fresh_venues),
        dispersion=dispersion,
        executable_dislocation=executable_dislocation,
        materiality=materiality,
    )
    priority = _priority_band(
        score=score,
        candidate=candidate,
        fresh_venues=len(fresh_venues),
        dispersion=dispersion,
        executable_dislocation=executable_dislocation,
        liquidity=liquidity,
        near_arb=candidate.near_arb,
    )
    return BurstPriorityDecision(
        canonical_event_id=candidate.event.canonical_event_id,
        canonical_market_id=candidate.event.canonical_market_id,
        priority=priority,
        composite_score=score,
        factors=factors,
        reasons=reasons,
        executable_dislocation=executable_dislocation,
        cross_venue_dispersion=dispersion,
        fresh_executable_venues=len(fresh_venues),
        quote_eligibility=eligibility,
        event_occurred_at=(
            candidate.annotation.occurred_at if candidate.annotation is not None else None
        ),
        evaluated_at=candidate.event.as_of,
    )


def assess_quote_eligibility(
    quote: VenueQuoteSnapshot,
    annotation: EventAnnotationInput | None,
    *,
    max_quote_age_ms: int = DEFAULT_MAX_QUOTE_AGE_MS,
) -> QuoteEligibility:
    reasons: list[str] = []
    if quote.suspended:
        reasons.append("venue_suspended")
    age_ms = quote.quote_age_ms if quote.quote_age_ms is not None else 0
    if age_ms > max_quote_age_ms:
        reasons.append("stale_quote_age")
    if annotation is not None and quote.quote_timestamp < annotation.occurred_at:
        reasons.append("stale_pre_event_quote")
    if not quote.settlement_semantics_complete:
        reasons.append("incomplete_settlement_semantics")
    if not quote.costs_and_fx_complete:
        reasons.append("missing_costs_or_fx")
    if quote.executable_depth <= 0:
        reasons.append("no_executable_depth")
    return QuoteEligibility(
        venue=quote.venue,
        canonical_outcome=quote.canonical_outcome,
        executable=not reasons,
        reasons=reasons,
        quote_timestamp=quote.quote_timestamp,
        retrieved_at=quote.retrieved_at,
    )


def _materiality(annotation: EventAnnotationInput | None) -> Decimal:
    if annotation is None:
        return Decimal("0")
    return EVENT_MATERIALITY.get(annotation.category, Decimal("0"))


def _cross_venue_dispersion(quotes: list[VenueQuoteSnapshot]) -> Decimal:
    by_outcome: dict[str, list[Decimal]] = {}
    for quote in quotes:
        if quote.implied_probability is None:
            continue
        by_outcome.setdefault(quote.canonical_outcome, []).append(quote.implied_probability)
    peak = Decimal("0")
    for probabilities in by_outcome.values():
        if len(probabilities) < 2:
            continue
        spread = max(probabilities) - min(probabilities)
        if spread > peak:
            peak = spread
    return peak


def _min_executable_depth(quotes: list[VenueQuoteSnapshot]) -> Decimal:
    if not quotes:
        return Decimal("0")
    return min(quote.executable_depth for quote in quotes)


def _freshness_factor(eligibility: list[QuoteEligibility]) -> Decimal:
    if not eligibility:
        return Decimal("0")
    executable = sum(1 for item in eligibility if item.executable)
    return Decimal(executable) / Decimal(len(eligibility))


def _equivalent_venues_factor(count: int) -> Decimal:
    if count <= 1:
        return Decimal("0")
    return min(Decimal(count - 1) / Decimal("3"), Decimal("1"))


def _near_arb_proximity(signal: NearArbSignal | None) -> Decimal:
    if signal is None:
        return Decimal("0")
    return max(Decimal("0"), Decimal("1") - (signal.distance_to_trigger / Decimal("0.02")))


def _budget_headroom(budget: RateBudget | None) -> Decimal:
    if budget is None or budget.max_scans == 0:
        return Decimal("1")
    return Decimal(budget.available_scans) / Decimal(max(budget.max_scans, 1))


def _composite_score(factors: PriorityFactors) -> Decimal:
    total = Decimal("0")
    for name, weight in _WEIGHTS.items():
        total += getattr(factors, name) * weight
    return total.quantize(QUANT, rounding=ROUND_HALF_UP)


def _priority_band(
    *,
    score: Decimal,
    candidate: ScanCandidate,
    fresh_venues: int,
    dispersion: Decimal,
    executable_dislocation: bool,
    liquidity: Decimal,
    near_arb: NearArbSignal | None,
) -> ScanPriority:
    material_event = (
        candidate.annotation is not None
        and candidate.annotation.category in MATERIAL_EVENT_CATEGORIES
    )
    two_fresh_material_dispersion = fresh_venues >= 2 and dispersion >= MATERIAL_DISPERSION
    close_to_arb = near_arb is not None and near_arb.distance_to_trigger <= NEAR_ARB_CLOSE
    if (
        executable_dislocation
        and two_fresh_material_dispersion
        and close_to_arb
        and liquidity >= HIGH_LIQUIDITY
        and candidate.execution_risk_score <= 40
    ):
        return ScanPriority.CRITICAL
    if two_fresh_material_dispersion or score >= Decimal("55"):
        return ScanPriority.BURST
    if material_event or score >= Decimal("28") or close_to_arb:
        return ScanPriority.ELEVATED
    if score >= Decimal("12"):
        return ScanPriority.ELEVATED
    return ScanPriority.NORMAL


def _reasons(
    *,
    candidate: ScanCandidate,
    eligibility: list[QuoteEligibility],
    fresh_venues: int,
    dispersion: Decimal,
    executable_dislocation: bool,
    materiality: Decimal,
) -> list[str]:
    reasons: list[str] = []
    if candidate.annotation is not None:
        reasons.append(f"event:{candidate.annotation.category.value}")
        reasons.append("event_timestamp_separated_from_quote_timestamp")
    if materiality > 0:
        reasons.append("event_materiality_applied")
    if fresh_venues >= 2:
        reasons.append(f"fresh_executable_venues:{fresh_venues}")
    if dispersion >= MATERIAL_DISPERSION:
        reasons.append("material_cross_venue_dispersion")
    if executable_dislocation:
        reasons.append("executable_dislocation_after_gates")
    else:
        reasons.append("no_executable_dislocation")
    blocked = sorted(
        {reason for item in eligibility for reason in item.reasons if not item.executable}
    )
    reasons.extend(blocked)
    if candidate.headline_move > 0:
        reasons.append("headline_move_recorded_not_ranked")
    reasons.append("paper_mode_only")
    reasons.append("authorised_public_feeds_only")
    return reasons
