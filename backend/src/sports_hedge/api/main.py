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
from sports_hedge.api.mapping_reviews import router as mapping_reviews_router
from sports_hedge.api.capital_optimiser import router as capital_optimiser_router
from sports_hedge.api.paper import router as paper_router, server_owned_refresh_tick, get_accounting_schedule
from sports_hedge.api.priority_alerts import router as priority_alerts_router
from sports_hedge.api.watchlist import router as watchlist_router
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.serving_build import get_serving_build_info
from sports_hedge.config import emit_dotenv_operator_diagnostics, get_settings, inspect_dotenv_sources
from sports_hedge.persistence.universe_checkpoint import get_universe_checkpoint_store
from sports_hedge.domain.models import VenueCapabilities, VenueName
from sports_hedge.application.provider_runtime import (
    aclose_shared_provider_runtime,
    get_shared_provider_runtime,
)
from sports_hedge.venues.matchbook import aclose_shared_matchbook_client


@asynccontextmanager
async def lifespan(_app: FastAPI):
    emit_dotenv_operator_diagnostics(force=True)
    get_serving_build_info()
    coordinator = get_live_refresh_coordinator()
    coordinator.bind_universe_checkpoint_store(get_universe_checkpoint_store())
    coordinator.configure_from_settings()
    schedule = get_accounting_schedule()
    await coordinator.start_server_loop(server_owned_refresh_tick)
    await schedule.start()
    try:
        yield
    finally:
        await schedule.stop()
        await coordinator.stop_server_loop()
        await aclose_shared_provider_runtime()
        await aclose_shared_matchbook_client()


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
app.include_router(capital_optimiser_router)
app.include_router(mapping_reviews_router)
app.include_router(watchlist_router)
app.include_router(priority_alerts_router)


@app.get("/build-info")
async def build_info() -> dict[str, object]:
    """Cheap serving identity so soak/UI can prove which Git SHA is running."""

    return get_serving_build_info().as_public_dict()


@app.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    coordinator = get_live_refresh_coordinator()
    return {
        "status": "ok",
        "mode": settings.sports_hedge_mode,
        "execution_enabled": settings.sports_hedge_execution_enabled,
        "paper_autofill_enabled": settings.paper_autofill_enabled,
        "paper_auto_unwind_enabled": settings.paper_auto_unwind_enabled,
        "dotenv": inspect_dotenv_sources().as_public_dict(),
        "build": get_serving_build_info().as_public_dict(),
        "live_refresh": coordinator.health_live_refresh_fields(),
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
    runtime = get_shared_provider_runtime(settings)
    clients = (runtime.matchbook, runtime.polymarket, runtime.kalshi)

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

    return list(await asyncio.gather(*(_one(client) for client in clients)))
