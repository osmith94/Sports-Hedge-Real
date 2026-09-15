from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from test_read_only_collector import FakeMatchbook, FakePolymarket
from test_venue_union_discovery import NewcastleKalshi
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.lane_venues import (
    INSUFFICIENT_VENUES_WARNING,
    VENUE_HEALTH_DISABLED,
    is_operator_disabled_health,
    is_provider_health_failure,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import (
    EMPTY_LEGACY_POLYMARKET_SERIES_WARNING,
    Settings,
    legacy_extra_polymarket_series_warning,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot


class CountingMatchbook(FakeMatchbook):
    def __init__(self) -> None:
        super().__init__()
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return await super().list_events(**filters)


class CountingPolymarket(FakePolymarket):
    def __init__(self) -> None:
        super().__init__()
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        self.list_events_calls += 1
        return await super().list_events(**filters)


class FailingPolymarket(CountingPolymarket):
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        self.list_events_calls += 1
        raise RuntimeError("gamma_unreachable")


class CountingKalshi(NewcastleKalshi):
    def __init__(self) -> None:
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return await super().list_events(**filters)


def _settings(**kwargs: Any) -> Settings:
    return Settings(**kwargs)


def _collector(
    matchbook: object,
    polymarket: object,
    kalshi: object | None = None,
) -> ReadOnlyCrossVenueCollector:
    repository = SqliteMarketIntelligenceRepository()
    scan = PaperScanService(MarketIntelligenceService(repository))
    return ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=scan,
    )


def test_supported_plural_multi_series_has_no_obsolete_legacy_warning() -> None:
    settings = _settings()
    assert settings.resolved_polymarket_series_ids() == ["10188", "10355", "10193"]
    assert settings.polymarket_series_config_warnings() == []


def test_absent_legacy_variable_has_no_legacy_warning() -> None:
    settings = _settings(polymarket_gamma_series_id=None)
    assert settings.polymarket_gamma_series_id is None
    assert settings.resolved_polymarket_series_ids() == ["10188", "10355", "10193"]
    assert settings.polymarket_series_config_warnings() == []


def test_nonempty_legacy_already_in_targets_merges_without_obsolete_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="sports_hedge.config"):
        settings = _settings(polymarket_gamma_series_id="10188")
    assert settings.resolved_polymarket_series_ids() == ["10188", "10355", "10193"]
    assert settings.polymarket_series_config_warnings() == []
    assert not any("legacy single-series" in record.message for record in caplog.records)
    assert not any("POLYMARKET_GAMMA_SERIES_ID" in record.message for record in caplog.records)


def test_nonempty_legacy_extra_id_merges_with_actionable_hygiene_copy() -> None:
    settings = _settings(polymarket_gamma_series_id="99999")
    resolved = settings.resolved_polymarket_series_ids()
    assert resolved == ["10188", "10355", "10193", "99999"]
    warnings = settings.polymarket_series_config_warnings()
    assert warnings == [legacy_extra_polymarket_series_warning("99999", resolved)]
    text = warnings[0]
    assert "scanner_configuration:" in text
    assert "99999" in text
    assert "POLYMARKET_GAMMA_SERIES_IDS" in text
    assert "not a provider outage" in text
    assert "unavailable" not in text.lower()


def test_empty_legacy_disables_filtering_and_keeps_explicit_warning() -> None:
    settings = _settings(polymarket_gamma_series_id="")
    assert settings.resolved_polymarket_series_ids() == []
    warnings = settings.polymarket_series_config_warnings()
    assert warnings == [EMPTY_LEGACY_POLYMARKET_SERIES_WARNING]
    assert "filtering is disabled" in warnings[0]
    assert "not a provider outage" in warnings[0]


def test_whitespace_legacy_is_treated_as_empty_coverage_change() -> None:
    settings = _settings(polymarket_gamma_series_id="   ")
    assert settings.resolved_polymarket_series_ids() == []
    assert settings.polymarket_series_config_warnings() == [EMPTY_LEGACY_POLYMARKET_SERIES_WARNING]


@pytest.mark.asyncio
async def test_operator_disabled_venue_is_config_state_not_provider_failure() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        scan_lane=ScanLane.UNIVERSE.value,
        config_warnings=[],
    )
    assert report.venue_health["polymarket"] == VENUE_HEALTH_DISABLED
    assert is_operator_disabled_health(report.venue_health["polymarket"])
    assert not is_provider_health_failure(report.venue_health["polymarket"])
    assert report.venue_health["matchbook"] != VENUE_HEALTH_DISABLED
    assert report.venue_health["kalshi"] != "unavailable"
    assert polymarket.list_events_calls == 0
    assert all("outage" not in item.lower() or "not a provider outage" in item for item in report.config_warnings)
    assert all(not is_provider_health_failure(item) for item in report.venue_health.values() if item == VENUE_HEALTH_DISABLED)


@pytest.mark.asyncio
async def test_genuine_provider_failure_still_appears_as_provider_health() -> None:
    matchbook = CountingMatchbook()
    polymarket = FailingPolymarket()
    kalshi = CountingKalshi()
    collector = _collector(matchbook, polymarket, kalshi)
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        scan_lane=ScanLane.UNIVERSE.value,
    )
    assert report.venue_health["polymarket"] == "unavailable"
    assert is_provider_health_failure(report.venue_health["polymarket"])
    assert not is_operator_disabled_health(report.venue_health["polymarket"])
    assert any(
        issue.venue is VenueName.POLYMARKET and "gamma_unreachable" in issue.detail
        for issue in report.issues
    )
    assert INSUFFICIENT_VENUES_WARNING not in report.config_warnings


@pytest.mark.asyncio
async def test_warning_cleanup_does_not_bypass_insufficient_venue_safety_gate() -> None:
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    collector = _collector(matchbook, polymarket, CountingKalshi())
    report = await collector.collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        config_warnings=_settings().polymarket_series_config_warnings(),
    )
    assert _settings().polymarket_series_config_warnings() == []
    assert INSUFFICIENT_VENUES_WARNING in report.config_warnings
    assert "operator venue selection" in INSUFFICIENT_VENUES_WARNING
    assert "not a provider outage" in INSUFFICIENT_VENUES_WARNING
    assert report.paper_decisions == []
    assert report.matched_market_pairs == 0
    assert polymarket.list_events_calls == 0
    assert report.venue_health["polymarket"] == VENUE_HEALTH_DISABLED
    assert report.venue_health["kalshi"] == VENUE_HEALTH_DISABLED


def test_disabled_health_is_not_classified_as_provider_failure() -> None:
    assert is_operator_disabled_health(VENUE_HEALTH_DISABLED)
    assert not is_provider_health_failure(VENUE_HEALTH_DISABLED)
    assert is_provider_health_failure("unavailable")
    assert is_provider_health_failure("timeout")
    assert is_provider_health_failure("degraded")
    assert not is_operator_disabled_health("unavailable")
