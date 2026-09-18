from __future__ import annotations

from datetime import UTC, datetime

from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import LearnedMappingApplicator
from sports_hedge.normalization.identity import (
    canonical_matched_event_id,
    canonical_source_event_id,
)


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str = "Leeds United",
    away: str = "Leicester City",
    kickoff: datetime = KICKOFF,
) -> VenueEvent:
    canonical = CanonicalEvent(
        competition="Premier League",
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def test_same_fixture_split_events_form_one_cluster() -> None:
    """GAME/BTTS/TOTAL/FTTS siblings with realistic minute-offset kickoffs.

    Live Kalshi sibling events often differ by 1–4 minutes. Exact curated
    seniors in the same target competition must still form one cluster.
    """

    game_kickoff = KICKOFF
    polymarket = [
        _event(VenueName.POLYMARKET, "pm-moneyline", kickoff=game_kickoff),
        _event(
            VenueName.POLYMARKET,
            "pm-btts",
            kickoff=game_kickoff.replace(minute=game_kickoff.minute + 1),
        ),
        _event(
            VenueName.POLYMARKET,
            "pm-totals",
            kickoff=game_kickoff.replace(minute=game_kickoff.minute + 3),
        ),
    ]
    kalshi = [
        _event(VenueName.KALSHI, "k-game", kickoff=game_kickoff),
        _event(
            VenueName.KALSHI,
            "k-btts",
            kickoff=game_kickoff.replace(minute=game_kickoff.minute + 1),
        ),
        _event(
            VenueName.KALSHI,
            "k-totals",
            kickoff=game_kickoff.replace(minute=game_kickoff.minute + 3),
        ),
        _event(
            VenueName.KALSHI,
            "k-ftts",
            kickoff=game_kickoff.replace(minute=game_kickoff.minute + 4),
        ),
    ]
    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=25,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert len(cluster.polymarket_events) == 3
    assert len(cluster.kalshi_events) == 4
    assert cluster.matchbook is None
    assert counts["polymarket_kalshi"] == 1
    assert counts["matchbook_polymarket"] == 0


def test_greedy_one_to_one_does_not_split_same_fixture_across_clusters() -> None:
    """A PM moneyline event must not consume the only Kalshi GAME slot as a
    separate fixture from PM BTTS / Kalshi BTTS."""

    polymarket = [
        _event(VenueName.POLYMARKET, "pm-moneyline"),
        _event(VenueName.POLYMARKET, "pm-btts"),
    ]
    kalshi = [
        _event(VenueName.KALSHI, "k-game"),
        _event(VenueName.KALSHI, "k-btts"),
    ]
    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=1,
    )
    assert counts["polymarket_kalshi"] == 1
    assert len(clusters) == 1
    assert {item.source_event_id for item in clusters[0].polymarket_events} == {
        "pm-moneyline",
        "pm-btts",
    }
    assert {item.source_event_id for item in clusters[0].kalshi_events} == {"k-game", "k-btts"}


def test_different_fixtures_stay_separate() -> None:
    polymarket = [
        _event(VenueName.POLYMARKET, "pm-leeds", home="Leeds United", away="Leicester City"),
        _event(VenueName.POLYMARKET, "pm-ncl", home="Newcastle United", away="Chelsea"),
    ]
    kalshi = [
        _event(VenueName.KALSHI, "k-leeds", home="Leeds United", away="Leicester City"),
        _event(VenueName.KALSHI, "k-ncl", home="Newcastle United", away="Chelsea"),
    ]
    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=25,
    )
    assert len(clusters) == 2
    assert counts["polymarket_kalshi"] == 2


def test_bulk_match_prefilter_never_rejects_a_matching_pair() -> None:
    matcher = EventMatcher()
    exact = _event(VenueName.MATCHBOOK, "mb-exact").canonical
    offset = _event(
        VenueName.POLYMARKET,
        "pm-offset",
        kickoff=KICKOFF.replace(minute=KICKOFF.minute + 1),
    ).canonical
    fuzzy = _event(
        VenueName.POLYMARKET,
        "pm-fuzzy",
        home="Leeds Utd",
        away="Leicester",
    ).canonical

    for counterpart in (offset, fuzzy):
        assert matcher.match(exact, counterpart).matched is True
        assert matcher.could_match(exact, counterpart) is True

    unrelated = _event(
        VenueName.POLYMARKET,
        "pm-unrelated",
        home="Newcastle United",
        away="Chelsea",
    ).canonical
    assert matcher.match(exact, unrelated).matched is False
    assert matcher.could_match(exact, unrelated) is False


def test_exact_curated_siblings_match_inside_declared_kickoff_window() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance.total_seconds() == 300
    exact = _event(VenueName.MATCHBOOK, "mb-game").canonical
    offset = _event(
        VenueName.KALSHI,
        "k-total",
        kickoff=KICKOFF.replace(minute=KICKOFF.minute + 3),
    ).canonical
    result = matcher.match(exact, offset)
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "kickoff_offset" in result.reasons
    assert matcher.could_match(exact, offset) is True


def test_bulk_clustering_snapshots_enabled_rules_once() -> None:
    class RuleStore:
        def __init__(self) -> None:
            self.calls = 0

        def list_enabled(self) -> list:
            self.calls += 1
            return []

    store = RuleStore()
    matcher = EventMatcher(learned_applicator=LearnedMappingApplicator(store))
    polymarket = [
        _event(VenueName.POLYMARKET, "pm-leeds"),
        _event(
            VenueName.POLYMARKET,
            "pm-newcastle",
            home="Newcastle United",
            away="Chelsea",
        ),
    ]
    kalshi = [
        _event(VenueName.KALSHI, "k-leeds"),
        _event(
            VenueName.KALSHI,
            "k-newcastle",
            home="Newcastle United",
            away="Chelsea",
        ),
    ]

    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=25,
    )

    assert len(clusters) == 2
    assert counts["polymarket_kalshi"] == 2
    assert store.calls == 1


def test_three_venue_pair_decision_id_differs_from_cluster_id() -> None:
    matchbook = [_event(VenueName.MATCHBOOK, "mb-leeds")]
    polymarket = [_event(VenueName.POLYMARKET, "pm-leeds")]
    kalshi = [_event(VenueName.KALSHI, "k-leeds")]
    clusters, _counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=25,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    cluster_id = cluster_canonical_event_id(cluster)
    pair_id = canonical_matched_event_id(
        [matchbook[0].canonical, polymarket[0].canonical]
    )
    assert cluster_id == canonical_source_event_id(matchbook[0].canonical)
    assert pair_id != cluster_id
    aliases = cluster_identity_aliases(cluster)
    assert aliases[pair_id] == cluster_id
    assert aliases[canonical_matched_event_id([matchbook[0].canonical, kalshi[0].canonical])] == cluster_id
    assert aliases[canonical_matched_event_id([polymarket[0].canonical, kalshi[0].canonical])] == cluster_id
    assert aliases["mb-leeds"] == cluster_id
    assert "Leeds United" not in aliases

