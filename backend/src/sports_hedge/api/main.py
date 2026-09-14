from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from sports_hedge.api.dislocations import router as dislocations_router
from sports_hedge.api.event_intelligence import router as event_intelligence_router
from sports_hedge.api.historical import router as historical_router
from sports_hedge.api.market_intelligence import router as market_intelligence_router
from sports_hedge.api.notifications import router as notifications_router
from sports_hedge.api.operations import router as operations_router
from sports_hedge.api.paper import router as paper_router, server_owned_refresh_tick, get_accounting_schedule
from sports_hedge.api.priority_alerts import router as priority_alerts_router
from sports_hedge.api.watchlist import router as watchlist_router
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueCapabilities, VenueName
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient


@asynccontextmanager
async def lifespan(_app: FastAPI):
    coordinator = get_live_refresh_coordinator()
    coordinator.configure_from_settings()
    schedule = get_accounting_schedule()
    await coordinator.start_server_loop(server_owned_refresh_tick)
    await schedule.start()
    try:
        yield
    finally:
        await schedule.stop()
        await coordinator.stop_server_loop()


app = FastAPI(
    title="Sports Hedge API",
    version="0.1.0",
    description="Phase 1 paper-only football arbitrage research API",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.include_router(dislocations_router)
app.include_router(event_intelligence_router)
app.include_router(historical_router)
app.include_router(market_intelligence_router)
app.include_router(notifications_router)
app.include_router(operations_router)
app.include_router(paper_router)
app.include_router(watchlist_router)
app.include_router(priority_alerts_router)


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    coordinator = get_live_refresh_coordinator()
    coordinator.configure_from_settings(settings)
    return {
        "status": "ok",
        "mode": settings.sports_hedge_mode,
        "execution_enabled": settings.sports_hedge_execution_enabled,
        "paper_autofill_enabled": settings.paper_autofill_enabled,
        "live_refresh": {
            "discovery_source": "matchbook",
            "discovery_mode": "venue_union",
            "matching_venue": "polymarket",
            "matching_venues": ["polymarket", "kalshi"],
            "server_loop_enabled": coordinator.status.server_loop_enabled,
            "paper_autofill_enabled": coordinator.status.paper_autofill_enabled,
            "interval_seconds": coordinator.status.interval_seconds,
        },
    }


@app.get("/venues")
def venues() -> list[dict[str, object]]:
    paper_only = VenueCapabilities(
        data_enabled=True,
        paper_enabled=True,
        execution_enabled=False,
    )
    return [
        {
            "venue": VenueName.MATCHBOOK,
            "capabilities": paper_only.model_dump(),
            "integration": "official_api",
        },
        {
            "venue": VenueName.POLYMARKET,
            "capabilities": paper_only.model_dump(),
            "integration": "public_market_data",
        },
        {
            "venue": VenueName.KALSHI,
            "capabilities": paper_only.model_dump(),
            "integration": "official_api",
        },
        {
            "venue": VenueName.SMARKETS,
            "capabilities": VenueCapabilities(
                data_enabled=False,
                paper_enabled=False,
                execution_enabled=False,
            ).model_dump(),
            "integration": "deferred",
        },
    ]


@app.get("/venues/health")
async def venue_health() -> list[dict[str, object]]:
    """Read-only data-plane health for first-class venues. Never false-green."""

    settings = get_settings()
    timeout = settings.paper_scan_provider_timeout_seconds
    clients = (
        MatchbookClient(settings),
        PolymarketClient(settings),
        KalshiClient(settings),
    )

    async def _one(client) -> dict[str, object]:
        try:
            health = await asyncio.wait_for(client.health(), timeout=timeout)
            return health.model_dump()
        except TimeoutError:
            return {
                "venue": client.name,
                "ok": False,
                "authenticated": False,
                "checked_at": datetime.now(UTC),
                "detail": f"health_timed_out_after_{timeout:g}s",
            }
        except Exception as exc:
            return {
                "venue": client.name,
                "ok": False,
                "authenticated": False,
                "checked_at": datetime.now(UTC),
                "detail": str(exc),
            }

    try:
        return list(await asyncio.gather(*(_one(client) for client in clients)))
    finally:
        for client in clients:
            await client.aclose()
