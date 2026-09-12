"""Build unwind inputs from persisted paper trades without touching treasury."""

from __future__ import annotations

from sports_hedge.application.complete_set import SOLVER_MODEL_SIMPLE
from sports_hedge.paper.trades import PaperLegFillKind, PaperTrade
from sports_hedge.paper.unwind.models import OpenPaperLeg, OpenPaperPosition


class UnwindIdentityError(ValueError):
    """Unknown or stale identity cannot be marked-to-market."""


def position_from_trade(trade: PaperTrade) -> OpenPaperPosition:
    if not trade.canonical_event_id or not trade.canonical_market_id:
        raise UnwindIdentityError("unknown_or_stale_canonical_identity")
    if not trade.settlement_key:
        raise UnwindIdentityError("unknown_or_stale_settlement_identity")
    if trade.guaranteed_profit_gbp_at_open is None:
        raise UnwindIdentityError("unknown_hold_pnl")
    legs = [open_leg_from_trade_leg(trade, leg) for leg in trade.legs if leg.filled_stake > 0]
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
    )


def _fee_snapshot_id(trade: PaperTrade, leg) -> str | None:
    matches = [item.snapshot_id for item in trade.venue_costs if item.venue is leg.venue]
    known = [item for item in matches if item]
    if len(known) == 1:
        return known[0]
    return None
