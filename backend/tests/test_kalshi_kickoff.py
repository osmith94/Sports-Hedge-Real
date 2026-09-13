from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from sports_hedge.config import Settings
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.venues import KalshiNormalizer, PolymarketNormalizer, VenueNormalizationError
from sports_hedge.venues.kalshi import KalshiClient, attach_milestones_to_events

KICKOFF = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
EXPIRATION = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)

POLYMARKET_EVENT = {
    "id": "leeds-lei-pm",
    "title": "Leeds United vs. Leicester City",
    "startTime": KICKOFF.isoformat().replace("+00:00", "Z"),
    "series": [{"id": 10188, "title": "Premier League 2025"}],
}

KALSHI_EXPIRATION_ONLY = {
    "event_ticker": "KXEPLGAME-26SEP20LEELEI",
    "series_ticker": "KXEPLGAME",
    "title": "Leeds United vs Leicester City",
    "category": "Sports",
    "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
    "markets": [
        {
            "ticker": "KXEPLGAME-26SEP20LEELEI-LEE",
            "title": "Leeds United wins",
            "yes_sub_title": "Leeds United",
            "occurrence_datetime": EXPIRATION.isoformat().replace("+00:00", "Z"),
            "expected_expiration_time": EXPIRATION.isoformat().replace("+00:00", "Z"),
        }
    ],
}

KALSHI_MILESTONE = {
    "id": "ms-epl-leelei",
    "type": "soccer_game",
    "start_date": KICKOFF.isoformat().replace("+00:00", "Z"),
    "end_date": EXPIRATION.isoformat().replace("+00:00", "Z"),
    "primary_event_tickers": ["KXEPLGAME-26SEP20LEELEI"],
    "related_event_tickers": ["KXEPLGAME-26SEP20LEELEI"],
    "details": {"main_game_event_ticker": "KXEPLGAME-26SEP20LEELEI"},
}


def test_expiration_datetime_is_not_used_as_kickoff() -> None:
    with pytest.raises(VenueNormalizationError, match="scheduled kickoff"):
        KalshiNormalizer().normalize_event(KALSHI_EXPIRATION_ONLY)


def test_date_only_clock_is_refused() -> None:
    payload = {
        "event_ticker": "KXEPLGAME-26SEP20LEELEI",
        "series_ticker": "KXEPLGAME",
        "title": "Leeds United vs Leicester City",
        "strike_date": "2026-09-20",
        "markets": [
            {
                "ticker": "KXEPLGAME-26SEP20LEELEI-LEE",
                "occurrence_datetime": EXPIRATION.isoformat().replace("+00:00", "Z"),
                "expected_expiration_time": EXPIRATION.isoformat().replace("+00:00", "Z"),
            }
        ],
    }
    with pytest.raises(VenueNormalizationError, match="scheduled kickoff"):
        KalshiNormalizer().normalize_event(payload)


def test_milestone_start_date_is_scheduled_kickoff() -> None:
    payload = {**KALSHI_EXPIRATION_ONLY, "milestone": KALSHI_MILESTONE}
    kalshi = KalshiNormalizer().normalize_event(payload)
    polymarket = PolymarketNormalizer().normalize_event(POLYMARKET_EVENT)
    assert kalshi.kickoff_utc == KICKOFF
    assert kalshi.kickoff_utc != EXPIRATION
    match = EventMatcher().match(polymarket, kalshi)
    assert match.matched is True
    assert "kickoff_outside_tolerance" not in match.reasons


def test_attach_milestones_copies_start_date_onto_event() -> None:
    events = [{**KALSHI_EXPIRATION_ONLY}]
    attach_milestones_to_events(events, [KALSHI_MILESTONE])
    assert events[0]["start_date"] == KALSHI_MILESTONE["start_date"]
    assert events[0]["milestone"]["id"] == KALSHI_MILESTONE["id"]
    kalshi = KalshiNormalizer().normalize_event(events[0])
    assert kalshi.kickoff_utc == KICKOFF


@pytest.mark.asyncio
async def test_list_events_requests_milestones_and_attaches_start_date() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "events": [dict(KALSHI_EXPIRATION_ONLY)],
                    "milestones": [KALSHI_MILESTONE],
                },
            )
        return httpx.Response(404)

    settings = Settings(kalshi_series_tickers=["KXEPLGAME"], kalshi_event_max_pages=1)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        venue = KalshiClient(settings, client=http)
        payload = await venue.list_events()

    assert all(url.params.get("with_milestones") == "true" for url in seen)
    event = payload["events"][0]
    assert event["start_date"] == KALSHI_MILESTONE["start_date"]
    assert KalshiNormalizer().normalize_event(event).kickoff_utc == KICKOFF


@pytest.mark.asyncio
async def test_list_events_fetches_milestones_endpoint_when_events_lack_kickoff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"events": [dict(KALSHI_EXPIRATION_ONLY)]})
        if request.url.path.endswith("/milestones"):
            return httpx.Response(200, json={"milestones": [KALSHI_MILESTONE]})
        return httpx.Response(404)

    settings = Settings(kalshi_series_tickers=["KXEPLGAME"], kalshi_event_max_pages=1)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        venue = KalshiClient(settings, client=http)
        payload = await venue.list_events()

    event = payload["events"][0]
    assert event["start_date"] == KALSHI_MILESTONE["start_date"]
    assert KalshiNormalizer().normalize_event(event).kickoff_utc == KICKOFF
