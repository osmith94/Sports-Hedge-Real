"""Build unwind inputs from persisted paper trades without touching treasury."""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.application.complete_set import SOLVER_MODEL_SIMPLE
from sports_hedge.paper.trades import (
    PaperCloseFill,
    PaperLegFillKind,
    PaperTrade,
    paper_close_fill_id,
)
from sports_hedge.paper.unwind.models import (
    CloseLegPlan,
    OpenPaperFillShare,
    OpenPaperLeg,
    OpenPaperPosition,
    RemainingLockSource,
    UnwindDecision,
)


class UnwindIdentityError(ValueError):
    """Unknown or stale identity cannot be marked-to-market."""


def position_from_trade(trade: PaperTrade) -> OpenPaperPosition:
    if not trade.canonical_event_id or not trade.canonical_market_id:
        raise UnwindIdentityError("unknown_or_stale_canonical_identity")
    if not trade.settlement_key:
        raise UnwindIdentityError("unknown_or_stale_settlement_identity")
    if trade.guaranteed_profit_gbp_at_open is None:
        raise UnwindIdentityError("unknown_hold_pnl")
    legs = _aggregate_same_market_close_legs(
        [open_leg_from_trade_leg(trade, leg) for leg in trade.legs if leg.filled_stake > 0]
    )
    if not legs:
        raise UnwindIdentityError("no_filled_legs")
    return OpenPaperPosition(
        trade_id=trade.trade_id,
        opportunity_id=trade.opportunity_id,
        canonical_event_id=trade.canonical_event_id,
        canonical_market_id=trade.canonical_market_id,
        settlement_fingerprint_key=trade.settlement_key,
        solver_model=trade.solver_model or SOLVER_MODEL_SIMPLE,
        hold_pnl_gbp=trade.guaranteed_profit_gbp_at_open,
        capital_locked_native=dict(trade.capital_locked_native),
        # PaperTrade has no authoritative remaining-lock / settlement timer.
        # Leave both unknown rather than inventing a match-finish clock.
        expected_settlement_at=None,
        remaining_lock_minutes=None,
        remaining_lock_basis=RemainingLockSource.UNKNOWN,
        legs=legs,
        paper_only=trade.paper_only,
        places_orders=trade.places_orders,
    )


def open_leg_from_trade_leg(trade: PaperTrade, leg) -> OpenPaperLeg:
    if not leg.source_event_id or not leg.source_runner_id or not leg.source_market_id:
        raise UnwindIdentityError("unknown_or_stale_leg_identity")
    if not leg.opening_action or not leg.settlement_fingerprint_key:
        raise UnwindIdentityError("unknown_or_stale_leg_identity")
    price = leg.filled_odds or leg.displayed_odds
    if price is None:
        raise UnwindIdentityError("missing_filled_price")
    fingerprint = leg.settlement_fingerprint_key or trade.settlement_key
    if not fingerprint:
        raise UnwindIdentityError("unknown_or_stale_settlement_identity")
    if leg.fill_kind is PaperLegFillKind.UNFILLED:
        raise UnwindIdentityError("unfilled_leg")
    return OpenPaperLeg(
        venue=leg.venue,
        source_event_id=leg.source_event_id,
        source_market_id=leg.source_market_id,
        source_runner_id=leg.source_runner_id,
        source_contract_id=leg.source_contract_id,
        canonical_market_id=trade.canonical_market_id or "",
        canonical_outcome=leg.outcome,
        canonical_state=leg.canonical_state or leg.outcome,
        opening_action=leg.opening_action,
        filled_price=price,
        filled_size=leg.filled_stake,
        native_currency=leg.currency,
        fee_snapshot_id=_fee_snapshot_id(trade, leg),
        settlement_fingerprint_key=fingerprint,
        fill_kind=leg.fill_kind,
        fill_id=leg.fill_id,
        opening_fills=(
            [
                OpenPaperFillShare(
                    fill_id=leg.fill_id,
                    filled_size=leg.filled_stake,
                    filled_price=price,
                )
            ]
            if leg.fill_id
            else []
        ),
    )


def _aggregate_same_market_close_legs(legs: list[OpenPaperLeg]) -> list[OpenPaperLeg]:
    """One shared reverse book per native market / runner / close action.

    Multiple top-up tranches on the same Matchbook/Kalshi identity must consume
    executable close depth once, not independently reuse the full book.
    The aggregated opening price preserves Σ(size_i * price_i).
    """

    grouped: dict[tuple, OpenPaperLeg] = {}
    order: list[tuple] = []
    for leg in legs:
        key = (
            leg.venue,
            leg.source_event_id,
            leg.source_market_id,
            leg.source_runner_id,
            leg.opening_action,
            leg.canonical_outcome,
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = leg
            order.append(key)
            continue
        total_size = existing.filled_size + leg.filled_size
        total_payout = (existing.filled_size * existing.filled_price) + (
            leg.filled_size * leg.filled_price
        )
        weighted_price = total_payout / total_size
        grouped[key] = existing.model_copy(
            update={
                "filled_size": total_size,
                "filled_price": weighted_price,
                "opening_fills": _opening_shares(existing) + _opening_shares(leg),
                "fill_id": existing.fill_id or leg.fill_id,
            }
        )
    return [grouped[key] for key in order]


def _opening_shares(leg: OpenPaperLeg) -> list[OpenPaperFillShare]:
    if leg.opening_fills:
        return list(leg.opening_fills)
    if leg.fill_id:
        return [
            OpenPaperFillShare(
                fill_id=leg.fill_id,
                filled_size=leg.filled_size,
                filled_price=leg.filled_price,
            )
        ]
    return []


def _split_total(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    if not weights:
        return []
    whole = sum(weights, Decimal("0"))
    if whole <= 0:
        raise UnwindIdentityError("aggregate_open_basis_zero")
    parts: list[Decimal] = []
    running = Decimal("0")
    last = len(weights) - 1
    for index, weight in enumerate(weights):
        if index == last:
            parts.append(total - running)
            break
        part = total * weight / whole
        parts.append(part)
        running += part
    return parts


def allocated_close_shares(
    open_leg: OpenPaperLeg,
    close_leg: CloseLegPlan,
) -> list[tuple[OpenPaperFillShare, Decimal, Decimal, Decimal, Decimal, Decimal]]:
    """Allocate one aggregate close across every underlying opening fill/lock."""

    shares = _opening_shares(open_leg)
    if not shares:
        if not open_leg.fill_id:
            raise UnwindIdentityError("missing_lock_identity")
        shares = [
            OpenPaperFillShare(
                fill_id=open_leg.fill_id,
                filled_size=open_leg.filled_size,
                filled_price=open_leg.filled_price,
            )
        ]
    weights = [share.filled_size * share.filled_price for share in shares]
    proceeds = close_leg.proceeds if close_leg.proceeds else close_leg.matched_stake
    quantities = _split_total(close_leg.filled_close_quantity, weights)
    proceeds_parts = _split_total(proceeds, weights)
    fee_parts = _split_total(close_leg.closing_fee, weights)
    native_parts = _split_total(close_leg.native_close_pnl, weights)
    gbp_parts = _split_total(close_leg.gbp_close_pnl, weights)
    allocated = []
    for share, quantity, proceeds_part, fee, native_pnl, gbp_pnl in zip(
        shares, quantities, proceeds_parts, fee_parts, native_parts, gbp_parts, strict=True
    ):
        allocated.append((share, quantity, proceeds_part, fee, native_pnl, gbp_pnl))
    return allocated


def close_fills_from_decision(
    position: OpenPaperPosition,
    decision: UnwindDecision,
    *,
    fx_rates: dict[tuple, Decimal],
) -> list[PaperCloseFill]:
    """Persist reverse-side close fills separately from opening legs."""

    if len(decision.close_plan.legs) != len(position.legs):
        raise UnwindIdentityError("unwind_leg_mismatch")
    fills: list[PaperCloseFill] = []
    for close_leg, open_leg in zip(decision.close_plan.legs, position.legs, strict=True):
        if not close_leg.executable:
            raise UnwindIdentityError("close_not_fully_executable")
        rate = fx_rates.get((close_leg.venue, close_leg.native_currency.upper()))
        if rate is None:
            raise UnwindIdentityError(f"missing_fx_rate:{close_leg.native_currency}")
        for share, quantity, proceeds, fee, native_pnl, gbp_pnl in allocated_close_shares(
            open_leg, close_leg
        ):
            fills.append(
                PaperCloseFill(
                    fill_id=paper_close_fill_id(share.fill_id),
                    opening_fill_id=share.fill_id,
                    venue=close_leg.venue,
                    outcome=close_leg.canonical_outcome,
                    native_currency=close_leg.native_currency,
                    close_action=close_leg.close_action,
                    filled_close_quantity=quantity,
                    weighted_closing_price=close_leg.weighted_closing_price,
                    proceeds_native=proceeds,
                    closing_fee_native=fee,
                    native_close_pnl=native_pnl,
                    gbp_close_pnl=gbp_pnl,
                    fx_rate_gbp_per_unit=rate,
                    lock_id=share.fill_id,
                    fee_snapshot_id=close_leg.fee_snapshot_id,
                    quote_age_ms=close_leg.quote_age_ms,
                )
            )
    return fills


def _fee_snapshot_id(trade: PaperTrade, leg) -> str | None:
    matches = [item.snapshot_id for item in trade.venue_costs if item.venue is leg.venue]
    known = [item for item in matches if item]
    if len(known) == 1:
        return known[0]
    return None
