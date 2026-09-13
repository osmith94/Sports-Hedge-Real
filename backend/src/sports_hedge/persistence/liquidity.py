from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sports_hedge.domain.models import VenueName
from sports_hedge.paper.liquidity import (
    PaperLiquidityPool,
    PaperLiquiditySnapshot,
    apply_carrying_values,
    default_pools,
)


class SqlitePaperLiquidityRepository:
    """Persisted hypothetical paper standing-capital configuration."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        matchbook_gbp: Decimal = Decimal("5000"),
        polymarket_usd: Decimal = Decimal("5000"),
        kalshi_usd: Decimal = Decimal("5000"),
    ) -> None:
        self._matchbook_gbp = matchbook_gbp
        self._polymarket_usd = polymarket_usd
        self._kalshi_usd = kalshi_usd
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()
        self._seed_if_empty()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_liquidity_pools (
                venue TEXT PRIMARY KEY,
                native_currency TEXT NOT NULL,
                available TEXT NOT NULL,
                locked TEXT NOT NULL,
                transit TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def _seed_if_empty(self) -> None:
        count = self._connection.execute("SELECT COUNT(*) AS n FROM paper_liquidity_pools").fetchone()["n"]
        if count:
            return
        self.replace(
            default_pools(
                matchbook_gbp=self._matchbook_gbp,
                polymarket_usd=self._polymarket_usd,
                kalshi_usd=self._kalshi_usd,
            )
        )

    def get(self, *, gbp_per_unit: dict[str, Decimal] | None = None, fx_source: str | None = None) -> PaperLiquiditySnapshot:
        rows = list(self._connection.execute("SELECT * FROM paper_liquidity_pools"))
        by_venue = {VenueName(row["venue"]): _pool_from_row(row) for row in rows}
        pools = []
        for venue in (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI, VenueName.SMARKETS):
            if venue in by_venue:
                pools.append(by_venue[venue])
            else:
                seeded = default_pools(
                    matchbook_gbp=self._matchbook_gbp,
                    polymarket_usd=self._polymarket_usd,
                    kalshi_usd=self._kalshi_usd,
                )
                pools.append(next(item for item in seeded if item.venue is venue))
        valued = apply_carrying_values(pools, gbp_per_unit=gbp_per_unit, fx_source=fx_source)
        updated = max((pool.updated_at for pool in valued if pool.updated_at is not None), default=datetime.now(UTC))
        return PaperLiquiditySnapshot(pools=valued, updated_at=updated)

    def replace(self, pools: list[PaperLiquidityPool]) -> PaperLiquiditySnapshot:
        now = datetime.now(UTC)
        self._connection.execute("DELETE FROM paper_liquidity_pools")
        for pool in pools:
            self._connection.execute(
                """
                INSERT INTO paper_liquidity_pools (
                    venue, native_currency, available, locked, transit, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pool.venue.value,
                    pool.native_currency,
                    str(pool.available),
                    str(pool.locked),
                    str(pool.transit),
                    (pool.updated_at or now).isoformat(),
                ),
            )
        self._connection.commit()
        return self.get()

    def sync_from_treasury_pools(self, pools: list[object]) -> PaperLiquiditySnapshot:
        """Copy native available/locked from the authoritative treasury snapshot.

        Solver liquidity is a standing-capital projection, not a second lock book.
        """

        updates: dict[VenueName, Decimal] = {}
        locked: dict[VenueName, Decimal] = {}
        for pool in pools:
            venue = getattr(pool, "venue", None)
            if venue is None:
                continue
            resolved = venue if isinstance(venue, VenueName) else VenueName(str(venue))
            if resolved is VenueName.SMARKETS:
                continue
            updates[resolved] = Decimal(str(getattr(pool, "available_cash")))
            locked[resolved] = Decimal(str(getattr(pool, "locked_capital")))
        if not updates:
            return self.get()
        return self.update_available(updates, locked=locked)

    def update_available(
        self,
        updates: dict[VenueName, Decimal],
        *,
        locked: dict[VenueName, Decimal] | None = None,
        transit: dict[VenueName, Decimal] | None = None,
    ) -> PaperLiquiditySnapshot:
        current = {pool.venue: pool for pool in self.get().pools}
        now = datetime.now(UTC)
        merged: list[PaperLiquidityPool] = []
        for venue, pool in current.items():
            merged.append(
                pool.model_copy(
                    update={
                        "available": updates.get(venue, pool.available),
                        "locked": (locked or {}).get(venue, pool.locked),
                        "transit": (transit or {}).get(venue, pool.transit),
                        "updated_at": now,
                    }
                )
            )
        return self.replace(merged)

    def reset(self) -> PaperLiquiditySnapshot:
        return self.replace(
            default_pools(
                matchbook_gbp=self._matchbook_gbp,
                polymarket_usd=self._polymarket_usd,
                kalshi_usd=self._kalshi_usd,
            )
        )

    def close(self) -> None:
        self._connection.close()


def _pool_from_row(row: sqlite3.Row) -> PaperLiquidityPool:
    venue = VenueName(row["venue"])
    solver = venue is not VenueName.SMARKETS
    return PaperLiquidityPool(
        venue=venue,
        native_currency=row["native_currency"],
        available=Decimal(row["available"]),
        locked=Decimal(row["locked"]),
        transit=Decimal(row["transit"]),
        included_in_solver=solver,
        connection_status="connected" if solver else "not_connected",
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
