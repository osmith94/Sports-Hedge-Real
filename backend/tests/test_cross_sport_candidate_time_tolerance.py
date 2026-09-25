"""Candidate generation must be at least as wide as EventMatcher.

Synthetic events only. Not a live Arizona/Colorado result. PAPER / read-only.
Does not change MLB PAPER market admission.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    build_indexed_candidates,
    build_indexed_candidates_cooperative,
    cluster_venue_events,
)
from sports_hedge.application.hot_identity import same_hot_scheduling_unit
from sports_hedge.application.universe_matching_report import (
    STAGE_NO_CANDIDATE,
    build_universe_matching_report,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
from sports_hedge.mlb.identity import mlb_scheduled_games_compatible
from sports_hedge.mlb.normalize import scheduled_game_key
from sports_hedge.mlb.register import mlb_approved_paper_venue_pair
from sports_hedge.tennis.constants import TENNIS_SUPPORTING_KICKOFF_WINDOW_SECONDS

BASE = datetime(2026, 9, 24, 19, 10, tzinfo=UTC)
WINDOW = timedelta(minutes=5)
INSIDE = (
    timedelta(0),
    timedelta(minutes=1),
    timedelta(minutes=4, seconds=59),
    timedelta(minutes=5),
)
OUTSIDE = timedelta(minutes=5, seconds=1)

SCHEDULED = (
    ("football", "football", "Premier League", "Brentford", "Chelsea", None),
    ("nfl", "american_football", "NFL", "Kansas City Chiefs", "Indianapolis Colts", None),
    ("nba", "basketball", "NBA", "Los Angeles Lakers", "Boston Celtics", None),
    (
        "ncaab",
        "basketball",
        "NCAA Men's Basketball",
        "Duke Blue Devils",
        "Gonzaga Bulldogs",
        None,
    ),
    ("mlb", "baseball", "mlb", "Arizona Diamondbacks", "Colorado Rockies", "mlb"),
)


def _event(
    venue: VenueName,
    source_id: str,
    *,
    sport: str,
    competition: str,
    home: str,
    away: str,
    kickoff: datetime,
    game_key: str | None = None,
    tournament: str = "",
    round_label: str = "",
    event_type: str = "",
) -> VenueEvent:
    canonical = CanonicalEvent(
        sport=sport,
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_id,
        scheduled_game_key=game_key,
        tournament=tournament,
        round_label=round_label,
        event_type=event_type,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_id},
        canonical=canonical,
        source_event_id=source_id,
    )


def _mlb_key(kickoff: datetime, *labels: object) -> str:
    return scheduled_game_key(kickoff, *labels)


def _scheduled_pair(sport_row: tuple, delta: timedelta) -> tuple[VenueEvent, VenueEvent]:
    _name, sport, competition, home, away, mlb = sport_row
    left_key = _mlb_key(BASE) if mlb else None
    right_kickoff = BASE + delta
    right_key = _mlb_key(right_kickoff) if mlb else None
    left = _event(
        VenueName.MATCHBOOK,
        "mb",
        sport=sport,
        competition=competition,
        home=home,
        away=away,
        kickoff=BASE,
        game_key=left_key,
    )
    right = _event(
        VenueName.KALSHI,
        "k",
        sport=sport,
        competition=competition,
        home=home,
        away=away,
        kickoff=right_kickoff,
        game_key=right_key,
    )
    return left, right


def _has_pair(candidates: list[tuple[VenueEvent, VenueEvent]], left: VenueEvent, right: VenueEvent) -> bool:
    wanted = {left.source_event_id, right.source_event_id}
    return any({item.source_event_id, other.source_event_id} == wanted for item, other in candidates)


@pytest.mark.parametrize("sport_row", SCHEDULED, ids=lambda row: row[0])
@pytest.mark.parametrize("delta", INSIDE, ids=lambda delta: f"{int(delta.total_seconds())}s")
def test_scheduled_team_drift_reaches_the_matcher(sport_row: tuple, delta: timedelta) -> None:
    left, right = _scheduled_pair(sport_row, delta)
    matcher = EventMatcher(kickoff_tolerance=WINDOW)
    candidates, _diag = build_indexed_candidates(
        [left, right], kickoff_tolerance=matcher.kickoff_tolerance, matcher=matcher
    )
    assert _has_pair(candidates, left, right)
    assert matcher.could_match(left.canonical, right.canonical) is True
    assert matcher.match(left.canonical, right.canonical).matched is True


@pytest.mark.parametrize("sport_row", SCHEDULED, ids=lambda row: row[0])
def test_scheduled_team_outside_five_minutes_is_not_identity(sport_row: tuple) -> None:
    left, right = _scheduled_pair(sport_row, OUTSIDE)
    matcher = EventMatcher(kickoff_tolerance=WINDOW)
    assert matcher.could_match(left.canonical, right.canonical) is False
    assert matcher.match(left.canonical, right.canonical).matched is False
    candidates, _diag = build_indexed_candidates(
        [left, right], kickoff_tolerance=matcher.kickoff_tolerance, matcher=matcher
    )
    assert _has_pair(candidates, left, right) is False


def test_kickoff_bucket_boundary_14_59_and_15_00_still_generates() -> None:
    start = datetime(2026, 9, 24, 14, 59, tzinfo=UTC)
    left = _event(
        VenueName.MATCHBOOK,
        "mb-boundary",
        sport="football",
        competition="Premier League",
        home="Brentford",
        away="Chelsea",
        kickoff=start,
    )
    right = _event(
        VenueName.POLYMARKET,
        "pm-boundary",
        sport="football",
        competition="Premier League",
        home="Brentford",
        away="Chelsea",
        kickoff=datetime(2026, 9, 24, 15, 0, tzinfo=UTC),
    )
    matcher = EventMatcher(kickoff_tolerance=WINDOW)
    candidates, _diag = build_indexed_candidates(
        [left, right], kickoff_tolerance=matcher.kickoff_tolerance, matcher=matcher
    )
    assert _has_pair(candidates, left, right)
    assert matcher.match(left.canonical, right.canonical).matched is True


def test_arizona_colorado_one_minute_drift_is_one_fixture() -> None:
    """Owner-live shape: Matchbook 19:11, Kalshi/Polymarket 19:10. Fixture/demo."""

    matchbook_at = datetime(2026, 9, 24, 19, 11, tzinfo=UTC)
    other_at = datetime(2026, 9, 24, 19, 10, tzinfo=UTC)
    home, away = "Arizona Diamondbacks", "Colorado Rockies"
    matchbook = _event(
        VenueName.MATCHBOOK,
        "mb-ari-col",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=matchbook_at,
        game_key=_mlb_key(matchbook_at),
    )
    kalshi = _event(
        VenueName.KALSHI,
        "k-ari-col",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=other_at,
        game_key=_mlb_key(other_at),
    )
    polymarket = _event(
        VenueName.POLYMARKET,
        "pm-ari-col",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=other_at,
        game_key=_mlb_key(other_at),
    )
    assert matchbook.canonical.scheduled_game_key != kalshi.canonical.scheduled_game_key
    matcher = EventMatcher()
    candidates, _diag = build_indexed_candidates(
        [matchbook, kalshi, polymarket],
        kickoff_tolerance=matcher.kickoff_tolerance,
        matcher=matcher,
    )
    assert _has_pair(candidates, matchbook, kalshi)
    assert _has_pair(candidates, matchbook, polymarket)
    assert _has_pair(candidates, kalshi, polymarket)
    for left, right in ((matchbook, kalshi), (matchbook, polymarket), (kalshi, polymarket)):
        assert matcher.could_match(left.canonical, right.canonical) is True
        assert matcher.match(left.canonical, right.canonical).matched is True
    clusters, _counts = cluster_venue_events(
        matchbook=[matchbook],
        polymarket=[polymarket],
        kalshi=[kalshi],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert {item.source_event_id for item in clusters[0].events_for(VenueName.MATCHBOOK)} == {
        "mb-ari-col"
    }
    assert {item.source_event_id for item in clusters[0].events_for(VenueName.KALSHI)} == {
        "k-ari-col"
    }
    assert {item.source_event_id for item in clusters[0].events_for(VenueName.POLYMARKET)} == {
        "pm-ari-col"
    }
    evidence = {
        "identity_evidence": "retained",
        "nodes": [
            {
                "venue": item.venue.value,
                "source_event_id": item.source_event_id,
                "sport": "baseball",
                "competition": "mlb",
                "home_team": home,
                "away_team": away,
                "kickoff_utc": item.canonical.kickoff_utc.isoformat(),
                "scheduled_game_key": item.canonical.scheduled_game_key,
                "normalization_evidence": "retained",
                "candidate_generation_evidence": "retained",
            }
            for item in (matchbook, kalshi, polymarket)
        ],
        "clusters": [
            {
                "canonical_event_id": "evt-ari-col",
                "member_keys": [
                    [item.venue.value, item.source_event_id]
                    for item in (matchbook, kalshi, polymarket)
                ],
                "venues": ["matchbook", "kalshi", "polymarket"],
                "pair_kinds": ["matchbook_kalshi", "matchbook_polymarket", "polymarket_kalshi"],
            }
        ],
        "generated_pairs": [
            {
                "left_venue": left.venue.value,
                "left_source_event_id": left.source_event_id,
                "right_venue": right.venue.value,
                "right_source_event_id": right.source_event_id,
                "scored": True,
            }
            for left, right in ((matchbook, kalshi), (matchbook, polymarket), (kalshi, polymarket))
        ],
        "scored_pairs": [],
        "meta": {
            "enabled_venues": ["matchbook", "kalshi", "polymarket"],
            "completeness": "complete",
            "source_event_counts": {
                "raw_by_venue": {"matchbook": 1, "kalshi": 1, "polymarket": 1}
            },
        },
    }
    report = build_universe_matching_report(evidence)
    fixture = report["fixtures"][0]
    assert fixture["matched"] is True
    assert fixture["missing_venues"] == []
    rendered = str(fixture)
    assert STAGE_NO_CANDIDATE not in rendered


def test_mlb_doubleheader_ordinals_stay_isolated() -> None:
    home, away = "Arizona Diamondbacks", "Colorado Rockies"
    game1_at = BASE
    game1_drift = BASE + timedelta(seconds=30)
    game2_at = BASE + timedelta(seconds=30)
    hours = BASE + timedelta(hours=6)
    game1 = _event(
        VenueName.MATCHBOOK,
        "g1",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=game1_at,
        game_key=_mlb_key(game1_at, "Game 1"),
    )
    game1_late = _event(
        VenueName.KALSHI,
        "g1-late",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=game1_drift,
        game_key=_mlb_key(game1_drift, "Game 1"),
    )
    game2 = _event(
        VenueName.POLYMARKET,
        "g2",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=game2_at,
        game_key=_mlb_key(game2_at, "Game 2"),
    )
    no_ordinal = _event(
        VenueName.KALSHI,
        "plain",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=game1_drift,
        game_key=_mlb_key(game1_drift),
    )
    later = _event(
        VenueName.MATCHBOOK,
        "later",
        sport="baseball",
        competition="mlb",
        home=home,
        away=away,
        kickoff=hours,
        game_key=_mlb_key(hours),
    )
    matcher = EventMatcher()
    assert matcher.match(game1.canonical, game1_late.canonical).matched is True
    conflict = matcher.match(game1.canonical, game2.canonical)
    assert conflict.matched is False
    assert conflict.reasons == ["mlb_doubleheader_or_start_mismatch"]
    ambiguous = matcher.match(game1.canonical, no_ordinal.canonical)
    assert ambiguous.matched is False
    assert ambiguous.reasons == ["mlb_game_identity_ambiguous"]
    assert matcher.match(game1.canonical, later.canonical).matched is False
    candidates, _diag = build_indexed_candidates(
        [game1, game1_late, game2, no_ordinal, later],
        kickoff_tolerance=matcher.kickoff_tolerance,
        matcher=matcher,
    )
    assert _has_pair(candidates, game1, game1_late)
    assert _has_pair(candidates, game1, game2) is False
    assert _has_pair(candidates, game1, no_ordinal) is False
    assert _has_pair(candidates, game1, later) is False
    assert mlb_scheduled_games_compatible(game1.canonical.scheduled_game_key, None) == (
        False,
        "mlb_game_identity_ambiguous",
    )


def test_market_and_hot_layers_do_not_reapply_exact_minute_equality() -> None:
    from test_mlb_stage1 import _moneyline_markets

    from sports_hedge.matching.markets import MarketMatcher

    kalshi, polymarket, _matchbook = _moneyline_markets()
    drifted_at = kalshi.event.kickoff_utc + timedelta(minutes=1)
    drifted_event = kalshi.event.model_copy(
        update={
            "kickoff_utc": drifted_at,
            "scheduled_game_key": _mlb_key(drifted_at),
            "source_event_id": "k-drift",
        }
    )
    drifted = kalshi.model_copy(update={"event": drifted_event, "source_market_id": "drift"})
    assert mlb_approved_paper_venue_pair(polymarket, drifted) is False
    result = MarketMatcher().match(polymarket, drifted)
    assert "participant_identity_unproven" not in result.reasons
    assert "mlb_doubleheader_or_start_mismatch" not in result.reasons
    left = SimpleNamespace(
        sport="baseball",
        competition="mlb",
        home_team="Arizona Diamondbacks",
        away_team="Colorado Rockies",
        kickoff_utc=BASE,
        scheduled_game_key=_mlb_key(BASE),
        canonical_event_id="hot-a",
    )
    right = SimpleNamespace(
        sport="baseball",
        competition="mlb",
        home_team="Arizona Diamondbacks",
        away_team="Colorado Rockies",
        kickoff_utc=BASE + timedelta(minutes=1),
        scheduled_game_key=_mlb_key(BASE + timedelta(minutes=1)),
        canonical_event_id="hot-b",
    )
    game2 = SimpleNamespace(
        sport="baseball",
        competition="mlb",
        home_team="Arizona Diamondbacks",
        away_team="Colorado Rockies",
        kickoff_utc=BASE + timedelta(seconds=30),
        scheduled_game_key=_mlb_key(BASE + timedelta(seconds=30), "Game 2"),
        canonical_event_id="hot-g2",
    )
    game1 = SimpleNamespace(
        sport="baseball",
        competition="mlb",
        home_team="Arizona Diamondbacks",
        away_team="Colorado Rockies",
        kickoff_utc=BASE,
        scheduled_game_key=_mlb_key(BASE, "Game 1"),
        canonical_event_id="hot-g1",
    )
    assert same_hot_scheduling_unit(left, right) is True
    assert same_hot_scheduling_unit(game1, game2) is False


def _true_match_keys(items: list[VenueEvent], matcher: EventMatcher) -> set[frozenset[str]]:
    found: set[frozenset[str]] = set()
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            result = matcher.match(left.canonical, right.canonical)
            if result.matched:
                assert matcher.could_match(left.canonical, right.canonical) is True
                found.add(frozenset({left.source_event_id, right.source_event_id}))
    return found


def _candidate_keys(candidates: list[tuple[VenueEvent, VenueEvent]]) -> set[frozenset[str]]:
    return {
        frozenset({left.source_event_id, right.source_event_id}) for left, right in candidates
    }


def _oracle_items() -> list[VenueEvent]:
    items: list[VenueEvent] = []
    for sport_row in SCHEDULED:
        left, right = _scheduled_pair(sport_row, timedelta(minutes=1))
        left = _event(
            left.venue,
            f"{sport_row[0]}-mb",
            sport=left.canonical.sport,
            competition=left.canonical.competition,
            home=left.canonical.home_team,
            away=left.canonical.away_team,
            kickoff=left.canonical.kickoff_utc,
            game_key=left.canonical.scheduled_game_key,
        )
        right = _event(
            right.venue,
            f"{sport_row[0]}-k",
            sport=right.canonical.sport,
            competition=right.canonical.competition,
            home=right.canonical.home_team,
            away=right.canonical.away_team,
            kickoff=right.canonical.kickoff_utc,
            game_key=right.canonical.scheduled_game_key,
        )
        items.extend((left, right))
    tennis_at = BASE
    tennis_later = BASE + timedelta(days=2)
    items.append(
        _event(
            VenueName.MATCHBOOK,
            "tennis-mb",
            sport="tennis",
            competition="ATP",
            home="Jie Cui",
            away="Adolfo Vallejo",
            kickoff=tennis_at,
            tournament="atp hangzhou",
            round_label="r16",
            event_type="singles",
        )
    )
    items.append(
        _event(
            VenueName.POLYMARKET,
            "tennis-pm",
            sport="tennis",
            competition="ATP",
            home="Adolfo Vallejo",
            away="Jie Cui",
            kickoff=tennis_later,
            tournament="atp hangzhou",
            round_label="r16",
            event_type="singles",
        )
    )
    return items


def test_indexed_candidates_cover_every_authoritative_match() -> None:
    matcher = EventMatcher()
    items = _oracle_items()
    exhaustive = _true_match_keys(items, matcher)
    assert exhaustive
    sync, _diag = build_indexed_candidates(
        items, kickoff_tolerance=matcher.kickoff_tolerance, matcher=matcher
    )
    missing = exhaustive - _candidate_keys(sync)
    assert not missing
    too_wide = BASE + timedelta(seconds=TENNIS_SUPPORTING_KICKOFF_WINDOW_SECONDS + 1)
    late = _event(
        VenueName.KALSHI,
        "tennis-late",
        sport="tennis",
        competition="ATP",
        home="Jie Cui",
        away="Adolfo Vallejo",
        kickoff=too_wide,
        tournament="atp hangzhou",
        round_label="r16",
        event_type="singles",
    )
    early = next(item for item in items if item.source_event_id == "tennis-mb")
    assert matcher.match(early.canonical, late.canonical).matched is False


@pytest.mark.asyncio
async def test_cooperative_candidates_match_the_synchronous_index() -> None:
    matcher = EventMatcher()
    items = _oracle_items()
    sync, _sync_diag = build_indexed_candidates(
        items, kickoff_tolerance=matcher.kickoff_tolerance, matcher=matcher
    )
    coop, _coop_diag = await build_indexed_candidates_cooperative(
        items, kickoff_tolerance=matcher.kickoff_tolerance, matcher=matcher, yield_every=2
    )
    assert _candidate_keys(sync) == _candidate_keys(coop)
    exhaustive = _true_match_keys(items, matcher)
    assert exhaustive <= _candidate_keys(coop)
