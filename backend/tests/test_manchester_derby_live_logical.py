from __future__ import annotations

import os

import pytest

from sports_hedge.application.manchester_derby_logical import (
    DERBY_KICKOFF_UTC,
    LIVE_LOGICAL_ENV,
    is_named_derby_payload,
    is_named_derby_title,
    run_manchester_derby_logical,
)
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.venues import KalshiNormalizer, PolymarketNormalizer, VenueNormalizationError

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


def test_captured_kalshi_expiration_clock_is_not_kickoff() -> None:
    polymarket = PolymarketNormalizer().normalize_event(CAPTURED_PM_DERBY)
    assert polymarket.kickoff_utc == DERBY_KICKOFF_UTC
    with pytest.raises(VenueNormalizationError, match="scheduled kickoff"):
        KalshiNormalizer().normalize_event(CAPTURED_KALSHI_GAME)


def test_captured_kalshi_milestone_start_date_joins_polymarket() -> None:
    payload = {
        **CAPTURED_KALSHI_GAME,
        "milestone": {
            "id": "ms-epl-munmci",
            "type": "soccer_game",
            "start_date": "2026-09-13T15:30:00Z",
            "end_date": "2026-09-13T18:30:00Z",
            "primary_event_tickers": ["KXEPLGAME-26SEP13MUNMCI"],
            "related_event_tickers": ["KXEPLGAME-26SEP13MUNMCI"],
            "details": {"main_game_event_ticker": "KXEPLGAME-26SEP13MUNMCI"},
        },
    }
    polymarket = PolymarketNormalizer().normalize_event(CAPTURED_PM_DERBY)
    kalshi = KalshiNormalizer().normalize_event(payload)
    assert kalshi.kickoff_utc == DERBY_KICKOFF_UTC
    match = EventMatcher().match(polymarket, kalshi)
    assert match.matched is True
    assert "kickoff_outside_tolerance" not in match.reasons


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
    kalshi_events = report["venues"]["kalshi"]["events"]
    for item in kalshi_events:
        if item.get("normalized"):
            assert item["kickoff_utc"]
            assert "T18:30" not in item["kickoff_utc"]
        else:
            error = (item.get("normalize_error") or "").lower()
            assert "kickoff" in error
    pairwise = report["pairwise"]["polymarket_kalshi"]
    pm_normalized = [item for item in report["venues"]["polymarket"]["events"] if item.get("normalized")]
    kalshi_normalized = [item for item in kalshi_events if item.get("normalized")]
    if pm_normalized and kalshi_normalized:
        assert pairwise["events_matched"], report["canonical_identity"]["reason"]
    elif pairwise["events_matched"]:
        assert pairwise["reason"]
    else:
        assert "event_identity_not_matched" in pairwise["reason"] or "kickoff" in (
            report["canonical_identity"]["reason"] or ""
        )
    if report["venues"]["matchbook"]["reachable"] is False:
        requirement = report["matchbook_owner_requirement"].casefold()
        assert "windows" in requirement
        assert "matchbook" in requirement
    assert isinstance(report.get("family_reasons"), list)
    if pairwise["events_matched"]:
        assert report["family_reasons"] or pairwise["reason"]
