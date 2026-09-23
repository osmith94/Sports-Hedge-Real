"""Issue #534: discovered-fixture sport is read-model metadata, not identity."""

from __future__ import annotations

from datetime import UTC, datetime

from sports_hedge.application.approved_market_catalogue import DerivedPriceEngineItem
from sports_hedge.application.collector import _fixture_from_cluster
from sports_hedge.application.fixture_clusters import FixtureCluster, VenueEvent
from sports_hedge.application.fixture_sport import resolve_discovered_fixture_sport
from sports_hedge.application.price_engine import _discovered_fixture_sport
from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.venues import MatchbookNormalizer

SEEN_AT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _matchbook_fixture(payload: dict) -> object:
    canonical = MatchbookNormalizer().normalize_event(payload)
    cluster = FixtureCluster(
        matchbook_events=[
            VenueEvent(
                venue=VenueName.MATCHBOOK,
                raw=payload,
                canonical=canonical,
                source_event_id=canonical.source_event_id,
            )
        ]
    )
    return _fixture_from_cluster(
        cluster,
        seen_at=SEEN_AT,
        polymarket_events=[],
        queried_series_ids=None,
    )


def test_discovered_fixtures_expose_football_nfl_and_baseball_sports() -> None:
    football = _matchbook_fixture(
        {
            "id": "mb-pl",
            "name": "Arsenal vs Chelsea",
            "start": "2026-09-21T15:00:00Z",
            "competition-name": "Premier League",
            "sport-name": "Football",
        }
    )
    nfl = _matchbook_fixture(
        {
            "id": "mb-nfl",
            "name": "Indianapolis Colts at Kansas City Chiefs",
            "start": "2026-09-21T00:20:00Z",
            "sport-id": 1,
            "sport-name": "American Football",
            "meta-tags": [
                {"name": "American Football", "type": "SPORT"},
                {"name": "NFL", "type": "COMPETITION"},
            ],
        }
    )
    baseball = _matchbook_fixture(
        {
            "id": "mb-mlb",
            "name": "New York Yankees vs Boston Red Sox",
            "start": "2026-09-21T23:00:00Z",
            "competition-name": "MLB",
            "sport-name": "Baseball",
        }
    )

    assert football.sport == "football"
    assert football.target_competition_code == "premier_league"
    assert football.canonical_event_id
    assert nfl.sport == "american_football"
    assert nfl.target_competition_code == "nfl"
    assert baseball.sport == "baseball"
    assert baseball.target_competition_code is None
    assert len({football.sport, nfl.sport, baseball.sport}) == 3
    assert baseball.home_team == "New York Yankees"
    assert baseball.away_team == "Boston Red Sox"


def test_unrecognized_competition_labels_stay_unknown() -> None:
    mystery = _matchbook_fixture(
        {
            "id": "mb-card",
            "name": "Alpha vs Beta",
            "start": "2026-09-21T23:00:00Z",
            "competition-name": "Mystery Boxing Card",
        }
    )
    mlb_label_only = resolve_discovered_fixture_sport(
        competition="MLB",
        canonical_sport="football",
    )

    assert mystery.sport == "unknown"
    assert mlb_label_only == "unknown"


def test_price_engine_projection_sport_follows_catalogue_identity() -> None:
    football = DerivedPriceEngineItem(
        catalogue_row_id="row-pl",
        content_version=1,
        canonical_event_id="evt-pl",
        register_canonical_key="MATCH_RESULT_FT",
        competition="Premier League",
    )
    nfl = DerivedPriceEngineItem(
        catalogue_row_id="row-nfl",
        content_version=1,
        canonical_event_id="evt-nfl",
        register_canonical_key="NFL_GAME_WINNER_FT",
        competition="NFL",
    )
    unlabeled = DerivedPriceEngineItem(
        catalogue_row_id="row-unknown",
        content_version=1,
        canonical_event_id="evt-unknown",
        register_canonical_key="UNREGISTERED",
        competition="MLB",
    )

    assert _discovered_fixture_sport(football) == "football"
    assert _discovered_fixture_sport(nfl) == "american_football"
    assert _discovered_fixture_sport(unlabeled) == "unknown"
