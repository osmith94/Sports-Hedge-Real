"""Issue #291: read-only capture + production-path replay harness.

Scenario 1 is a live capture attempt. This environment has no Matchbook
credentials, so no real same-event pair is claimed. Scenarios 2/3 are
labelled fixture/demo and must not be presented as a live backtest.
PAPER / execution off. Polymarket off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.capture_replay import (
    DATA_CLASS_FIXTURE_DEMO,
    DATA_CLASS_LIVE_CAPTURE,
    FORBIDDEN_WRITE_METHODS,
    PROVENANCE_CAPTURED_PUBLIC,
    PROVENANCE_FIXTURE,
    PROVENANCE_UNAVAILABLE,
    CaptureAttemptReport,
    ReplayBundle,
    assert_no_write_methods,
    existing_repo_same_event_capture,
    load_json,
    replay_bundle,
    sanitize_payload,
    scenario2_bayern_fair_price_bundle,
    scenario3_safe_90m_bundle,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.equivalence_diagnostics import zero_equivalent_reason_from_inventory
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.catalogue.admission import catalogue_allows_solver
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ATTEMPT_PATH = FIXTURES / "capture_replay" / "issue291_live_capture_attempt.json"


def test_phase_a_existing_fixtures_are_not_the_same_event() -> None:
    inspection = existing_repo_same_event_capture()
    assert inspection["same_event_capture_found"] is False
    assert "Bayern Munich vs Union Berlin" in inspection["kalshi_fixture"]["title"]
    assert "Chelsea" in inspection["matchbook_fixture"]["name"]
    assert "Hull" in inspection["matchbook_fixture"]["name"]
    assert inspection["kalshi_fixture"]["data_class"] == "captured_public_payload_shape"


def test_paper_and_execution_boundaries_hold() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert_no_write_methods()
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)


def test_sanitize_payload_strips_secrets_and_keeps_tickers() -> None:
    cleaned = sanitize_payload(
        {
            "event_ticker": "KXEPLGAME-SAFE",
            "username": "should-not-keep",
            "password": "should-not-keep",
            "session-token": "should-not-keep",
            "headers": {"Authorization": "Bearer x"},
            "markets": [{"ticker": "KXEPLGAME-SAFE-HOME", "token": "nope"}],
        }
    )
    dumped = json.dumps(cleaned)
    assert "should-not-keep" not in dumped
    assert "Bearer" not in dumped
    assert cleaned["event_ticker"] == "KXEPLGAME-SAFE"
    assert cleaned["markets"][0]["ticker"] == "KXEPLGAME-SAFE-HOME"
    assert "token" not in cleaned["markets"][0]


def test_live_attempt_fixture_does_not_fabricate_a_pair() -> None:
    assert ATTEMPT_PATH.is_file(), "live capture attempt report must be committed"
    report = CaptureAttemptReport.model_validate(load_json(ATTEMPT_PATH))
    assert report.data_class == DATA_CLASS_LIVE_CAPTURE
    assert report.paper_mode == "paper"
    assert report.execution_enabled is False
    assert report.polymarket_included is False
    assert report.existing_same_event_capture_found is False
    assert report.same_event_overlap_found is False
    assert report.matched_equivalent is None
    assert report.comparison_economics_computed is None
    assert report.arb is None
    assert report.matchbook.credentials_present is False
    assert report.matchbook.events_listed == 0
    assert report.matchbook.provenance == PROVENANCE_UNAVAILABLE
    assert "MATCHBOOK_USERNAME" in (report.matchbook.unavailable_reason or "")
    assert report.kalshi.reachable is True
    assert report.kalshi.events_listed > 0
    assert report.kalshi.provenance == "live_read_only_capture"
    dumped = json.dumps(report.model_dump(mode="json"))
    assert "session-token" not in dumped.casefold()
    assert "authorization" not in dumped.casefold()
    assert report.block_reason is not None


@pytest.mark.asyncio
async def test_scenario2_fair_price_stays_blocked_on_production_path() -> None:
    bundle = scenario2_bayern_fair_price_bundle()
    assert bundle.data_class == DATA_CLASS_FIXTURE_DEMO
    assert bundle.matchbook.provenance == PROVENANCE_FIXTURE
    assert bundle.kalshi.provenance == PROVENANCE_CAPTURED_PUBLIC
    assert "not live" in bundle.matchbook.identified_as.casefold()
    report, summary = await replay_bundle(bundle)
    clustered = [
        item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.matched_equivalent_count == 0
    rows = report.fixture_markets[fixture.canonical_event_id]
    assert not any(row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT for row in rows)
    assert not any(row.entered_solver for row in rows)
    reason = zero_equivalent_reason_from_inventory(rows)
    assert reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
    assert summary.matched_equivalent is False
    assert summary.entered_solver is False
    assert summary.comparison_economics_computed is False
    assert summary.arb is False
    assert summary.catalogue_admission_allowed is False
    assert summary.block_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
    assert summary.network_used is False
    left = PayloadSide(
        venue=VenueName.MATCHBOOK,
        event=bundle.matchbook.event or {},
        markets=bundle.matchbook.markets,
    )
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=bundle.kalshi.event or {},
        markets=list((bundle.kalshi.event or {}).get("markets") or []),
        series=bundle.kalshi.series,
    )
    assessment = classify_payload_pair(left, right)
    assert assessment.state is CatalogueApprovalState.REVIEW_REQUIRED
    mb = normalize_payload_side(left)
    kalshi = normalize_payload_side(right)
    assert catalogue_allows_solver(mb, kalshi) is False
    assert scan_eligible_pair(mb, kalshi, MarketMatcher().match(mb, kalshi)) is False


@pytest.mark.asyncio
async def test_scenario3_safe_90m_reaches_matched_equivalent() -> None:
    bundle = scenario3_safe_90m_bundle()
    assert bundle.data_class == DATA_CLASS_FIXTURE_DEMO
    assert "fixture/demo" in bundle.identified_as.casefold()
    report, summary = await replay_bundle(bundle)
    clustered = [
        item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.matched_equivalent_count == 1
    rows = report.fixture_markets[fixture.canonical_event_id]
    equivalent = [
        row
        for row in rows
        if row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        and row.family == "match_result"
        and row.entered_solver
    ]
    assert equivalent
    assert summary.matched_equivalent is True
    assert summary.entered_solver is True
    assert summary.comparison_economics_computed is True
    assert summary.catalogue_admission_allowed is True
    assert summary.canonical_fixture_id == fixture.canonical_event_id
    assert summary.matchbook_event_id == "28901"
    assert summary.kalshi_event_ticker == "KXSERIEAGAME-MONSAS"
    assert summary.canonical_market_key is not None
    assert "match_result" in summary.canonical_market_key
    assert summary.matchbook_prices
    assert summary.kalshi_prices
    assert summary.block_reason is None
    assert summary.network_used is False
    assert summary.arb is False
    assert summary.net_edge is not None and summary.net_edge <= 0
    coverage = report.scan_diagnostics["matching_coverage"]
    assert coverage["equivalent_markets"] == 1
    left = PayloadSide(
        venue=VenueName.MATCHBOOK,
        event=bundle.matchbook.event or {},
        markets=bundle.matchbook.markets,
    )
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=bundle.kalshi.event or {},
        markets=list((bundle.kalshi.event or {}).get("markets") or []),
        series=bundle.kalshi.series,
    )
    assert classify_payload_pair(left, right).state is CatalogueApprovalState.APPROVED_EQUIVALENT
    mb = normalize_payload_side(left)
    kalshi = normalize_payload_side(right)
    assert catalogue_allows_solver(mb, kalshi) is True
    assert scan_eligible_pair(mb, kalshi, MarketMatcher().match(mb, kalshi)) is True


@pytest.mark.asyncio
async def test_replay_does_not_use_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("replay must not use the network")

    monkeypatch.setattr("httpx.AsyncClient.request", _boom)
    monkeypatch.setattr("httpx.AsyncClient.send", _boom)
    bundle = scenario3_safe_90m_bundle()
    _report, summary = await replay_bundle(bundle)
    assert summary.matched_equivalent is True
    assert summary.network_used is False


def test_replay_bundle_round_trip_json() -> None:
    bundle = scenario2_bayern_fair_price_bundle()
    restored = ReplayBundle.model_validate(json.loads(bundle.model_dump_json()))
    assert restored.bundle_id == bundle.bundle_id
    assert restored.kalshi.event is not None
    assert "fair price" in restored.kalshi.event["markets"][0]["rules_secondary"].casefold()


def test_cli_module_is_importable() -> None:
    from sports_hedge.application import capture_replay

    assert callable(capture_replay.main)
    assert capture_replay.DATA_CLASS_LIVE_CAPTURE == DATA_CLASS_LIVE_CAPTURE
