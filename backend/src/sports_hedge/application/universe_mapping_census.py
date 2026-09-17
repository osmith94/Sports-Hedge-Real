"""Opt-in headless UNIVERSE mapping census. Owner-live / read-only.

Windows usage (repository root, existing `.env`):

    $env:SPORTS_HEDGE_OWNER_LIVE_CENSUS = "1"
    python -m sports_hedge.application.universe_mapping_census

Normal pytest / CI must not run this. One bounded UNIVERSE collect. No venue
writes, no credential output, no login retry loop. Matchbook 400 auth-fault
latch and 429 cooldown are left in place.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from sports_hedge.application.collector import (
    CollectionReport,
    ReadOnlyCrossVenueCollector,
    acknowledge_task_cancellation,
)
from sports_hedge.application.mapping_census import (
    CENSUS_DATA_CLASS_OWNER_LIVE,
    OWNER_LIVE_CENSUS_ENV,
    MappingCensus,
    census_from_report,
    render_census,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import (
    MatchbookAuthError,
    MatchbookAuthFaultError,
    MatchbookClient,
    MatchbookRateLimitedError,
)
from sports_hedge.venues.polymarket import PolymarketClient


class CensusSafetyError(RuntimeError):
    """Live census refused because the paper-only boundary would be crossed."""


def owner_live_census_enabled(environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return str(env.get(OWNER_LIVE_CENSUS_ENV) or "").strip() == "1"


def assert_paper_only_read_only(settings: Settings | Any) -> None:
    mode = getattr(settings, "sports_hedge_mode", None)
    execution_enabled = bool(getattr(settings, "sports_hedge_execution_enabled", False))
    if mode != "paper":
        raise CensusSafetyError("owner-live census requires SPORTS_HEDGE_MODE=paper")
    if execution_enabled:
        raise CensusSafetyError(
            "owner-live census requires SPORTS_HEDGE_EXECUTION_ENABLED=false"
        )


async def run_owner_live_universe_census(
    *,
    settings: Settings | None = None,
) -> MappingCensus:
    """One bounded UNIVERSE collect through the production collector.

    Isolated in-memory intelligence/liquidity stores so the diagnostic does not
    mutate operator paper ledgers, scan-cycle history, or UNIVERSE checkpoints.
    Matchbook login is attempted only by the existing client path; auth-fault
    and 429 cooldown fail closed without a retry loop here.
    """

    resolved = settings or get_settings()
    assert_paper_only_read_only(resolved)
    intelligence_store = SqliteMarketIntelligenceRepository()
    fx_store = SqliteFxRateRepository()
    liquidity = SqlitePaperLiquidityRepository()
    matchbook = MatchbookClient(resolved)
    polymarket = PolymarketClient(resolved)
    kalshi = KalshiClient(resolved)
    paper_scan = PaperScanService(
        MarketIntelligenceService(intelligence_store),
        fx_service=FxRateService(fx_store),
        cost_resolver=VenueCostResolver(),
        liquidity=liquidity,
    )
    timeout = float(resolved.paper_scan_universe_generation_budget_seconds)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=paper_scan,
        venue_timeout_seconds=resolved.paper_scan_venue_timeout_seconds,
        provider_call_timeout_seconds=resolved.paper_scan_provider_timeout_seconds,
        cluster_concurrency=resolved.paper_scan_cluster_concurrency,
        provider_concurrency={
            VenueName.MATCHBOOK: resolved.paper_scan_matchbook_concurrency,
            VenueName.POLYMARKET: resolved.paper_scan_polymarket_concurrency,
            VenueName.KALSHI: resolved.paper_scan_kalshi_concurrency,
        },
        cycle_timeout_seconds=timeout,
    )
    try:
        report = await _one_universe_collect(
            collector,
            settings=resolved,
            timeout=timeout,
        )
    finally:
        acknowledge_task_cancellation()
        await _aclose_quietly(polymarket, kalshi, matchbook)
        intelligence_store.close()
        fx_store.close()
        liquidity.close()
    return census_from_report(
        report,
        data_class=CENSUS_DATA_CLASS_OWNER_LIVE,
        settings=resolved,
    )


async def _one_universe_collect(
    collector: ReadOnlyCrossVenueCollector,
    *,
    settings: Settings,
    timeout: float,
) -> CollectionReport:
    try:
        return await collector.collect_and_scan(
            polymarket_queried_series_ids=settings.resolved_polymarket_series_ids(),
            config_warnings=settings.polymarket_series_config_warnings(),
            scan_lane=ScanLane.UNIVERSE.value,
            generation_resume=False,
            universe_generation_id=1,
            cycle_timeout_seconds=timeout,
            maximum_execution_risk=int(settings.max_execution_risk),
            minimum_net_edge=Decimal(str(settings.min_net_edge)),
            minimum_mapping_confidence=float(settings.min_mapping_confidence),
            assumed_latency_ms=int(settings.simulated_latency_ms),
        )
    except (MatchbookAuthFaultError, MatchbookRateLimitedError, MatchbookAuthError) as exc:
        # Latch/cooldown already recorded on the client. Do not retry.
        raise CensusSafetyError(_safe_provider_message(exc)) from exc


def _safe_provider_message(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.casefold()
    for token in ("password", "username", "token", "session", "mfa", "authorization", "secret"):
        if token in lowered:
            return f"{type(exc).__name__}: [redacted]"
    return f"{type(exc).__name__}: {text[:200]}"


async def _aclose_quietly(*clients: Any) -> None:
    for client in clients:
        closer = getattr(client, "aclose", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            continue


def main() -> None:
    import asyncio
    import json
    import sys

    if not owner_live_census_enabled():
        sys.stderr.write(
            f"Refusing owner-live census: set {OWNER_LIVE_CENSUS_ENV}=1 "
            "for this explicit read-only diagnostic.\n"
        )
        raise SystemExit(2)
    census = asyncio.run(run_owner_live_universe_census())
    sys.stdout.write(render_census(census))
    sys.stdout.write(json.dumps(census_as_public_json(census), indent=2))
    sys.stdout.write("\n")


def census_as_public_json(census: MappingCensus) -> dict[str, Any]:
    from sports_hedge.application.mapping_census import census_as_public_dict

    return census_as_public_dict(census)


if __name__ == "__main__":
    main()
