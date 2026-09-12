from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sports_hedge.liquidity.book import BookLevel, OrderBookWalker
from sports_hedge.paper.fills import (
    PaperFillConfig,
    PaperFillRecord,
    PaperOpportunityFills,
    PaperOpportunityLeg,
    apply_odds_haircut,
    realised_slippage_bps,
)

STALE_QUOTE = "stale_quote"
INSUFFICIENT_DEPTH = "insufficient_depth"
NO_VISIBLE_DEPTH = "no_visible_depth"
PRICE_UNAVAILABLE_AFTER_SLIPPAGE = "price_unavailable_after_slippage"


class PaperFillSimulator:
    """Convert eligible paper legs into simulated fills.

    Ideal mode fills the requested stake at displayed odds and is only for checking
    the mathematical opportunity. Realistic mode walks visible depth, can skip
    top-of-book after latency, applies explicit slippage/impact, allows partial
    fills, and can reject stale quotes. The simulator never calls venue order APIs.
    """

    def __init__(self, walker: OrderBookWalker | None = None) -> None:
        self.walker = walker or OrderBookWalker()

    def simulate(
        self,
        legs: list[PaperOpportunityLeg],
        config: PaperFillConfig,
        *,
        opportunity_id: str | None = None,
        now: datetime | None = None,
    ) -> PaperOpportunityFills:
        if not legs:
            raise ValueError("at least one opportunity leg is required")
        simulated_at = _ensure_utc(now or datetime.now(UTC))
        fills = [self.simulate_leg(leg, config, now=simulated_at) for leg in legs]
        return PaperOpportunityFills(
            opportunity_id=opportunity_id or str(uuid4()),
            mode=config.mode,
            simulated_at=simulated_at,
            fills=fills,
        )

    def simulate_leg(
        self,
        leg: PaperOpportunityLeg,
        config: PaperFillConfig,
        *,
        now: datetime | None = None,
    ) -> PaperFillRecord:
        simulated_at = _ensure_utc(now or datetime.now(UTC))
        filled_at = _fill_timestamp(config, simulated_at)

        if config.is_ideal:
            return _record(
                leg,
                config,
                filled_at=filled_at,
                filled_stake=leg.requested_stake,
                remaining_stake=Decimal("0"),
                weighted_odds=leg.displayed_odds,
                worst_odds=leg.displayed_odds,
                slippage=Decimal("0"),
                levels_consumed=0,
                fully_filled=True,
                rejection_reason=None,
            )

        effective_age_ms = leg.quote_age_ms + config.assumed_latency_ms
        if config.max_quote_age_ms is not None and effective_age_ms > config.max_quote_age_ms:
            return _record(
                leg,
                config,
                filled_at=filled_at,
                filled_stake=Decimal("0"),
                remaining_stake=leg.requested_stake,
                weighted_odds=None,
                worst_odds=None,
                slippage=Decimal("0"),
                levels_consumed=0,
                fully_filled=False,
                rejection_reason=STALE_QUOTE,
            )

        visible_levels = _visible_levels(leg.levels, config)
        if not visible_levels:
            return _record(
                leg,
                config,
                filled_at=filled_at,
                filled_stake=Decimal("0"),
                remaining_stake=leg.requested_stake,
                weighted_odds=None,
                worst_odds=None,
                slippage=Decimal("0"),
                levels_consumed=0,
                fully_filled=False,
                rejection_reason=NO_VISIBLE_DEPTH,
            )

        book_fill = self.walker.fill(visible_levels, leg.requested_stake)
        if book_fill.filled_stake <= 0 or book_fill.weighted_average_odds is None:
            return _record(
                leg,
                config,
                filled_at=filled_at,
                filled_stake=Decimal("0"),
                remaining_stake=leg.requested_stake,
                weighted_odds=None,
                worst_odds=None,
                slippage=Decimal("0"),
                levels_consumed=0,
                fully_filled=False,
                rejection_reason=INSUFFICIENT_DEPTH,
            )

        visible_depth = sum((level.available_stake for level in visible_levels), Decimal("0"))
        impact_bps = Decimal("0")
        if config.price_impact_bps > 0 and visible_depth > 0:
            impact_bps = config.price_impact_bps * (book_fill.filled_stake / visible_depth)
        haircut_bps = config.slippage_bps + impact_bps
        slipped_odds = apply_odds_haircut(book_fill.weighted_average_odds, haircut_bps)
        if slipped_odds <= 1:
            return _record(
                leg,
                config,
                filled_at=filled_at,
                filled_stake=Decimal("0"),
                remaining_stake=leg.requested_stake,
                weighted_odds=None,
                worst_odds=book_fill.worst_odds,
                slippage=haircut_bps,
                levels_consumed=book_fill.levels_consumed,
                fully_filled=False,
                rejection_reason=PRICE_UNAVAILABLE_AFTER_SLIPPAGE,
            )

        remaining = book_fill.remaining_stake
        rejection = INSUFFICIENT_DEPTH if remaining > 0 else None
        return _record(
            leg,
            config,
            filled_at=filled_at,
            filled_stake=book_fill.filled_stake,
            remaining_stake=remaining,
            weighted_odds=slipped_odds,
            worst_odds=book_fill.worst_odds,
            slippage=realised_slippage_bps(leg.displayed_odds, slipped_odds),
            levels_consumed=book_fill.levels_consumed,
            fully_filled=remaining == 0,
            rejection_reason=rejection,
        )


def _visible_levels(levels: list[BookLevel], config: PaperFillConfig) -> list[BookLevel]:
    ordered = sorted(levels, key=lambda item: item.decimal_odds, reverse=True)
    skip = 0
    if config.ms_per_skipped_level > 0 and config.assumed_latency_ms > 0:
        skip = config.assumed_latency_ms // config.ms_per_skipped_level
    if skip:
        ordered = ordered[skip:]
    return ordered


def _fill_timestamp(config: PaperFillConfig, simulated_at: datetime) -> datetime:
    return _ensure_utc(simulated_at + timedelta(milliseconds=config.assumed_latency_ms))


def _record(
    leg: PaperOpportunityLeg,
    config: PaperFillConfig,
    *,
    filled_at: datetime,
    filled_stake: Decimal,
    remaining_stake: Decimal,
    weighted_odds: Decimal | None,
    worst_odds: Decimal | None,
    slippage: Decimal,
    levels_consumed: int,
    fully_filled: bool,
    rejection_reason: str | None,
) -> PaperFillRecord:
    return PaperFillRecord(
        mode=config.mode,
        filled_at=filled_at,
        venue=leg.venue,
        currency=leg.currency,
        source_market_id=leg.source_market_id,
        source_runner_id=leg.source_runner_id,
        outcome=leg.outcome,
        requested_stake=leg.requested_stake,
        filled_stake=filled_stake,
        remaining_stake=remaining_stake,
        displayed_odds=leg.displayed_odds,
        weighted_odds=weighted_odds,
        worst_odds=worst_odds,
        slippage_bps=slippage,
        levels_consumed=levels_consumed,
        fully_filled=fully_filled,
        rejection_reason=rejection_reason,
        assumed_latency_ms=config.assumed_latency_ms,
        quote_age_ms=leg.quote_age_ms,
        quote_captured_at=leg.quote_captured_at,
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "INSUFFICIENT_DEPTH",
    "NO_VISIBLE_DEPTH",
    "PRICE_UNAVAILABLE_AFTER_SLIPPAGE",
    "PaperFillSimulator",
    "STALE_QUOTE",
]
