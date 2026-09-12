"""Standing paper/demo liquidity pools in native venue currency.

These balances are hypothetical paper capital, not live venue funds.
USD and GBP are never summed as one cash figure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.paper_journal import NativeCurrencyMixError
from sports_hedge.domain.models import VenueName


PaperCapitalKind = Literal["paper_hypothetical"]
NativeCurrency = Literal["GBP", "USD"]


POOL_SPEC: dict[VenueName, tuple[NativeCurrency, bool]] = {
    VenueName.MATCHBOOK: ("GBP", True),
    VenueName.POLYMARKET: ("USD", True),
    VenueName.KALSHI: ("USD", True),
    VenueName.SMARKETS: ("GBP", False),
}


class PaperLiquidityPool(BaseModel):
    venue: VenueName
    native_currency: NativeCurrency
    available: Decimal = Field(ge=0)
    locked: Decimal = Field(default=Decimal("0"), ge=0)
    transit: Decimal = Field(default=Decimal("0"), ge=0)
    included_in_solver: bool
    connection_status: Literal["connected", "not_connected"]
    capital_kind: PaperCapitalKind = "paper_hypothetical"
    gbp_carrying_value: Decimal | None = None
    gbp_carrying_status: Literal["identity", "fx_converted", "fx_unavailable"] = "fx_unavailable"
    gbp_fx_source: str | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def enforce_venue_contract(self) -> "PaperLiquidityPool":
        currency, solver_ok = POOL_SPEC[self.venue]
        if self.native_currency != currency:
            raise ValueError(f"{self.venue.value} standing pool is {currency} native")
        if self.venue is VenueName.SMARKETS:
            self.included_in_solver = False
            self.connection_status = "not_connected"
        elif not solver_ok:
            self.included_in_solver = False
        return self


class PaperLiquiditySnapshot(BaseModel):
    capital_kind: PaperCapitalKind = "paper_hypothetical"
    data_kind: Literal["paper_config"] = "paper_config"
    pools: list[PaperLiquidityPool]
    updated_at: datetime

    @model_validator(mode="after")
    def require_all_venues(self) -> "PaperLiquiditySnapshot":
        venues = [pool.venue for pool in self.pools]
        if len(venues) != len(set(venues)):
            raise ValueError("duplicate venue liquidity pool")
        missing = [venue for venue in POOL_SPEC if venue not in venues]
        if missing:
            raise ValueError(f"missing standing pools: {[venue.value for venue in missing]}")
        return self

    def pool(self, venue: VenueName) -> PaperLiquidityPool:
        for pool in self.pools:
            if pool.venue is venue:
                return pool
        raise KeyError(venue)

    def native_totals_by_currency(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for pool in self.pools:
            totals[pool.native_currency] = totals.get(pool.native_currency, Decimal("0")) + (
                pool.available + pool.locked + pool.transit
            )
        return totals

    def combined_cash_gbp(self) -> Decimal:
        raise NativeCurrencyMixError("USD and GBP standing pools must not be summed as one cash figure")

    def solver_gbp_limits(self, gbp_per_unit: dict[str, Decimal]) -> dict[VenueName, Decimal]:
        """GBP-equivalent available capital for connected venues only."""

        limits: dict[VenueName, Decimal] = {}
        for pool in self.pools:
            if not pool.included_in_solver or pool.venue is VenueName.SMARKETS:
                continue
            rate = gbp_per_unit.get(pool.native_currency)
            if rate is None or rate <= 0:
                raise ValueError(f"missing_fx_rate:{pool.native_currency}")
            limits[pool.venue] = pool.available * rate
        return limits


def default_pools(
    *,
    matchbook_gbp: Decimal,
    polymarket_usd: Decimal,
    kalshi_usd: Decimal | None = None,
) -> list[PaperLiquidityPool]:
    now = datetime.now(UTC)
    kalshi_available = Decimal("5000") if kalshi_usd is None else kalshi_usd
    return [
        PaperLiquidityPool(
            venue=VenueName.MATCHBOOK,
            native_currency="GBP",
            available=matchbook_gbp,
            included_in_solver=True,
            connection_status="connected",
            gbp_carrying_value=matchbook_gbp,
            gbp_carrying_status="identity",
            gbp_fx_source="functional_currency",
            updated_at=now,
        ),
        PaperLiquidityPool(
            venue=VenueName.POLYMARKET,
            native_currency="USD",
            available=polymarket_usd,
            included_in_solver=True,
            connection_status="connected",
            gbp_carrying_status="fx_unavailable",
            updated_at=now,
        ),
        PaperLiquidityPool(
            venue=VenueName.KALSHI,
            native_currency="USD",
            available=kalshi_available,
            included_in_solver=True,
            connection_status="connected",
            gbp_carrying_status="fx_unavailable",
            updated_at=now,
        ),
        PaperLiquidityPool(
            venue=VenueName.SMARKETS,
            native_currency="GBP",
            available=Decimal("0"),
            included_in_solver=False,
            connection_status="not_connected",
            gbp_carrying_value=Decimal("0"),
            gbp_carrying_status="identity",
            gbp_fx_source="functional_currency",
            updated_at=now,
        ),
    ]


def apply_carrying_values(
    pools: list[PaperLiquidityPool],
    *,
    gbp_per_unit: dict[str, Decimal] | None = None,
    fx_source: str | None = None,
) -> list[PaperLiquidityPool]:
    rates = gbp_per_unit or {}
    valued: list[PaperLiquidityPool] = []
    for pool in pools:
        native_total = pool.available + pool.locked + pool.transit
        if pool.native_currency == "GBP":
            valued.append(
                pool.model_copy(
                    update={
                        "gbp_carrying_value": native_total,
                        "gbp_carrying_status": "identity",
                        "gbp_fx_source": "functional_currency",
                    }
                )
            )
            continue
        rate = rates.get(pool.native_currency)
        if rate is None or rate <= 0:
            valued.append(
                pool.model_copy(
                    update={
                        "gbp_carrying_value": None,
                        "gbp_carrying_status": "fx_unavailable",
                        "gbp_fx_source": None,
                    }
                )
            )
            continue
        valued.append(
            pool.model_copy(
                update={
                    "gbp_carrying_value": native_total * rate,
                    "gbp_carrying_status": "fx_converted",
                    "gbp_fx_source": fx_source or "backend_fx",
                }
            )
        )
    return valued
