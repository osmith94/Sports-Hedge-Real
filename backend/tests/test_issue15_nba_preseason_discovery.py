"""Issue #15: Matchbook NBA Preseason is listed and was filtered out.

Captured 2026-10-08 public GETs. Prices stripped. Execution stays disabled.
The Approved Match Register still does not admit Matchbook↔Polymarket NBA.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from sports_hedge.application.collector import (
    _fixture_from_cluster,
    matchbook_scope_discovery_params,
)
from sports_hedge.application.fixture_clusters import VenueEvent, cluster_venue_events
from sports_hedge.application.target_competitions import (
    REJECTED_NON_NCAAB_BASKETBALL,
    UNKNOWN_COMPETITION,
    filter_in_scope_events,
    resolve_target_competition,
    scope_matchbook_event,
)
from sports_hedge.catalogue.admission import assess_catalogue_admission
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.nba.constants import (
    MATCHBOOK_NBA_COMPETITION_TAG_ID,
    MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID,
    NBA_PAIR_UNAPPROVED_REASON,
)
from sports_hedge.nba.detect import is_nba_payload, matchbook_nba_listing_phase
from sports_hedge.normalization.venues import (
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)
from sports_hedge.venues.matchbook import MATCHBOOK_SESSION_PATH, MatchbookClient

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "nba"
    / "matchbook_nba_preseason_2026_10_08.json"
)
CAPTURED_AT = datetime(2026, 10, 8, 16, 42, tzinfo=UTC)


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def _event(name: str) -> dict:
    for event in _load()["matchbook_preseason_events"]:
        if event["name"] == name:
            return event
    raise AssertionError(name)


def _market(event: dict, name: str, handicap=None) -> dict:
    for market in event["markets"]:
        if market["name"] == name and market.get("handicap") == handicap:
            return market
    raise AssertionError((name, handicap))


def _basketball(name: str, *, competition: str, tag_id: str, sport_id: str = "4") -> dict:
    return {
        "id": f"synthetic-{tag_id}",
        "name": name,
        "sport-id": sport_id,
        "start": "2026-10-08T23:00:00.000Z",
        "status": "open",
        "meta-tags": [
            {"id": sport_id, "name": "Basketball", "type": "SPORT"},
            {"id": tag_id, "name": competition, "type": "COMPETITION"},
        ],
    }


def _venue_column(matchbook: bool, polymarket: bool, kalshi: bool) -> str:
    """Same tokens as frontend hotVenuePresenceLabel."""

    return " / ".join(
        [
            "MB" if matchbook else "—",
            "PM" if polymarket else "—",
            "K" if kalshi else "—",
        ]
    )


def _cluster(matchbook: list[dict], polymarket: list[dict]):
    matcher = EventMatcher()
    mb_events = []
    for payload in matchbook:
        canonical = MatchbookNormalizer().normalize_event(payload)
        mb_events.append(
            VenueEvent(VenueName.MATCHBOOK, payload, canonical, canonical.source_event_id)
        )
    pm_events = []
    for payload in polymarket:
        canonical = PolymarketNormalizer().normalize_event(payload)
        pm_events.append(
            VenueEvent(VenueName.POLYMARKET, payload, canonical, canonical.source_event_id)
        )
    clusters, _counts = cluster_venue_events(
        matchbook=mb_events,
        polymarket=pm_events,
        kalshi=[],
        matcher=matcher,
        max_event_pairs=20,
    )
    return clusters


def test_captured_preseason_tag_is_nba_and_regular_tag_is_not_the_game_book() -> None:
    census = _load()["census"]
    assert census["filters"]["tag_ids_406202315670010"]["total"] == 1
    assert census["filters"]["tag_ids_931295691050041"]["total"] == 6
    assert census["filters"]["tag_ids_comma_both_is_AND"]["total"] == 0
    assert census["polymarket_series_10345_open_events"] == 35
    celtics = _event("Boston Celtics at Cleveland Cavaliers")
    kings = _event("Sacramento Kings at Los Angeles Lakers")
    assert matchbook_nba_listing_phase(celtics) == "preseason"
    assert matchbook_nba_listing_phase(kings) == "preseason"
    assert is_nba_payload(celtics) is True
    assert is_nba_payload(kings) is True
    assert str(celtics["id"]) == "34545720020100023"
    assert celtics["start"] == "2026-10-08T23:00:00.000Z"
    assert kings["start"] == "2026-10-09T02:30:00.000Z"
    assert resolve_target_competition("NBA Preseason").code.value == "nba"
    assert resolve_target_competition("NBA Summer League") is None
    assert resolve_target_competition("NBA G League") is None


def test_preseason_scope_accepts_nba_and_rejects_other_basketball() -> None:
    celtics = _event("Boston Celtics at Cleveland Cavaliers")
    euro = _load()["matchbook_euroleague_not_nba"]
    mixed = ["premier_league", "nfl", "nba", "mlb"]
    allowed = scope_matchbook_event(celtics, selected_codes=mixed)
    assert allowed.allowed is True
    assert allowed.competition is not None
    assert allowed.competition.code.value == "nba"
    assert allowed.label == "NBA Preseason"
    nba_only = scope_matchbook_event(celtics, selected_codes=["nba"])
    assert nba_only.allowed is True
    both = scope_matchbook_event(celtics, selected_codes=["nba", "ncaab"])
    assert both.allowed is True
    assert both.competition.code.value == "nba"
    ncaab_only = scope_matchbook_event(celtics, selected_codes=["ncaab"])
    assert ncaab_only.allowed is False
    assert ncaab_only.reason == REJECTED_NON_NCAAB_BASKETBALL
    dropped = filter_in_scope_events([euro], venue=VenueName.MATCHBOOK, selected_codes=mixed)
    assert dropped.allowed == []
    assert dropped.skipped_by_reason[UNKNOWN_COMPETITION] == 1
    for competition, tag_id in (
        ("WNBA", "502879947700009"),
        ("NBA Summer League", "summer"),
        ("NBA G League", "gleague"),
        ("Exhibition", "exhibition"),
    ):
        payload = _basketball("Boston Celtics at Cleveland Cavaliers", competition=competition, tag_id=tag_id)
        assert is_nba_payload(payload) is False
        decision = scope_matchbook_event(payload, selected_codes=["nba"])
        assert decision.allowed is False
    ncaa = _basketball(
        "Duke at North Carolina",
        competition="NBA Preseason",
        tag_id=MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID,
        sport_id="5",
    )
    ncaa["meta-tags"][0] = {"id": "5", "name": "NCAA Basketball", "type": "SPORT"}
    assert is_nba_payload(ncaa) is False


def test_two_preseason_fixtures_normalise_and_only_the_listed_one_matches_polymarket() -> None:
    celtics = _event("Boston Celtics at Cleveland Cavaliers")
    kings = _event("Sacramento Kings at Los Angeles Lakers")
    pm = _load()["polymarket_bos_cle"]
    absent = _load()["polymarket_hou_dal_absent_on_matchbook"]
    mb_event = MatchbookNormalizer().normalize_event(celtics)
    kings_event = MatchbookNormalizer().normalize_event(kings)
    pm_event = PolymarketNormalizer().normalize_event(pm)
    assert mb_event.home_team == "cleveland cavaliers"
    assert mb_event.away_team == "boston celtics"
    assert mb_event.kickoff_utc == pm_event.kickoff_utc
    assert mb_event.source_event_id == "34545720020100023"
    assert pm_event.source_event_id == "1116715"
    assert pm_event.home_team == "cleveland cavaliers"
    assert pm_event.away_team == "boston celtics"
    assert kings_event.home_team == "los angeles lakers"
    assert kings_event.away_team == "sacramento kings"
    assert kings_event.source_event_id == "34546973226700023"
    assert "Rockets vs. Mavericks" == absent["title"]
    assert absent["title"] not in _load()["census"]["matchbook_preseason_names"]

    matched = _cluster([celtics], [pm])
    assert len(matched) == 1
    assert matched[0].matchbook is not None
    assert matched[0].polymarket is not None
    fixture = _fixture_from_cluster(
        matched[0],
        seen_at=CAPTURED_AT,
        polymarket_events=[],
        queried_series_ids=["10345"],
    )
    assert fixture.matchbook_matched is True
    assert fixture.polymarket_matched is True
    assert fixture.kalshi_matched is False
    assert _venue_column(True, True, False) == "MB / PM / —"
    assert fixture.home_team == "cleveland cavaliers"
    assert fixture.source_event_id == "34545720020100023"

    reversed_payload = json.loads(json.dumps(celtics))
    reversed_payload["name"] = "Cleveland Cavaliers at Boston Celtics"
    reversed_clusters = _cluster([reversed_payload], [pm])
    assert len(reversed_clusters) == 2
    assert all(cluster.venue_count == 1 for cluster in reversed_clusters)

    later = json.loads(json.dumps(pm))
    later["startTime"] = "2026-10-22T23:00:00Z"
    later["id"] = "regular-season-not-this-preseason-game"
    separated = _cluster([celtics], [later])
    assert len(separated) == 2

    rockets = _cluster([], [absent])
    assert len(rockets) == 1
    assert rockets[0].matchbook is None
    missing = _fixture_from_cluster(
        rockets[0],
        seen_at=CAPTURED_AT,
        polymarket_events=[],
        queried_series_ids=["10345"],
    )
    assert missing.matchbook_matched is False
    assert missing.polymarket_matched is True
    assert missing.kalshi_matched is False
    assert _venue_column(False, True, False) == "— / PM / —"


def test_preseason_moneyline_is_inspected_but_not_register_admitted() -> None:
    celtics = _event("Boston Celtics at Cleveland Cavaliers")
    pm = _load()["polymarket_bos_cle"]
    event = MatchbookNormalizer().normalize_event(celtics)
    pm_event = PolymarketNormalizer().normalize_event(pm)
    moneyline = MatchbookNormalizer().normalize_market(event, _market(celtics, "Moneyline"))
    pm_market = PolymarketNormalizer().normalize_market(pm_event, pm["markets"][0])
    assert {runner.outcome.value for runner in moneyline.runners} == {"home", "away"}
    assert registered_canonical_key(moneyline, pm_market) is None
    match = MarketMatcher().match(moneyline, pm_market)
    assert match.matched is False
    assert NBA_PAIR_UNAPPROVED_REASON in match.reasons
    admission = assess_catalogue_admission(moneyline, pm_market)
    assert admission.admitted is False
    assert admission.assessment.reason == NBA_PAIR_UNAPPROVED_REASON
    half = _market(celtics, "Handicap", -5.5)
    integer = _market(celtics, "Handicap", -5)
    period = _market(celtics, "1st Half Total", 108.5)
    MatchbookNormalizer().normalize_market(event, half)
    with pytest.raises(VenueNormalizationError, match="half-point"):
        MatchbookNormalizer().normalize_market(event, integer)
    with pytest.raises(VenueNormalizationError, match="period"):
        MatchbookNormalizer().normalize_market(event, period)
    assert Settings.model_fields["sports_hedge_execution_enabled"].default is False


def test_nba_only_discovery_unions_tags_without_an_and_query() -> None:
    params = matchbook_scope_discovery_params(["nba"], basketball_sport_id="4")
    assert params["sport-ids"] == "4"
    assert params["tag-id-union"] == (
        f"{MATCHBOOK_NBA_COMPETITION_TAG_ID},{MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID}"
    )
    assert "tag-ids" not in params
    mixed = matchbook_scope_discovery_params(
        ["premier_league", "nfl", "nba", "mlb"],
        football_sport_id="15",
        american_football_sport_id="1",
        basketball_sport_id="4",
        baseball_sport_id="3",
    )
    assert mixed["sport-ids"] == "15,1,4,3"
    assert "tag-ids" not in mixed
    assert "tag-id-union" not in mixed


@pytest.mark.asyncio
async def test_matchbook_tag_union_is_two_gets_inside_one_list_events() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            return httpx.Response(200, json={"session-token": "paper-session"})
        if request.url.path == "/edge/rest/events":
            params = dict(request.url.params)
            seen.append(params)
            tag = params.get("tag-ids")
            if tag == MATCHBOOK_NBA_COMPETITION_TAG_ID:
                events = [{"id": 33613052933500045, "name": "NBA Championship Winner 2026/27"}]
            elif tag == MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID:
                events = [{"id": 34545720020100023, "name": "Boston Celtics at Cleveland Cavaliers"}]
            else:
                events = []
            return httpx.Response(
                200,
                json={"total": len(events), "per-page": 100, "offset": 0, "events": events},
            )
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.matchbook.com") as client:
        venue = MatchbookClient(
            Settings(matchbook_username="paper-user", matchbook_password="paper-pass"),
            client=client,
        )
        payload = await venue.list_events(
            **{
                "sport-ids": "4",
                "tag-id-union": (
                    f"{MATCHBOOK_NBA_COMPETITION_TAG_ID},"
                    f"{MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID}"
                ),
                "after": 1,
                "before": 2,
            }
        )
    assert [event["id"] for event in payload["events"]] == [
        33613052933500045,
        34545720020100023,
    ]
    assert payload["truncated"] is False
    assert [item.get("tag-ids") for item in seen] == [
        MATCHBOOK_NBA_COMPETITION_TAG_ID,
        MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID,
    ]
    assert all("tag-id-union" not in item for item in seen)
    price_engine = Path(__file__).parents[1] / "src" / "sports_hedge" / "application" / "price_engine.py"
    assert "tag-id-union" not in price_engine.read_text()
