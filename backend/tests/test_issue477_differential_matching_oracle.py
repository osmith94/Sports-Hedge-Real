"""#477 verification: exhaustive EventMatcher vs indexed candidate generation.

Data class: synthetic/fixture events. Not live, historical, or modelled quotes.
PAPER / read-only. Production matching thresholds, competition veto, squad-category
safety, and market-family equivalence are not widened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from random import Random
from typing import Any

import pytest

from sports_hedge.application.fixture_clusters import (
    ClusterPass,
    VenueEvent,
    build_indexed_candidates,
    cluster_venue_events,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import (
    DEFAULT_EVENT_MATCH_THRESHOLD,
    PAPER_EVENT_MATCH_THRESHOLD,
    EventMatcher,
    known_target_competition_mismatch,
)


KICKOFF = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
KICKOFF_WINDOW = timedelta(minutes=5)
PRODUCTION_CLUSTER_VENUES = (
    VenueName.MATCHBOOK,
    VenueName.POLYMARKET,
    VenueName.KALSHI,
)


class LayoutVenue(StrEnum):
    """Synthetic layout labels, including a future fifth provider.

    ``betfair_exchange`` is test-only. It is not a production VenueName and
    must not be treated as a live adapter.
    """

    MATCHBOOK = "matchbook"
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    SMARKETS = "smarkets"
    BETFAIR = "betfair_exchange"


LAYOUTS: dict[int, tuple[LayoutVenue, ...]] = {
    2: (LayoutVenue.MATCHBOOK, LayoutVenue.POLYMARKET),
    3: (LayoutVenue.MATCHBOOK, LayoutVenue.POLYMARKET, LayoutVenue.KALSHI),
    4: (
        LayoutVenue.MATCHBOOK,
        LayoutVenue.POLYMARKET,
        LayoutVenue.KALSHI,
        LayoutVenue.SMARKETS,
    ),
    5: (
        LayoutVenue.MATCHBOOK,
        LayoutVenue.POLYMARKET,
        LayoutVenue.KALSHI,
        LayoutVenue.SMARKETS,
        LayoutVenue.BETFAIR,
    ),
}


@dataclass(frozen=True)
class IdentityCase:
    name: str
    home_left: str
    away_left: str
    home_right: str
    away_right: str
    competition_left: str
    competition_right: str
    kickoff_offset: timedelta
    expect_match: bool
    sibling: bool = False


IDENTITY_CASES: tuple[IdentityCase, ...] = (
    IdentityCase(
        name="accents",
        home_left="Atlético Madrid",
        away_left="Deportivo Alavés",
        home_right="Atletico Madrid",
        away_right="Deportivo Alaves",
        competition_left="La Liga",
        competition_right="La Liga",
        kickoff_offset=timedelta(0),
        expect_match=True,
    ),
    IdentityCase(
        name="safe_suffixes",
        home_left="Arsenal FC",
        away_left="Chelsea FC",
        home_right="Arsenal",
        away_right="Chelsea",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=timedelta(0),
        expect_match=True,
    ),
    IdentityCase(
        name="aliases",
        home_left="Espanyol Barcelona",
        away_left="Elche CF",
        home_right="Espanyol",
        away_right="Elche",
        competition_left="La Liga",
        competition_right="La Liga",
        kickoff_offset=timedelta(0),
        expect_match=True,
    ),
    IdentityCase(
        name="calcio_and_afc_aliases",
        home_left="Frosinone Calcio",
        away_left="AFC Bournemouth",
        home_right="Frosinone",
        away_right="Bournemouth",
        competition_left="Serie A",
        competition_right="Serie A",
        kickoff_offset=timedelta(0),
        expect_match=True,
    ),
    IdentityCase(
        name="kickoff_plus_five_minutes",
        home_left="Leeds United",
        away_left="Leicester City",
        home_right="Leeds United",
        away_right="Leicester City",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=KICKOFF_WINDOW,
        expect_match=True,
    ),
    IdentityCase(
        name="kickoff_outside_five_minutes",
        home_left="Leeds United",
        away_left="Leicester City",
        home_right="Leeds United",
        away_right="Leicester City",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=KICKOFF_WINDOW + timedelta(seconds=1),
        expect_match=False,
    ),
    IdentityCase(
        name="known_competition_conflict",
        home_left="Arsenal",
        away_left="Chelsea",
        home_right="Arsenal",
        away_right="Chelsea",
        competition_left="Premier League",
        competition_right="La Liga",
        kickoff_offset=timedelta(0),
        expect_match=False,
    ),
    IdentityCase(
        name="youth_incompatible",
        home_left="Arsenal",
        away_left="Chelsea",
        home_right="Arsenal U21",
        away_right="Chelsea",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=timedelta(0),
        expect_match=False,
    ),
    IdentityCase(
        name="women_incompatible",
        home_left="Chelsea",
        away_left="Arsenal",
        home_right="Chelsea Women",
        away_right="Arsenal",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=timedelta(0),
        expect_match=False,
    ),
    IdentityCase(
        name="reserve_incompatible",
        home_left="Chelsea FC",
        away_left="Arsenal FC",
        home_right="Chelsea Reserves",
        away_right="Arsenal",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=timedelta(0),
        expect_match=False,
    ),
    IdentityCase(
        name="unknown_competitions_same_label",
        home_left="Arsenal",
        away_left="Chelsea",
        home_right="Arsenal FC",
        away_right="Chelsea FC",
        competition_left="Regional Cup Alpha",
        competition_right="Regional Cup Alpha",
        kickoff_offset=timedelta(0),
        expect_match=True,
    ),
    IdentityCase(
        name="same_venue_family_siblings",
        home_left="Leeds United",
        away_left="Leicester City",
        home_right="Leeds",
        away_right="Leicester",
        competition_left="Premier League",
        competition_right="Premier League",
        kickoff_offset=timedelta(minutes=2),
        expect_match=True,
        sibling=True,
    ),
)


def _canonical_source_venue(layout: LayoutVenue) -> VenueName:
    try:
        return VenueName(layout.value)
    except ValueError:
        return VenueName.SMARKETS


def _event(
    venue: LayoutVenue | VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
    sport: str = "football",
) -> VenueEvent:
    layout = LayoutVenue(str(venue)) if not isinstance(venue, LayoutVenue) else venue
    source_venue = _canonical_source_venue(layout)
    canonical = CanonicalEvent(
        sport=sport,
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=source_venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=layout,  # type: ignore[arg-type]
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _pair_key(left: VenueEvent, right: VenueEvent) -> frozenset[tuple[str, str]]:
    return frozenset(
        (
            (str(left.venue.value), left.source_event_id),
            (str(right.venue.value), right.source_event_id),
        )
    )


def _event_key(item: VenueEvent) -> tuple[str, str]:
    return (str(item.venue.value), item.source_event_id)


def exhaustive_true_match_keys(
    items: list[VenueEvent],
    matcher: EventMatcher,
) -> set[frozenset[tuple[str, str]]]:
    matched: set[frozenset[tuple[str, str]]] = set()
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            result = matcher.match(left.canonical, right.canonical)
            if result.matched:
                assert matcher.could_match(left.canonical, right.canonical) is True
                matched.add(_pair_key(left, right))
    return matched


def indexed_candidate_keys(
    items: list[VenueEvent],
    *,
    kickoff_tolerance: timedelta = KICKOFF_WINDOW,
    cache: Any | None = None,
) -> tuple[set[frozenset[tuple[str, str]]], dict[str, int]]:
    candidates, diagnostics = build_indexed_candidates(
        items, kickoff_tolerance=kickoff_tolerance, cache=cache
    )
    return {_pair_key(left, right) for left, right in candidates}, diagnostics


def assert_indexed_retains_exhaustive(
    items: list[VenueEvent],
    matcher: EventMatcher,
) -> set[frozenset[tuple[str, str]]]:
    exhaustive = exhaustive_true_match_keys(items, matcher)
    indexed, diagnostics = indexed_candidate_keys(
        items, kickoff_tolerance=matcher.kickoff_tolerance
    )
    missing = exhaustive - indexed
    assert not missing, (
        f"indexed path dropped exhaustive matches {missing}; "
        f"diagnostics={diagnostics}"
    )
    assert diagnostics["candidate_pairs_generated"] == len(indexed)
    return exhaustive


def _partition_production(items: list[VenueEvent]) -> tuple[
    list[VenueEvent], list[VenueEvent], list[VenueEvent]
]:
    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    for item in items:
        value = item.venue.value
        if value == VenueName.MATCHBOOK.value:
            matchbook.append(
                VenueEvent(
                    venue=VenueName.MATCHBOOK,
                    raw=item.raw,
                    canonical=item.canonical,
                    source_event_id=item.source_event_id,
                )
            )
        elif value == VenueName.POLYMARKET.value:
            polymarket.append(
                VenueEvent(
                    venue=VenueName.POLYMARKET,
                    raw=item.raw,
                    canonical=item.canonical,
                    source_event_id=item.source_event_id,
                )
            )
        elif value == VenueName.KALSHI.value:
            kalshi.append(
                VenueEvent(
                    venue=VenueName.KALSHI,
                    raw=item.raw,
                    canonical=item.canonical,
                    source_event_id=item.source_event_id,
                )
            )
    return matchbook, polymarket, kalshi


def _union_groups(
    items: list[VenueEvent],
    matched_keys: set[frozenset[tuple[str, str]]],
) -> dict[tuple[str, str], set[tuple[str, str]]]:
    parent = {_event_key(item): _event_key(item) for item in items}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for pair in matched_keys:
        left, right = tuple(pair)
        union(left, right)
    groups: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for item in items:
        key = _event_key(item)
        groups.setdefault(find(key), set()).add(key)
    return groups


@pytest.mark.parametrize("case", IDENTITY_CASES, ids=lambda case: case.name)
def test_identity_case_matches_current_event_matcher_semantics(case: IdentityCase) -> None:
    matcher = EventMatcher()
    assert matcher.threshold == DEFAULT_EVENT_MATCH_THRESHOLD
    left = _event(
        LayoutVenue.MATCHBOOK,
        f"mb-{case.name}",
        home=case.home_left,
        away=case.away_left,
        competition=case.competition_left,
    )
    right_venue = LayoutVenue.POLYMARKET if not case.sibling else LayoutVenue.MATCHBOOK
    right = _event(
        right_venue,
        f"{'pm' if not case.sibling else 'mb-sib'}-{case.name}",
        home=case.home_right,
        away=case.away_right,
        competition=case.competition_right,
        kickoff=KICKOFF + case.kickoff_offset,
    )
    result = matcher.match(left.canonical, right.canonical)
    assert result.matched is case.expect_match
    if case.expect_match:
        assert matcher.could_match(left.canonical, right.canonical) is True
        assert result.confidence >= matcher.threshold
    if case.name == "known_competition_conflict":
        assert known_target_competition_mismatch(
            case.competition_left, case.competition_right
        )
        assert result.reasons == ["competition_mismatch"]
    if case.name in {"youth_incompatible", "women_incompatible", "reserve_incompatible"}:
        assert "participant_squad_category_mismatch" in result.reasons
    if case.name == "kickoff_outside_five_minutes":
        assert result.reasons == ["kickoff_outside_tolerance"]


def test_kickoff_bucket_boundary_pair_is_indexed_and_matched() -> None:
    base = datetime(2026, 9, 21, 15, 4, 59, tzinfo=UTC)
    left = _event(
        LayoutVenue.POLYMARKET,
        "pm-boundary",
        home="Leeds United",
        away="Leicester City",
        kickoff=base,
    )
    right = _event(
        LayoutVenue.KALSHI,
        "k-boundary",
        home="Leeds United",
        away="Leicester City",
        kickoff=base + KICKOFF_WINDOW,
    )
    matcher = EventMatcher(kickoff_tolerance=KICKOFF_WINDOW)
    assert matcher.match(left.canonical, right.canonical).matched is True
    indexed, _diag = indexed_candidate_keys([left, right])
    assert _pair_key(left, right) in indexed


@pytest.mark.parametrize("venue_count", (2, 3, 4, 5))
@pytest.mark.parametrize("seed", (1, 2, 7, 13, 21))
def test_indexed_path_retains_every_exhaustive_true_match(
    venue_count: int, seed: int
) -> None:
    matcher = EventMatcher()
    paper = EventMatcher(threshold=PAPER_EVENT_MATCH_THRESHOLD)
    items = _synthetic_universe(venue_count=venue_count, seed=seed)
    default_hits = assert_indexed_retains_exhaustive(items, matcher)
    paper_hits = assert_indexed_retains_exhaustive(items, paper)
    assert default_hits <= paper_hits
    assert len(items) <= 48


@pytest.mark.parametrize("venue_count", (2, 3))
def test_indexed_clustering_unions_match_exhaustive_for_production_venues(
    venue_count: int,
) -> None:
    matcher = EventMatcher()
    items = _synthetic_universe(venue_count=venue_count, seed=3, sibling=True)
    exhaustive = exhaustive_true_match_keys(items, matcher)
    indexed, _diag = indexed_candidate_keys(items)
    assert exhaustive <= indexed
    matchbook, polymarket, kalshi = _partition_production(items)
    clusters, _counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=64,
    )
    clustered: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for cluster in clusters:
        members = {
            (item.venue.value, item.source_event_id)
            for item in (
                *cluster.matchbook_events,
                *cluster.polymarket_events,
                *cluster.kalshi_events,
            )
        }
        for member in members:
            clustered[member] = members
    expected_groups = _union_groups(items, exhaustive)
    expected_lookup: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for group in expected_groups.values():
        for member in group:
            expected_lookup[member] = group
    for item in items:
        key = _event_key(item)
        if key[0] not in {venue.value for venue in PRODUCTION_CLUSTER_VENUES}:
            continue
        assert clustered[key] == expected_lookup[key]


def test_same_venue_siblings_remain_indexed_and_clusterable() -> None:
    matcher = EventMatcher()
    game = _event(
        VenueName.POLYMARKET,
        "pm-game",
        home="Leeds United",
        away="Leicester City",
    )
    btts = _event(
        VenueName.POLYMARKET,
        "pm-btts",
        home="Leeds",
        away="Leicester",
        kickoff=KICKOFF + timedelta(minutes=2),
    )
    totals = _event(
        VenueName.KALSHI,
        "k-totals",
        home="Leeds United",
        away="Leicester City",
        kickoff=KICKOFF + timedelta(minutes=3),
    )
    items = [game, btts, totals]
    exhaustive = assert_indexed_retains_exhaustive(items, matcher)
    assert _pair_key(game, btts) in exhaustive
    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=[
            VenueEvent(
                venue=VenueName.POLYMARKET,
                raw=game.raw,
                canonical=game.canonical,
                source_event_id=game.source_event_id,
            ),
            VenueEvent(
                venue=VenueName.POLYMARKET,
                raw=btts.raw,
                canonical=btts.canonical,
                source_event_id=btts.source_event_id,
            ),
        ],
        kalshi=[
            VenueEvent(
                venue=VenueName.KALSHI,
                raw=totals.raw,
                canonical=totals.canonical,
                source_event_id=totals.source_event_id,
            )
        ],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert {item.source_event_id for item in clusters[0].polymarket_events} == {
        "pm-game",
        "pm-btts",
    }
    assert {item.source_event_id for item in clusters[0].kalshi_events} == {"k-totals"}
    assert counts["polymarket_kalshi"] == 1


def test_unknown_competition_pairs_are_not_dropped_by_the_index() -> None:
    left = _event(
        LayoutVenue.MATCHBOOK,
        "mb-unknown",
        home="Arsenal",
        away="Chelsea",
        competition="Regional Cup Alpha",
    )
    right = _event(
        LayoutVenue.KALSHI,
        "k-unknown",
        home="Arsenal FC",
        away="Chelsea FC",
        competition="Regional Cup Alpha",
    )
    matcher = EventMatcher()
    assert matcher.match(left.canonical, right.canonical).matched is True
    indexed, _diag = indexed_candidate_keys([left, right])
    assert _pair_key(left, right) in indexed


def test_hard_vetoes_stay_fail_closed_in_both_paths() -> None:
    matcher = EventMatcher()
    items = [
        _event(LayoutVenue.MATCHBOOK, "mb-pl", home="Arsenal", away="Chelsea", competition="Premier League"),
        _event(LayoutVenue.KALSHI, "k-ll", home="Arsenal", away="Chelsea", competition="La Liga"),
        _event(LayoutVenue.POLYMARKET, "pm-u21", home="Arsenal U21", away="Chelsea", competition="Premier League"),
        _event(
            LayoutVenue.SMARKETS,
            "sm-women",
            home="Chelsea Women",
            away="Arsenal",
            competition="Premier League",
        ),
    ]
    exhaustive = exhaustive_true_match_keys(items, matcher)
    indexed, _diag = indexed_candidate_keys(items)
    assert exhaustive <= indexed
    assert exhaustive == set()


def test_cluster_pass_consider_unions_future_provider_pairs_without_finalize_lists() -> None:
    """Indexed matching stays venue-agnostic; FixtureCluster lists stay 3-venue.

    A fifth synthetic venue must still be a candidate and union in ClusterPass
    parent pointers. Production finalize() only materialises Matchbook /
    Polymarket / Kalshi members — that bound is not widened here.
    """

    matcher = EventMatcher()
    matchbook = [
        VenueEvent(
            venue=VenueName.MATCHBOOK,
            raw={"id": "mb-1"},
            canonical=CanonicalEvent(
                competition="Premier League",
                home_team="Arsenal",
                away_team="Chelsea",
                kickoff_utc=KICKOFF,
                source_venue=VenueName.MATCHBOOK,
                source_event_id="mb-1",
            ),
            source_event_id="mb-1",
        )
    ]
    future = _event(
        LayoutVenue.BETFAIR,
        "bf-1",
        home="Arsenal FC",
        away="Chelsea FC",
    )
    stuffed = [
        VenueEvent(
            venue=future.venue,  # type: ignore[arg-type]
            raw=future.raw,
            canonical=future.canonical,
            source_event_id=future.source_event_id,
        )
    ]
    items = [*matchbook, *stuffed]
    exhaustive = assert_indexed_retains_exhaustive(items, matcher)
    assert exhaustive
    cluster_pass = ClusterPass(
        matchbook=[*matchbook, *stuffed],
        polymarket=[],
        kalshi=[],
        matcher=matcher,
        max_event_pairs=8,
    )
    for left, right in cluster_pass.pairs():
        cluster_pass.consider(left, right)
    mb_key = (VenueName.MATCHBOOK, "mb-1")
    bf_key = (future.venue, "bf-1")
    assert cluster_pass._find(mb_key) == cluster_pass._find(bf_key)


def _synthetic_universe(
    *,
    venue_count: int,
    seed: int,
    sibling: bool = False,
) -> list[VenueEvent]:
    rng = Random(seed)
    venues = LAYOUTS[venue_count]
    items: list[VenueEvent] = []
    true_matches = 4 if venue_count <= 3 else 3
    for index, case in enumerate(IDENTITY_CASES[:true_matches]):
        kickoff = KICKOFF + timedelta(minutes=15 * index)
        for venue_index, venue in enumerate(venues):
            home = case.home_left if venue_index == 0 else case.home_right
            away = case.away_left if venue_index == 0 else case.away_right
            competition = (
                case.competition_left if venue_index == 0 else case.competition_right
            )
            offset = case.kickoff_offset if venue_index else timedelta(0)
            items.append(
                _event(
                    venue,
                    f"{venue.value}-tm-{index}",
                    home=home,
                    away=away,
                    competition=competition,
                    kickoff=kickoff + offset,
                )
            )
            if sibling and venue in {
                LayoutVenue.POLYMARKET,
                LayoutVenue.KALSHI,
            }:
                items.append(
                    _event(
                        venue,
                        f"{venue.value}-tm-{index}-btts",
                        home=home,
                        away=away,
                        competition=competition,
                        kickoff=kickoff + offset + timedelta(minutes=1),
                    )
                )
    distractors = 6 + venue_count
    clubs = (
        ("Liverpool", "Everton", "Premier League"),
        ("Bayern Munich", "Dortmund", "Bundesliga"),
        ("Inter", "Napoli", "Serie A"),
        ("PSG", "Lyon", "Ligue 1"),
        ("Ajax", "Feyenoord", "Eredivisie"),
        ("Benfica", "Porto", "Primeira Liga"),
        ("Atlanta United", "Inter Miami", "MLS"),
        ("Club America", "Guadalajara", "Liga MX"),
    )
    for index in range(distractors):
        venue = venues[index % len(venues)]
        home, away, competition = clubs[index % len(clubs)]
        day = index // len(venues)
        items.append(
            _event(
                venue,
                f"{venue.value}-d-{index}",
                home=f"{home} {seed}-{index}",
                away=f"{away} {seed}-{index}",
                competition=competition if rng.random() > 0.15 else "Mystery Cup",
                kickoff=KICKOFF + timedelta(days=day + 2, hours=index % 8),
            )
        )
    boundary_base = datetime(2026, 9, 21, 15, 4, 59, tzinfo=UTC)
    items.append(
        _event(
            venues[0],
            f"{venues[0].value}-boundary-a",
            home="Leeds United",
            away="Leicester City",
            kickoff=boundary_base,
        )
    )
    items.append(
        _event(
            venues[-1],
            f"{venues[-1].value}-boundary-b",
            home="Leeds United",
            away="Leicester City",
            kickoff=boundary_base + KICKOFF_WINDOW,
        )
    )
    return items
