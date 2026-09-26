"""UI step 2: lightweight operations heartbeat and UNIVERSE catalogue.

Synthetic fixture/demo state. Not live venue quotes. PAPER / read-only.
Scanner pairing, economics, and provider calls are not changed here.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api import operations as operations_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.live_refresh import LiveRefreshCoordinator, get_live_refresh_coordinator
from sports_hedge.application.operations_read_model import (
    CATALOGUE_FORBIDDEN_FIELDS,
    HEARTBEAT_OMITTED_FIELDS,
)
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _fixture(
    canonical_id: str,
    *,
    kickoff: datetime | None = None,
    evaluation: str = "evaluated",
    home: str = "Home",
    matchbook: bool = True,
    polymarket: bool = False,
    kalshi: bool = False,
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=f"src-{canonical_id}",
        canonical_event_id=canonical_id,
        home_team=home,
        away_team="Away",
        competition="Premier League",
        sport="football",
        target_competition_code="EPL",
        kickoff_utc=kickoff or (NOW + timedelta(days=2)),
        last_seen_at=NOW,
        matchbook_matched=matchbook,
        polymarket_matched=polymarket,
        kalshi_matched=kalshi,
        market_evaluation_state=evaluation,
        current_net_edge=None,
        best_matchbook_price=None,
        solver_is_arbitrage=False,
    )


def _report(fixtures: list[DiscoveredFixture], *, lane: str = "universe") -> CollectionReport:
    return CollectionReport(
        started_at=NOW,
        completed_at=NOW,
        discovered_fixtures=fixtures,
        scan_lane=lane,
        operator_summary="ui-step2",
    )


def test_heartbeat_skips_full_operator_board_and_omits_heavy_fields() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.record_report(
        _report(
            [
                _fixture("evt-hot", kickoff=NOW + timedelta(minutes=20), polymarket=True),
                _fixture(
                    "evt-deferred",
                    evaluation="single_venue_no_cross_venue_candidate",
                ),
            ]
        ),
        scan_lane=ScanLane.UNIVERSE,
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("heartbeat must not project the full operator board")

    coordinator._fixture_state.operator_board = boom  # type: ignore[method-assign]
    heartbeat = coordinator.public_heartbeat()
    payload = heartbeat.model_dump(mode="json")
    assert HEARTBEAT_OMITTED_FIELDS.isdisjoint(payload)
    assert payload["deferred_awaiting_count"] == 1
    assert payload["universe_fixture_count"] == 2
    assert payload["universe_catalogue_version"]
    assert {row["canonical_event_id"] for row in payload["hot_roster"]}.isdisjoint({"evt-deferred"})
    assert payload["startup_phase"]
    source = inspect.getsource(LiveRefreshCoordinator.public_heartbeat)
    assert "operator_board" not in source
    assert "console_projection" not in source


def test_catalogue_changes_only_on_universe_publication() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.record_report(
        _report([_fixture("evt-1", home="Arsenal", polymarket=True)]),
        scan_lane=ScanLane.UNIVERSE,
    )
    first = coordinator.universe_fixture_catalogue()
    assert first.fixtures[0].home_team == "Arsenal"
    version = first.universe_catalogue_version
    for field in CATALOGUE_FORBIDDEN_FIELDS:
        assert field not in first.fixtures[0].model_dump()

    priced = _fixture("evt-1", home="Renamed By Hot", polymarket=True)
    priced.current_net_edge = priced.current_net_edge
    priced.best_matchbook_price = None
    coordinator.record_report(_report([priced], lane="hot"), scan_lane=ScanLane.HOT)
    after_hot = coordinator.universe_fixture_catalogue()
    assert after_hot.universe_catalogue_version == version
    assert after_hot.fixtures[0].home_team == "Arsenal"
    assert after_hot.universe_catalogue_as_of == first.universe_catalogue_as_of

    background = _fixture("evt-1", home="Renamed By Background", polymarket=True)
    coordinator._fixture_state.upsert_from_report(
        _report([background]),
        scan_lane=ScanLane.UNIVERSE,
        publish_universe_catalogue=False,
    )
    after_background = coordinator.universe_fixture_catalogue()
    assert after_background.universe_catalogue_version == version
    assert after_background.fixtures[0].home_team == "Arsenal"

    again = coordinator.public_heartbeat()
    assert again.universe_catalogue_version == version

    coordinator.record_report(
        _report([_fixture("evt-1", home="Arsenal", polymarket=True, kalshi=True)]),
        scan_lane=ScanLane.UNIVERSE,
    )
    changed = coordinator.universe_fixture_catalogue()
    assert changed.universe_catalogue_version != version
    assert changed.fixtures[0].kalshi_matched is True

    coordinator.clear_universe_working_set()
    cleared = coordinator.universe_fixture_catalogue()
    assert cleared.universe_fixture_count == 0
    assert cleared.universe_catalogue_version != changed.universe_catalogue_version


def test_price_engine_does_not_opt_into_catalogue_publication() -> None:
    source = inspect.getsource(CataloguePriceEngine)
    assert "publish_universe_catalogue=True" not in source
    on_fixture = inspect.getsource(LiveRefreshCoordinator.record_universe_fixture_progress)
    assert "publish_universe_catalogue=True" in on_fixture


def test_live_refresh_route_is_heartbeat_and_diagnostics_are_separate() -> None:
    live_src = inspect.getsource(paper_api.live_refresh_status)
    assert "public_heartbeat" not in live_src
    assert "_operations_heartbeat" in live_src
    assert "operator_board" not in live_src
    catalogue_src = inspect.getsource(operations_api.universe_fixtures)
    deferred_src = inspect.getsource(operations_api.deferred_fixtures)
    matching_src = inspect.getsource(operations_api.universe_matching_report)
    assert "collect_and_scan" not in catalogue_src
    assert "collect_and_scan" not in deferred_src
    assert "collect_and_scan" not in matching_src
    scans_src = inspect.getsource(paper_api.recent_scans)
    assert "limit" in scans_src

    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.record_report(
        _report(
            [
                _fixture("evt-a", evaluation="single_venue_no_cross_venue_candidate"),
                _fixture("evt-b", evaluation="cross_venue_unavailable", polymarket=True),
                _fixture("evt-c", kickoff=NOW + timedelta(minutes=15), polymarket=True, kalshi=True),
            ]
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    client = TestClient(app)
    try:
        heartbeat = client.get("/paper/live-refresh")
        assert heartbeat.status_code == 200
        body = heartbeat.json()
        raw = heartbeat.content
        assert len(raw) < 80_000
        assert "discovered_fixtures" not in body
        assert "recent_scan_cycles" not in body
        assert body["deferred_awaiting_count"] == 2
        assert "evt-a" not in raw.decode()
        catalogue = client.get("/operations/universe-fixtures")
        assert catalogue.status_code == 200
        catalogue_body = catalogue.json()
        assert catalogue_body["universe_fixture_count"] == 3
        assert catalogue_body["universe_catalogue_version"] == body["universe_catalogue_version"]
        assert "best_matchbook_price" not in catalogue.content.decode()
        assert "current_net_edge" not in catalogue.content.decode()
        deferred = client.get("/operations/deferred-fixtures")
        assert deferred.status_code == 200
        report = deferred.json()
        assert report["count"] == 2
        assert report["provider_calls"] == 0
        assert report["scan_started"] is False
        assert report["places_orders"] is False
        assert {row["canonical_event_id"] for row in report["rows"]} == {"evt-a", "evt-b"}
        cycles = client.get("/paper/scan-cycles", params={"limit": 50})
        assert cycles.status_code == 200
        assert isinstance(cycles.json(), list)
        scans = client.get("/paper/scans", params={"limit": 1})
        assert scans.status_code == 200
    finally:
        coordinator.reset()


def test_representative_heartbeat_is_much_smaller_than_the_old_board() -> None:
    coordinator = LiveRefreshCoordinator()
    fixtures = []
    for index in range(165):
        deferred = index < 116
        fixtures.append(
            _fixture(
                f"evt-{index:04d}",
                home=f"Home {index}",
                evaluation="single_venue_no_cross_venue_candidate" if deferred else "evaluated",
                polymarket=not deferred,
                kalshi=not deferred,
                kickoff=NOW + timedelta(minutes=index if index % 11 == 0 else 5000),
            )
        )
    coordinator.record_report(_report(fixtures), scan_lane=ScanLane.UNIVERSE)
    heartbeat = json.dumps(coordinator.public_heartbeat().model_dump(mode="json")).encode()
    board = json.dumps(coordinator.public_status().model_dump(mode="json")).encode()
    assert len(board) > 100_000
    assert len(heartbeat) < 80_000
    assert len(heartbeat) * 4 < len(board)
