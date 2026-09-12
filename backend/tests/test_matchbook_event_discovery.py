from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.venues.matchbook import (
    MatchbookClient,
    MatchbookDiscoveryError,
    select_football_sport_id,
)


SCAN_AT = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)
FOOTBALL_SPORT_ID = 15
TENNIS_SPORT_ID = 9


def test_select_football_sport_id_ignores_adjacent_football_labels() -> None:
    sport_id = select_football_sport_id(
        [
            {"id": 2, "name": "NCAA Football", "type": "SPORT"},
            {"id": 117, "name": "Gaelic Football", "type": "SPORT"},
            {"id": 9, "name": "Tennis", "type": "SPORT"},
            {"id": FOOTBALL_SPORT_ID, "name": "Football", "type": "SPORT"},
        ]
    )
    assert sport_id == FOOTBALL_SPORT_ID


def test_select_football_sport_id_accepts_soccer_alias() -> None:
    assert select_football_sport_id([{"id": "15", "name": "Soccer"}]) == 15


def test_select_football_sport_id_fails_closed_when_missing() -> None:
    with pytest.raises(MatchbookDiscoveryError, match="did not include"):
        select_football_sport_id([{"id": 9, "name": "Tennis"}])


def test_select_football_sport_id_fails_closed_when_ambiguous() -> None:
    with pytest.raises(MatchbookDiscoveryError, match="multiple"):
        select_football_sport_id(
            [
                {"id": 15, "name": "Football"},
                {"id": 99, "name": "Soccer"},
            ]
        )


def _settings(**overrides: Any) -> Settings:
    return Settings(
        matchbook_username="test-user",
        matchbook_password="test-password",
        **overrides,
    )


def _event(
    event_id: int,
    name: str,
    *,
    sport: str,
    competition: str,
    start: datetime = KICKOFF,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "name": name,
        "start": start.isoformat(),
        "sport-id": TENNIS_SPORT_ID if sport == "Tennis" else FOOTBALL_SPORT_ID,
        "sport-name": sport,
        "competition-name": competition,
        "status": "open",
    }


class MatchbookDiscoveryTransport:
    def __init__(self) -> None:
        self.event_offsets: list[int] = []
        self.sports_offsets: list[int] = []
        self.event_params: list[httpx.URL] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bpapi/rest/security/session":
            return httpx.Response(200, json={"session-token": "paper-session"})
        if request.url.path == "/edge/rest/lookups/sports":
            offset = int(request.url.params.get("offset", "0"))
            self.sports_offsets.append(offset)
            if offset == 0:
                return httpx.Response(
                    200,
                    json={
                        "total": 21,
                        "offset": 0,
                        "per-page": 20,
                        "sports": [
                            {"id": 2, "name": "NCAA Football", "type": "SPORT"},
                            {"id": 9, "name": "Tennis", "type": "SPORT"},
                            {"id": 117, "name": "Gaelic Football", "type": "SPORT"},
                            *[
                                {"id": 1000 + index, "name": f"Other Sport {index}", "type": "SPORT"}
                                for index in range(17)
                            ],
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "total": 21,
                    "offset": offset,
                    "per-page": 20,
                    "sports": [{"id": FOOTBALL_SPORT_ID, "name": "Football", "type": "SPORT"}],
                },
            )
        if request.url.path == "/edge/rest/events":
            assert request.headers["session-token"] == "paper-session"
            self.event_params.append(request.url)
            assert request.url.params.get("sport-ids") == str(FOOTBALL_SPORT_ID)
            assert request.url.params.get("after")
            assert request.url.params.get("before")
            offset = int(request.url.params.get("offset", "0"))
            self.event_offsets.append(offset)
            mixed = [
                _event(
                    10_000 + index,
                    f"Player {index} vs Player B",
                    sport="Tennis",
                    competition="ATP",
                )
                for index in range(100)
            ]
            targets = [
                _event(
                    8801,
                    "Tottenham Hotspur vs Everton",
                    sport="Football",
                    competition="Premier League",
                ),
                _event(
                    8802,
                    "Newcastle United vs Chelsea",
                    sport="Football",
                    competition="Premier League",
                ),
                _event(
                    8803,
                    "Athletic Bilbao vs Elche",
                    sport="Football",
                    competition="La Liga",
                ),
            ]
            if offset == 0:
                return httpx.Response(
                    200,
                    json={"offset": 0, "per-page": 100, "total": 103, "events": mixed},
                )
            if offset == 100:
                return httpx.Response(
                    200,
                    json={"offset": 100, "per-page": 100, "total": 103, "events": targets},
                )
            return httpx.Response(
                200,
                json={"offset": offset, "per-page": 100, "total": 103, "events": []},
            )
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_matchbook_paginates_past_mixed_first_page_and_keeps_concurrent_targets() -> None:
    transport = MatchbookDiscoveryTransport()
    settings = _settings()
    mock = httpx.MockTransport(transport.handler)
    async with httpx.AsyncClient(transport=mock, base_url=settings.matchbook_base_url) as http:
        venue = MatchbookClient(settings, client=http, clock=lambda: SCAN_AT)
        payload = await venue.list_events()

    ids = [item["id"] for item in payload["events"]]
    assert transport.event_offsets == [0, 100]
    assert 0 in transport.sports_offsets
    assert any(offset > 0 for offset in transport.sports_offsets)
    assert ids[0] == 10_000
    assert ids[100] == 8801
    assert 8801 in ids
    assert 8802 in ids
    assert 8803 in ids
    assert payload["truncated"] is False
    first_events = transport.event_params[0]
    after = int(first_events.params["after"])
    before = int(first_events.params["before"])
    assert after == int(datetime(2026, 9, 12, 11, 0, tzinfo=UTC).timestamp())
    assert before == int(datetime(2026, 9, 15, 17, 0, tzinfo=UTC).timestamp())
    assert after < int(KICKOFF.timestamp()) < before


@pytest.mark.asyncio
async def test_collector_discovers_premier_league_after_more_than_one_hundred_mixed_events() -> None:
    transport = MatchbookDiscoveryTransport()
    settings = _settings()
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    mock = httpx.MockTransport(transport.handler)
    async with httpx.AsyncClient(transport=mock, base_url=settings.matchbook_base_url) as http:
        matchbook = MatchbookClient(settings, client=http, clock=lambda: SCAN_AT)

        class EmptyPolymarket:
            async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
                del filters
                return []

            async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
                del event_id, filters
                return []

            async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                del args, kwargs
                raise AssertionError("order books should not be fetched without a match")

        collector = ReadOnlyCrossVenueCollector(
            matchbook=matchbook,
            polymarket=EmptyPolymarket(),
            paper_scan=PaperScanService(intelligence),
        )
        try:
            report = await collector.collect_and_scan(maximum_execution_risk=100)
        finally:
            repository.close()

    discovered = {item.source_event_id: item for item in report.discovered_fixtures}
    assert set(discovered) == {"8801", "8802", "8803"}
    assert discovered["8801"].home_team.lower().startswith("tottenham")
    assert discovered["8801"].target_competition_code == "premier_league"
    assert discovered["8802"].target_competition_code == "premier_league"
    assert discovered["8802"].kickoff_utc == discovered["8801"].kickoff_utc
    assert discovered["8803"].target_competition_code == "la_liga"
    assert report.raw_matchbook_events == 103
    assert not any(item.source_event_id.startswith("100") for item in report.discovered_fixtures)
    assert any(issue.stage == "target_competition" for issue in report.issues)


@pytest.mark.asyncio
async def test_matchbook_reports_explicit_issue_when_event_page_cap_is_hit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bpapi/rest/security/session":
            return httpx.Response(200, json={"session-token": "paper-session"})
        if request.url.path == "/edge/rest/lookups/sports":
            return httpx.Response(
                200,
                json={"sports": [{"id": FOOTBALL_SPORT_ID, "name": "Football", "type": "SPORT"}]},
            )
        if request.url.path == "/edge/rest/events":
            offset = int(request.url.params.get("offset", "0"))
            events = [
                _event(
                    offset + index,
                    f"Club {offset + index} vs Club B",
                    sport="Football",
                    competition="Bundesliga",
                )
                for index in range(100)
            ]
            return httpx.Response(
                200,
                json={"offset": offset, "per-page": 100, "total": 500, "events": events},
            )
        return httpx.Response(404)

    settings = _settings(matchbook_event_max_pages=2)
    mock = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=mock, base_url=settings.matchbook_base_url) as http:
        venue = MatchbookClient(settings, client=http, clock=lambda: SCAN_AT)
        payload = await venue.list_events()

    assert len(payload["events"]) == 200
    assert payload["truncated"] is True
    assert "truncated after 200 events" in payload["truncation-detail"]
