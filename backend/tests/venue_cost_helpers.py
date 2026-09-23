from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot


def venue_cost_payload(
    venue: str,
    rate: str,
    *,
    source: str = "test",
    detail: str | None = None,
) -> dict[str, str]:
    action = "buy" if venue == "polymarket" else "back"
    currency = "USD" if venue == "polymarket" else "GBP"
    payload = {
        "venue": venue,
        "action": action,
        "fee_basis": "profit_commission",
        "known_status": "known",
        "source": source,
        "rate": rate,
        "fee_scope": "per_quote",
        "order_role": "not_applicable",
        "currency": currency,
    }
    if detail:
        payload["detail"] = detail
    return payload


def profit_commission_cost(
    venue: VenueName,
    rate: Decimal | str,
    *,
    captured_at: datetime | None = None,
    source: str = "test",
    detail: str | None = None,
    currency: str | None = None,
) -> VenueCostSnapshot:
    action = MarketAction.BUY if venue is VenueName.POLYMARKET else MarketAction.BACK
    native = currency or ("USD" if venue is VenueName.POLYMARKET else "GBP")
    return VenueCostSnapshot.per_quote_profit_commission(
        venue,
        Decimal(rate),
        action=action,
        source=source,
        captured_at=captured_at,
        currency=native,
        detail=detail,
    )


def matchbook_kalshi_costs(
    matchbook_rate: Decimal | str = "0.02",
    *,
    captured_at: datetime | None = None,
    source: str = "test",
    series: dict | None = None,
) -> list[VenueCostSnapshot]:
    from sports_hedge.fees.kalshi import kalshi_cost_from_series

    captured = captured_at or datetime.now(UTC)
    payload = series or {
        "ticker": "KXEPLGAME",
        "title": "Premier League",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    return [
        profit_commission_cost(
            VenueName.MATCHBOOK, matchbook_rate, captured_at=captured, source=source
        ),
        kalshi_cost_from_series(payload, captured_at=captured),
    ]


def matchbook_polymarket_costs(
    matchbook_rate: Decimal | str = "0.02",
    polymarket_rate: Decimal | str = "0",
    *,
    captured_at: datetime | None = None,
    source: str = "test",
) -> list[VenueCostSnapshot]:
    captured = captured_at or datetime.now(UTC)
    pm_detail = (
        "assumed_zero operator test cost; not a verified venue fee"
        if Decimal(polymarket_rate) == 0
        else None
    )
    return [
        profit_commission_cost(
            VenueName.MATCHBOOK, matchbook_rate, captured_at=captured, source=source
        ),
        profit_commission_cost(
            VenueName.POLYMARKET,
            polymarket_rate,
            captured_at=captured,
            source=source,
            detail=pm_detail,
        ),
    ]
