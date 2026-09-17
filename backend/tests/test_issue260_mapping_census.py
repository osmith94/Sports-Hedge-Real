"""Issue #260 follow-up: deterministic backend mapping census.

Runs the production collector / normalizers / matcher. Not owner-live evidence.
"""

from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.mapping_census import (
    CENSUS_DATA_CLASS_FIXTURE,
    CENSUS_DATA_CLASS_OWNER_LIVE,
    OWNER_LIVE_CENSUS_ENV,
    census_from_report,
    render_census,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_cycle_audit import cycle_last_error
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_mapping_census import (
    CensusSafetyError,
    assert_paper_only_read_only,
    owner_live_census_enabled,
    run_owner_live_universe_census,
)
from sports_hedge.config import Settings
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REGULATION = (
    "Resolves based on 90 minutes of regulation time. Extra time and penalties do not count."
)
ET_AND_PENALTIES = "Resolves including penalties after extra time."

NEW_ARS_MB = "8101"
TOT_EVE_MB = "8201"
BHA_FUL_MB = "8301"
LEE_NEW_MB = "8401"
ARS_CHE_MB = "8501"
OUTRIGHT_MB = "8999"
TRUE_TOTAL_ID = "8401999"
PARTICIPANT_TOTAL_ID = "8401100"

KALSHI_GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}
KALSHI_TOTAL_SERIES = {
    "ticker": "KXEPLTOTAL",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}

# Pinned from the production collector path in this test file. Fixture/demo.
EXPECTED_FULL_CENSUS = {
    "discovered_fixtures": 5,
    "cross_venue_matched_events": 5,
    "normalized_markets_by_venue": {
        "matchbook": 7,
        "polymarket": 4,
        "kalshi": 2,
    },
    "equivalent_market_pairs": 5,
    "market_family_breakdown": {
        "match_result": 4,
        "total_goals": 1,
    },
    "evaluated_zero_equivalent_fixtures": 0,
    "unsupported_market_skips": 2,
    "qualifying_arbs": 5,
}


def _mb_event(event_id: str, name: str) -> dict[str, Any]:
    return {
        "id": int(event_id),
        "name": name,
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }


def _mb_runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "prices": [{"side": "back", "odds": odds, "available-amount": "80"}],
    }


def _match_odds(market_id: int, home: str, away: str) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Match Odds",
        "runners": [
            _mb_runner(1, home, "2.10"),
            _mb_runner(2, "Draw", "3.40"),
            _mb_runner(3, away, "3.60"),
        ],
    }


def _pm_event(event_id: str, title: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "title": title,
        "startTime": KICKOFF.isoformat(),
        "competition": "Premier League",
    }


def _pm_1x2(market_id: str, home: str, away: str, tokens: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "id": market_id,
        "question": "Match result?",
        "sportsMarketType": "moneyline",
        "outcomes": f'["{home}", "Draw", "{away}"]',
        "clobTokenIds": f'["{tokens[0]}", "{tokens[1]}", "{tokens[2]}"]',
        "description": REGULATION,
        "feesEnabled": False,
    }


def _pm_book(token: str) -> dict[str, Any]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000) - 150
    return {
        "asset_id": token,
        "timestamp": now_ms,
        "bids": [{"price": "0.30", "size": "200"}],
        "asks": [{"price": "0.32", "size": "200"}],
    }


def _kalshi_event(
    ticker: str, series: str, title: str, markets: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": title,
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "markets": markets,
    }


def _kalshi_1x2_markets(event_ticker: str, home: str, away: str) -> list[dict[str, Any]]:
    return [
        {
            "ticker": f"{event_ticker}-H",
            "event_ticker": event_ticker,
            "title": f"{home} vs {away}",
            "yes_sub_title": home,
            "rules_primary": REGULATION,
        },
        {
            "ticker": f"{event_ticker}-D",
            "event_ticker": event_ticker,
            "title": f"{home} vs {away}",
            "yes_sub_title": "Draw",
            "rules_primary": REGULATION,
        },
        {
            "ticker": f"{event_ticker}-A",
            "event_ticker": event_ticker,
            "title": f"{home} vs {away}",
            "yes_sub_title": away,
            "rules_primary": REGULATION,
        },
    ]


def _kalshi_total_market(event_ticker: str, title: str) -> dict[str, Any]:
    return {
        "ticker": f"{event_ticker}-3",
        "event_ticker": event_ticker,
        "title": f"{title} Total Goals 2.5",
        "yes_sub_title": "Over 2.5",
        "rules_primary": REGULATION,
    }


def _kalshi_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.33", "100.00"]],
            "no_dollars": [["0.64", "200.00"]],
        }
    }


class CensusMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                _mb_event(NEW_ARS_MB, "Newcastle United vs Arsenal"),
                _mb_event(TOT_EVE_MB, "Tottenham vs Everton"),
                _mb_event(BHA_FUL_MB, "Brighton vs Fulham"),
                _mb_event(LEE_NEW_MB, "Leeds United vs Newcastle United"),
                _mb_event(ARS_CHE_MB, "Arsenal vs Chelsea"),
                _mb_event(OUTRIGHT_MB, "Outright Premier League winner"),
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        key = str(event_id)
        markets: dict[str, list[dict[str, Any]]] = {
            NEW_ARS_MB: [
                _match_odds(9101, "Newcastle United", "Arsenal"),
                {
                    "id": 9102,
                    "name": "Total",
                    "runners": [{"id": 11, "name": "Over 2.5"}, {"id": 12, "name": "Under 2.5"}],
                },
                {
                    "id": 9103,
                    "name": "1st Half Total",
                    "runners": [{"id": 21, "name": "Over 1.5"}, {"id": 22, "name": "Under 1.5"}],
                },
            ],
            TOT_EVE_MB: [_match_odds(9201, "Tottenham", "Everton")],
            BHA_FUL_MB: [_match_odds(9301, "Brighton", "Fulham")],
            LEE_NEW_MB: [
                {
                    "id": int(TRUE_TOTAL_ID),
                    "name": "Over/Under 2.5 Goals",
                    "market-type": "other",
                    "runners": [
                        _mb_runner(21, "Over 2.5", "1.83"),
                        _mb_runner(22, "Under 2.5", "2.18"),
                    ],
                },
                {
                    "id": int(PARTICIPANT_TOTAL_ID),
                    "name": "Over/Under 2.5 Goals",
                    "market-type": "other",
                    "event-participant-id": 8401001,
                    "runners": [
                        {
                            "id": 11,
                            "name": "Over 2.5",
                            "event-participant-id": 8401001,
                            "prices": [
                                {"side": "back", "odds": "6.80", "available-amount": "41"}
                            ],
                        },
                        {
                            "id": 12,
                            "name": "Under 2.5",
                            "event-participant-id": 8401001,
                            "prices": [
                                {"side": "back", "odds": "1.16", "available-amount": "257"}
                            ],
                        },
                    ],
                },
            ],
            ARS_CHE_MB: [
                _match_odds(9501, "Arsenal", "Chelsea"),
                {
                    "id": 9507,
                    "name": "To Qualify",
                    "runners": [
                        _mb_runner(51, "Arsenal", "1.70"),
                        _mb_runner(52, "Chelsea", "2.20"),
                    ],
                },
            ],
        }
        return {"markets": markets.get(key, [])}


class CensusPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            _pm_event("pm-new-ars", "Newcastle United vs Arsenal"),
            _pm_event("pm-tot-eve", "Tottenham vs Everton"),
            _pm_event("pm-ars-che", "Arsenal vs Chelsea"),
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        key = str(event_id)
        if key == "pm-new-ars":
            return [
                _pm_1x2("pm-new-ars-1x2", "Newcastle United", "Arsenal", ("nh", "nd", "na"))
            ]
        if key == "pm-tot-eve":
            return [_pm_1x2("pm-tot-eve-1x2", "Tottenham", "Everton", ("th", "td", "ta"))]
        if key == "pm-ars-che":
            return [
                _pm_1x2("pm-ars-che-1x2", "Arsenal", "Chelsea", ("ah", "ad", "aa")),
                {
                    "id": "pm-ars-che-advances",
                    "question": "Who will to qualify?",
                    "sportsMarketType": "to qualify",
                    "outcomes": '["Arsenal", "Chelsea"]',
                    "clobTokenIds": '["aq", "cq"]',
                    "description": ET_AND_PENALTIES,
                    "feesEnabled": False,
                },
            ]
        return []

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        return _pm_book(str(outcome_id))


class CensusKalshi:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        brighton = "KXEPLGAME-26SEP20BHAFUL"
        leeds = "KXEPLTOTAL-26SEP20LEENEW"
        return {
            "events": [
                _kalshi_event(
                    brighton,
                    "KXEPLGAME",
                    "Brighton vs Fulham",
                    _kalshi_1x2_markets(brighton, "Brighton", "Fulham"),
                ),
                _kalshi_event(
                    leeds,
                    "KXEPLTOTAL",
                    "Leeds United vs Newcastle United",
                    [_kalshi_total_market(leeds, "Leeds United vs Newcastle United")],
                ),
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        if series_ticker == "KXEPLTOTAL":
            return KALSHI_TOTAL_SERIES
        return KALSHI_GAME_SERIES

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        return _kalshi_book()


def _census_costs() -> list:
    captured = datetime.now(UTC)
    return [
        *matchbook_polymarket_costs("0.02", "0.02", captured_at=captured),
        kalshi_cost_from_series(KALSHI_GAME_SERIES, captured_at=captured),
    ]


def _fx() -> list[FxRateSnapshot]:
    return [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")]


async def _collect(*, skip_event_ids: list[str] | None = None, generation_resume: bool = False):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=CensusMatchbook(),
        polymarket=CensusPolymarket(),
        kalshi=CensusKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=_census_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            skip_event_ids=skip_event_ids,
            generation_resume=generation_resume,
            universe_generation_id=1,
        )
        census = census_from_report(report, data_class=CENSUS_DATA_CLASS_FIXTURE)
        return report, census
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_deterministic_mapping_census_exact_counts() -> None:
    report, census = await _collect()
    rendered = render_census(census)
    assert "DETERMINISTIC FIXTURE MAPPING CENSUS" in rendered
    assert "Not owner-live evidence" in rendered
    assert census.data_class == CENSUS_DATA_CLASS_FIXTURE
    assert census.paper_mode == "paper"
    assert census.execution_enabled is False
    assert census.discovered_fixtures == EXPECTED_FULL_CENSUS["discovered_fixtures"]
    assert census.cross_venue_matched_events == EXPECTED_FULL_CENSUS["cross_venue_matched_events"]
    assert census.normalized_markets_by_venue == EXPECTED_FULL_CENSUS["normalized_markets_by_venue"]
    assert census.equivalent_market_pairs == EXPECTED_FULL_CENSUS["equivalent_market_pairs"]
    assert census.market_family_breakdown == EXPECTED_FULL_CENSUS["market_family_breakdown"]
    assert (
        census.evaluated_zero_equivalent_fixtures
        == EXPECTED_FULL_CENSUS["evaluated_zero_equivalent_fixtures"]
    )
    assert census.unsupported_market_skips == EXPECTED_FULL_CENSUS["unsupported_market_skips"]
    assert census.qualifying_arbs == EXPECTED_FULL_CENSUS["qualifying_arbs"]
    assert census.skip_failure_reasons.get("unsupported_market:matchbook", 0) >= 2
    assert cycle_last_error(report) is None
    assert any("Unsupported Matchbook market: Total" in issue.detail for issue in report.issues)
    assert any(
        "Unsupported Matchbook market: 1st Half Total" in issue.detail for issue in report.issues
    )

    rows = [row for items in report.fixture_markets.values() for row in items]
    match_result = [
        row
        for row in rows
        if row.family == "match_result"
        and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    ]
    totals = [
        row
        for row in rows
        if row.family == "total_goals"
        and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    ]
    team_totals = [row for row in rows if row.family == "team_total"]
    qualify_rows = [row for row in rows if row.family == "to_qualify"]
    assert len(match_result) == 4
    assert len(totals) == 1
    assert totals[0].matchbook is not None
    assert totals[0].kalshi is not None
    assert totals[0].matchbook.source_market_id == TRUE_TOTAL_ID
    assert team_totals
    assert all(row.kalshi is None for row in team_totals)
    assert qualify_rows
    assert all(
        row.comparison_status is not InventoryComparisonStatus.MATCHED_EQUIVALENT
        for row in qualify_rows
    )


@pytest.mark.asyncio
async def test_resumed_universe_census_still_reaches_equivalent_markets() -> None:
    baseline, _ = await _collect()
    skip = [
        item.canonical_event_id
        for item in baseline.discovered_fixtures
        if NEW_ARS_MB in item.canonical_event_id or NEW_ARS_MB in item.source_event_id
    ]
    assert skip
    resumed_report, resumed = await _collect(skip_event_ids=skip, generation_resume=True)
    assert resumed.generation_resume is True
    assert resumed.equivalent_market_pairs == 4
    assert resumed.market_family_breakdown.get("match_result") == 3
    assert resumed.market_family_breakdown.get("total_goals") == 1
    assert resumed.cross_venue_matched_events == 4
    assert cycle_last_error(resumed_report) is None
    assert resumed_report.scan_diagnostics["stale_generation_state_ignored"] is False
    remaining_sources = {item.source_event_id for item in resumed_report.discovered_fixtures}
    assert NEW_ARS_MB not in remaining_sources
    assert TOT_EVE_MB in remaining_sources
    assert LEE_NEW_MB in remaining_sources


def test_owner_live_census_is_opt_in_and_skipped_in_ci() -> None:
    assert owner_live_census_enabled({}) is False
    assert owner_live_census_enabled({OWNER_LIVE_CENSUS_ENV: "1"}) is True
    live_module = inspect.getmodule(run_owner_live_universe_census)
    assert live_module is not None
    module_src = inspect.getsource(live_module)
    runner_src = inspect.getsource(run_owner_live_universe_census)
    assert "await _one_universe_collect" in runner_src
    assert module_src.count("collect_and_scan") == 1
    for token in ("place_order", "cancel_order", "sign_order", "wallet"):
        assert token not in module_src
    assert "while True" not in module_src
    assert OWNER_LIVE_CENSUS_ENV in module_src
    assert "CENSUS_DATA_CLASS_OWNER_LIVE" in module_src
    assert "MatchbookAuthFaultError" in module_src
    assert "MatchbookRateLimitedError" in module_src
    assert "OWNER-LIVE / READ-ONLY" in module_src or "owner-live" in module_src.casefold()


def test_owner_live_census_refuses_non_paper_settings() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert_paper_only_read_only(settings)
    with pytest.raises(CensusSafetyError, match="SPORTS_HEDGE_EXECUTION_ENABLED"):
        assert_paper_only_read_only(
            SimpleNamespace(sports_hedge_mode="paper", sports_hedge_execution_enabled=True)
        )
    with pytest.raises(CensusSafetyError, match="SPORTS_HEDGE_MODE=paper"):
        assert_paper_only_read_only(
            SimpleNamespace(sports_hedge_mode="live", sports_hedge_execution_enabled=False)
        )


@pytest.mark.owner_live_census
@pytest.mark.asyncio
async def test_owner_live_universe_census_opt_in_read_only() -> None:
    if os.environ.get(OWNER_LIVE_CENSUS_ENV) != "1":
        pytest.skip(
            "Set SPORTS_HEDGE_OWNER_LIVE_CENSUS=1 to run the owner-live/read-only "
            "UNIVERSE mapping census against venue market-data."
        )
    census = await run_owner_live_universe_census()
    rendered = render_census(census)
    assert "OWNER-LIVE / READ-ONLY" in rendered
    assert census.data_class == CENSUS_DATA_CLASS_OWNER_LIVE
    assert census.execution_enabled is False
    assert "password" not in rendered.casefold()
    assert "session-token" not in rendered.casefold()
    assert census.discovered_fixtures >= 0
    assert census.equivalent_market_pairs >= 0
