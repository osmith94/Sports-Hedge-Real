from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from sports_hedge.application.manchester_derby_logical import (
    DERBY_KICKOFF_UTC,
    LIVE_LOGICAL_ENV,
    is_named_derby_payload,
    is_named_derby_title,
    run_manchester_derby_logical,
)
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.venues import KalshiNormalizer, PolymarketNormalizer

# Captured 2026-09-13 public payload shapes. These lock matcher/normalizer behaviour;
# they do not prove today's live books.
CAPTURED_PM_DERBY = {
    "id": "939685",
    "title": "Manchester United FC vs. Manchester City FC",
    "startTime": "2026-09-13T15:30:00Z",
    "series": [{"id": 10188, "title": "Premier League 2025"}],
}
CAPTURED_KALSHI_GAME = {
    "event_ticker": "KXEPLGAME-26SEP13MUNMCI",
    "series_ticker": "KXEPLGAME",
    "title": "Manchester United vs Manchester City",
    "category": "Sports",
    "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
    "sub_title": "MUN vs MCI (Sep 13)",
    "markets": [
        {
            "ticker": "KXEPLGAME-26SEP13MUNMCI-MUN",
            "title": "Manchester United wins",
            "yes_sub_title": "Manchester United",
            "occurrence_datetime": "2026-09-13T18:30:00Z",
            "expected_expiration_time": "2026-09-13T18:30:00Z",
        }
    ],
}


def test_named_derby_title_and_date_filter() -> None:
    assert is_named_derby_title("Manchester United FC vs. Manchester City FC")
    assert is_named_derby_title("Manchester United vs Manchester City: BTTS")
    assert is_named_derby_payload(CAPTURED_PM_DERBY)
    assert is_named_derby_payload(CAPTURED_KALSHI_GAME)
    assert not is_named_derby_payload(
        {
            "event_ticker": "KXEPLGAME-26SEP20MCISUN",
            "title": "Manchester City vs Sunderland",
            "markets": [{"occurrence_datetime": "2026-09-20T16:00:00Z"}],
        }
    )
    assert not is_named_derby_title("Leeds United vs Newcastle")


def test_captured_pm_kalshi_kickoff_mismatch_is_reported_not_guessed() -> None:
    polymarket = PolymarketNormalizer().normalize_event(CAPTURED_PM_DERBY)
    kalshi = KalshiNormalizer().normalize_event(CAPTURED_KALSHI_GAME)
    assert polymarket.kickoff_utc == DERBY_KICKOFF_UTC
    assert kalshi.kickoff_utc == datetime(2026, 9, 13, 18, 30, tzinfo=UTC)
    match = EventMatcher().match(polymarket, kalshi)
    assert match.matched is False
    assert "kickoff_outside_tolerance" in match.reasons


@pytest.mark.live_logical
@pytest.mark.skipif(
    os.environ.get(LIVE_LOGICAL_ENV) != "1",
    reason="Set SPORTS_HEDGE_LIVE_LOGICAL=1 to hit public Polymarket/Kalshi APIs",
)
@pytest.mark.asyncio
async def test_manchester_derby_live_logical_from_public_providers() -> None:
    report = await run_manchester_derby_logical()
    assert report["data_class"] == "live_provider_read_only"
    assert report["execution_enabled"] is False
    assert report["venues"]["polymarket"]["reachable"] is True
    assert report["venues"]["polymarket"]["discovered"] is True
    assert report["venues"]["kalshi"]["reachable"] is True
    assert report["venues"]["kalshi"]["discovered"] is True
    assert report["canonical_identity"]["reason"]
    pairwise = report["pairwise"]["polymarket_kalshi"]
    if pairwise["events_matched"]:
        assert pairwise["reason"]
    else:
        assert "event_identity_not_matched" in pairwise["reason"]
    if report["venues"]["matchbook"]["reachable"] is False:
        assert "owner-Windows" in report["matchbook_owner_requirement"]
