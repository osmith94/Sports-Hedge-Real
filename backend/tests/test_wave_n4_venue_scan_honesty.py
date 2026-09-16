from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.lane_venues import last_scan_venue_clause
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    _hot_operator_summary,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _fixture(canonical_id: str) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.POLYMARKET,
        source_event_id=canonical_id,
        canonical_event_id=canonical_id,
        home_team="Home",
        away_team="Away",
        competition="Premier League",
        kickoff_utc=NOW + timedelta(days=1),
        last_seen_at=NOW,
        market_evaluation_state="evaluated",
        opportunity_state="matched",
    )


def _decision(event_id: str, *, when: datetime = NOW) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=event_id,
        canonical_market_id=f"mkt-{event_id}",
        fixture_canonical_event_id=event_id,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        scanned_at=when,
        eligible_for_paper_simulation=False,
    )


def _report(
    *,
    when: datetime = NOW,
    venue_health: dict[str, str] | None = None,
    scan_lane: str = ScanLane.HOT.value,
) -> CollectionReport:
    fixture = _fixture("evt-n4")
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[_decision("evt-n4", when=when)],
        discovered_fixtures=[fixture],
        scan_lane=scan_lane,
        venue_health=venue_health
        or {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        operator_summary="test",
        fixture_identity_aliases={fixture.canonical_event_id: fixture.canonical_event_id},
        fixture_source_events={
            fixture.canonical_event_id: [
                {
                    "venue": "polymarket",
                    "source_event_id": fixture.source_event_id,
                    "raw": {"id": fixture.source_event_id},
                }
            ]
        },
    )


def test_last_scan_clause_separates_configured_from_unavailable_matchbook() -> None:
    clause = last_scan_venue_clause(
        {"matchbook": "unavailable", "polymarket": "ok", "kalshi": "ok"},
        configured=[VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
    )
    assert "last scan PM·K" in clause
    assert "MB unavailable" in clause
    assert "last scan MB" not in clause


def test_last_scan_clause_pmk_only_does_not_claim_matchbook() -> None:
    clause = last_scan_venue_clause(
        {"matchbook": "disabled", "polymarket": "ok", "kalshi": "ok"},
        configured=[VenueName.POLYMARKET, VenueName.KALSHI],
    )
    assert clause == "last scan PM·K"
    assert "MB" not in clause


def test_hot_operator_summary_uses_stamped_clocks_not_zero_seconds_ago() -> None:
    completed = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    next_due = completed + timedelta(seconds=30)
    summary = _hot_operator_summary(
        completed,
        4100,
        next_due,
        7,
        0,
        False,
        venue_health={"matchbook": "unavailable", "polymarket": "ok", "kalshi": "ok"},
        active_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
    )
    assert "completed at 2026-09-16T12:00:00Z" in summary
    assert "next due 2026-09-16T12:00:30Z" in summary
    assert "0s ago" not in summary
    assert "last scan PM·K" in summary
    assert "MB unavailable" in summary


def test_record_report_does_not_imply_matchbook_participation_when_unavailable() -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.record_report(
        _report(
            venue_health={
                "matchbook": "unavailable",
                "polymarket": "ok",
                "kalshi": "ok",
            }
        ),
        scan_lane=ScanLane.HOT,
    )
    status = coordinator.public_status()
    assert status.venue_health["matchbook"] == "unavailable"
    assert status.hot.venue_health["matchbook"] == "unavailable"
    assert status.hot.active_venues == [
        VenueName.MATCHBOOK,
        VenueName.POLYMARKET,
        VenueName.KALSHI,
    ]
    summary = status.hot.operator_summary or ""
    assert "completed at 2026-09-16T12:00:00Z" in summary
    assert "0s ago" not in summary
    assert "last scan PM·K" in summary
    assert "MB unavailable" in summary
    combined = status.operator_summary or ""
    assert "MB unavailable" in combined
    assert "0s ago" not in combined


def test_provider_recovery_restores_matchbook_last_scan_participation() -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.record_report(
        _report(
            venue_health={
                "matchbook": "unavailable",
                "polymarket": "ok",
                "kalshi": "ok",
            }
        ),
        scan_lane=ScanLane.HOT,
    )
    coordinator.record_report(
        _report(
            when=NOW + timedelta(seconds=30),
            venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        ),
        scan_lane=ScanLane.HOT,
    )
    summary = coordinator.public_status().hot.operator_summary or ""
    assert "last scan MB·PM·K" in summary
    assert "MB unavailable" not in summary
    assert "completed at 2026-09-16T12:00:30Z" in summary


def test_scheduled_then_manual_scan_keeps_named_clocks() -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.record_report(
        _report(when=NOW, venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"}),
        scan_lane=ScanLane.HOT,
    )
    later = NOW + timedelta(seconds=12)
    coordinator.record_explicit_report(
        _report(
            when=later,
            venue_health={"matchbook": "timeout", "polymarket": "ok", "kalshi": "ok"},
            scan_lane=ScanLane.UNIVERSE.value,
        )
    )
    status = coordinator.public_status()
    assert status.venue_health["matchbook"] == "timeout"
    assert "0s ago" not in (status.operator_summary or "")
    assert status.last_completed_at == later
    assert status.hot.last_completed_at == NOW
    hot_summary = status.hot.operator_summary or ""
    assert "completed at 2026-09-16T12:00:00Z" in hot_summary
    assert status.hot.venue_health["matchbook"] == "ok"
    assert status.venue_health["matchbook"] == "timeout"


def test_hot_last_scan_truth_survives_later_universe_recovery() -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.record_report(
        _report(
            venue_health={
                "matchbook": "unavailable",
                "polymarket": "ok",
                "kalshi": "ok",
            }
        ),
        scan_lane=ScanLane.HOT,
    )
    coordinator.record_report(
        _report(
            when=NOW + timedelta(seconds=40),
            scan_lane=ScanLane.UNIVERSE.value,
            venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    status = coordinator.public_status()
    assert status.venue_health["matchbook"] == "ok"
    assert status.hot.venue_health["matchbook"] == "unavailable"
    assert status.universe.venue_health["matchbook"] == "ok"
    hot_summary = status.hot.operator_summary or ""
    universe_summary = status.universe.operator_summary or ""
    assert "last scan PM·K" in hot_summary
    assert "MB unavailable" in hot_summary
    assert "last scan MB·PM·K" in universe_summary
    assert "MB unavailable" not in universe_summary
    assert status.hot.last_completed_at == NOW
    assert status.universe.last_completed_at == NOW + timedelta(seconds=40)


def test_universe_last_scan_truth_survives_later_hot_recovery() -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.record_report(
        _report(
            scan_lane=ScanLane.UNIVERSE.value,
            venue_health={
                "matchbook": "unavailable",
                "polymarket": "ok",
                "kalshi": "ok",
            },
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    coordinator.record_report(
        _report(
            when=NOW + timedelta(seconds=30),
            venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        ),
        scan_lane=ScanLane.HOT,
    )
    status = coordinator.public_status()
    assert status.venue_health["matchbook"] == "ok"
    assert status.universe.venue_health["matchbook"] == "unavailable"
    assert status.hot.venue_health["matchbook"] == "ok"
    universe_summary = status.universe.operator_summary or ""
    hot_summary = status.hot.operator_summary or ""
    assert "last scan PM·K" in universe_summary
    assert "MB unavailable" in universe_summary
    assert "last scan MB·PM·K" in hot_summary
    assert "MB unavailable" not in hot_summary
