from __future__ import annotations

from fastapi import FastAPI

from sports_hedge.api.market_intelligence import router as market_intelligence_router
from sports_hedge.api.notifications import router as notifications_router
from sports_hedge.api.paper import router as paper_router
from sports_hedge.api.priority_alerts import router as priority_alerts_router
from sports_hedge.api.watchlist import router as watchlist_router
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueCapabilities, VenueName

app = FastAPI(
    title="Sports Hedge API",
    version="0.1.0",
    description="Phase 1 paper-only football arbitrage research API",
)
app.include_router(market_intelligence_router)
app.include_router(notifications_router)
app.include_router(paper_router)
app.include_router(watchlist_router)
app.include_router(priority_alerts_router)


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "mode": settings.sports_hedge_mode,
        "execution_enabled": settings.sports_hedge_execution_enabled,
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
            "venue": VenueName.SMARKETS,
            "capabilities": VenueCapabilities(
                data_enabled=False,
                paper_enabled=False,
                execution_enabled=False,
            ).model_dump(),
            "integration": "deferred",
        },
    ]
