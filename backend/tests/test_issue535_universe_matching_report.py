"""#535 UNIVERSE matching review report.

Data class: synthetic/fixture identity evidence. Not live quotes.
The report is diagnostic only. EventMatcher thresholds, aliases, graph
assignment and provider calls are unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from sports_hedge.application.fixture_clusters import VenueEvent, cluster_member_keyset
from sports_hedge.application.universe_identity_shards import cluster_events_sharded
from sports_hedge.application.universe_matching_report import (
    SCHEMA_VERSION,
    TOP_NEARBY_CANDIDATES,
    build_universe_matching_report,
    new_matching_review_slot,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher, EventMatchResult
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

KICKOFF = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
TRAIL_KEYS = (
    "provider_discovery",
    "competition_scope",
    "normalization",
    "normalized_identity",
    "candidate_pairs",
    "scored_candidates",
    "graph_assignment",
    "final_venue_combination",
)


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
    title: str | None = None,
) -> VenueEvent:
    canonical = CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": title or f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


class ScriptedIdentityMatcher:
    def __init__(self, scores: dict[frozenset[str], float], *, threshold: float = 0.80) -> None:
        self.threshold = threshold
        self.kickoff_tolerance = timedelta(minutes=5)
        self._scores = scores

    def bulk_snapshot(self) -> ScriptedIdentityMatcher:
        return self

    def could_match(self, left: CanonicalEvent, right: CanonicalEvent) -> bool:
        return True

    def match(self, left: CanonicalEvent, right: CanonicalEvent) -> EventMatchResult:
        confidence = self._scores.get(frozenset({left.source_event_id, right.source_event_id}))
        if confidence is None:
            return EventMatchResult(matched=False, confidence=0.0, reasons=["no_scripted_edge"])
        return EventMatchResult(
            matched=confidence >= self.threshold,
            confidence=round(float(confidence), 6),
            reasons=["home_team_fuzzy"],
        )


def _meta(*, complete: bool = True, generation_id: int = 3) -> dict:
    return {
        "generated_at": "2026-09-23T12:00:00+00:00",
        "universe_generation_id": generation_id,
        "completeness": "complete" if complete else "deadline_leftover",
        "partial": not complete,
        "clustering_truncated": not complete,
        "selected_competition_codes": ["premier_league"],
        "selected_season_scope_codes": [],
        "enabled_venues": ["matchbook", "polymarket", "kalshi"],
        "venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        "source_event_counts": {
            "raw_by_venue": {"matchbook": 1, "polymarket": 1, "kalshi": 1},
            "normalized_by_venue": {"matchbook": 1, "polymarket": 1, "kalshi": 1},
            "skipped_out_of_scope": 0,
            "skipped_by_reason": {},
        },
    }


async def _report_from_events(
    matchbook: list[VenueEvent],
    polymarket: list[VenueEvent],
    kalshi: list[VenueEvent],
    matcher: object,
    *,
    meta: dict | None = None,
) -> tuple[dict, list]:
    slot = new_matching_review_slot()
    clusters, _counts, _truncated, _diagnostics, _unscored = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,  # type: ignore[arg-type]
        max_event_pairs=10_000,
        review_slot=slot,
    )
    slot["meta"] = meta or _meta()
    return build_universe_matching_report(slot), clusters


def _find_member(report: dict, source_event_id: str) -> tuple[dict, dict]:
    for fixture in report["fixtures"]:
        for member in fixture["members"]:
            if member["source_event_id"] == source_event_id:
                return fixture, member
    raise AssertionError(source_event_id)


def _assert_trail(member: dict) -> None:
    trail = member["stage_trail"]
    assert tuple(trail) == TRAIL_KEYS
    for key in TRAIL_KEYS:
        assert "evidence" in trail[key]


def test_report_build_performs_no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider HTTP is not part of report generation")

    monkeypatch.setattr(MatchbookClient, "list_events", boom)
    monkeypatch.setattr(PolymarketClient, "list_events", boom)
    monkeypatch.setattr(KalshiClient, "list_events", boom)
    report = build_universe_matching_report(
        {
            "identity_evidence": "retained",
            "matcher_threshold": 0.8,
            "nodes": [],
            "clusters": [],
            "scored_pairs": [],
            "generated_pairs": [],
            "scope_rejections": [],
            "normalization_rejections": [],
            "meta": _meta(),
        }
    )
    assert report["scanner_decisions_depend_on_report"] is False
    assert report["schema"] == SCHEMA_VERSION


async def test_review_capture_does_not_change_assignment() -> None:
    matcher = EventMatcher(threshold=0.80)
    matchbook = [_event(VenueName.MATCHBOOK, "mb", home="Leeds United", away="Leicester City")]
    polymarket = [
        _event(
            VenueName.POLYMARKET,
            "pm",
            home="Leeds United",
            away="Leicester City",
            title="Leeds United vs. Leicester City",
        )
    ]
    first, _, _, _, _ = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=[],
        matcher=matcher,
        max_event_pairs=8,
    )
    slot = new_matching_review_slot()
    second, _, _, _, _ = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=[],
        matcher=matcher,
        max_event_pairs=8,
        review_slot=slot,
    )
    assert {cluster_member_keyset(item) for item in first} == {
        cluster_member_keyset(item) for item in second
    }
    assert slot["identity_evidence"] == "retained"
    assert slot["scored_pairs"]


async def test_alias_miss_retains_rejected_candidate_score() -> None:
    matcher = ScriptedIdentityMatcher({frozenset({"mb-che", "pm-che"}): 0.55})
    report, clusters = await _report_from_events(
        [_event(VenueName.MATCHBOOK, "mb-che", home="Chelsea", away="Arsenal", title="Chelsea v Arsenal")],
        [
            _event(
                VenueName.POLYMARKET,
                "pm-che",
                home="Chelsea FC",
                away="Arsenal",
                title="Chelsea FC vs. Arsenal",
            )
        ],
        [],
        matcher,
    )
    assert all(cluster.venue_count == 1 for cluster in clusters)
    fixture, member = _find_member(report, "mb-che")
    _assert_trail(member)
    assert member["provider_label"] == "Chelsea v Arsenal"
    assert member["stage_trail"]["normalized_identity"]["home_team"] == "Chelsea"
    missing = {item["venue"]: item for item in fixture["missing_venues"]}
    polymarket = missing["polymarket"]
    assert polymarket["stage"] == "candidate_scored_and_rejected"
    top = polymarket["top_nearby_candidates"]
    assert top
    assert top[0]["source_event_id"] == "pm-che"
    assert top[0]["confidence"] == 0.55
    assert top[0]["confidence_evidence"] == "retained"
    assert top[0]["threshold"] == 0.8
    assert top[0]["kickoff_delta_seconds"] == 0.0
    assert top[0]["kickoff_delta_evidence"] == "retained"
    assert "home_team_fuzzy" in top[0]["reasons"]
    assert member["raw_home_evidence"] == "unavailable"


async def test_correct_match_keeps_raw_labels_and_provenance() -> None:
    matcher = EventMatcher(threshold=0.80)
    report, clusters = await _report_from_events(
        [
            _event(
                VenueName.MATCHBOOK,
                "mb-and",
                home="Andorra",
                away="Malta",
                title="Andorra v Malta",
            )
        ],
        [
            _event(
                VenueName.POLYMARKET,
                "unl-and-mal-2026-09-24",
                home="Andorra",
                away="Malta",
                title="Andorra vs. Malta",
            )
        ],
        [],
        matcher,
    )
    assert len(clusters) == 1
    assert clusters[0].venue_count == 2
    fixture, member = _find_member(report, "mb-and")
    _assert_trail(member)
    assert fixture["matched"] is True
    assert fixture["venue_combination"] == "matchbook+polymarket"
    assert member["provider_label"] == "Andorra v Malta"
    other = next(item for item in fixture["members"] if item["source_event_id"] != "mb-and")
    assert other["provider_label"] == "Andorra vs. Malta"
    assert member["stage_trail"]["normalized_identity"]["home_team"] == "Andorra"
    assert member["stage_trail"]["normalized_identity"]["away_team"] == "Malta"
    graph = member["stage_trail"]["graph_assignment"]
    assert graph["evidence"] == "retained"
    assert graph["decision"] == "matched"
    assert graph["chosen_edges"]
    assert fixture["event_match_confidence"] is not None
    assert fixture["event_match_threshold"] == 0.80


async def test_exact_names_outside_kickoff_window_are_unscored() -> None:
    matcher = EventMatcher(threshold=0.80)
    report, clusters = await _report_from_events(
        [_event(VenueName.MATCHBOOK, "mb-and", home="Andorra", away="Malta", title="Andorra v Malta")],
        [
            _event(
                VenueName.POLYMARKET,
                "unl-and-mal-2026-09-24",
                home="Andorra",
                away="Malta",
                title="Andorra vs. Malta",
                kickoff=KICKOFF + timedelta(hours=6),
            )
        ],
        [],
        matcher,
    )
    assert all(cluster.venue_count == 1 for cluster in clusters)
    fixture, _member = _find_member(report, "mb-and")
    missing = {item["venue"]: item for item in fixture["missing_venues"]}
    assert missing["polymarket"]["stage"] == "no_candidate_pair_generated"
    nearby = missing["polymarket"]["top_nearby_candidates"]
    assert nearby
    assert nearby[0]["source_event_id"] == "unl-and-mal-2026-09-24"
    assert nearby[0]["confidence"] is None
    assert nearby[0]["confidence_evidence"] == "not_scored_by_production"
    assert nearby[0]["kickoff_delta_evidence"] == "retained"
    assert nearby[0]["kickoff_delta_seconds"] == 6 * 3600


async def test_ambiguous_component_exposes_fail_closed_provenance() -> None:
    matcher = ScriptedIdentityMatcher(
        {
            frozenset({"mb", "pm"}): 0.95,
            frozenset({"pm", "k"}): 0.93,
            frozenset({"mb", "k"}): 0.0,
        }
    )

    def match(self: ScriptedIdentityMatcher, left: CanonicalEvent, right: CanonicalEvent) -> EventMatchResult:
        key = frozenset({left.source_event_id, right.source_event_id})
        if key == frozenset({"mb", "k"}):
            return EventMatchResult(
                matched=False, confidence=0.0, reasons=["curated_team_mismatch"]
            )
        confidence = self._scores[key]
        return EventMatchResult(
            matched=confidence >= self.threshold,
            confidence=confidence,
            reasons=["scripted_pairwise_evidence"],
        )

    matcher.match = match.__get__(matcher, ScriptedIdentityMatcher)  # type: ignore[method-assign]
    report, clusters = await _report_from_events(
        [_event(VenueName.MATCHBOOK, "mb", home="Alpha FC", away="Beta FC")],
        [_event(VenueName.POLYMARKET, "pm", home="Alpha FC", away="Beta FC")],
        [_event(VenueName.KALSHI, "k", home="Alpha FC", away="Beta FC")],
        matcher,
    )
    assert all(cluster.venue_count == 1 for cluster in clusters)
    _fixture, member = _find_member(report, "mb")
    graph = member["stage_trail"]["graph_assignment"]
    assert graph["evidence"] == "retained"
    assert graph["decision"] == "fail_closed"
    assert graph["fail_closed_reason"] == "contradictory_component"
    assert graph["rejected_competing_edges"]
    missing = {item["venue"]: item for item in _fixture["missing_venues"]}
    assert missing["polymarket"]["stage"] == "graph_assignment_fail_closed"


async def test_owner_live_label_pairs_are_self_describing() -> None:
    matcher = EventMatcher(threshold=0.80)
    cases = [
        ("mb-kr", "pm-kr", "fif-kr-ecu-2026-09-24", "South Korea", "Ecuador", "Korea Republic", "Ecuador", "Korea Republic vs. Ecuador"),
        ("mb-cn", "pm-cn", "fif-chn-mdv-2026-09-24", "China", "Maldives", "China PR", "Maldives", "China PR vs. Maldives"),
        ("mb-uz", "pm-uz", "fif-uzb-irn-2026-09-24", "Uzbekistan", "Iran", "Uzbekistan", "IR Iran", "Uzbekistan vs. IR Iran"),
    ]
    matchbook = []
    polymarket = []
    for mb_id, _pm_key, pm_id, mb_home, mb_away, pm_home, pm_away, title in cases:
        kickoff = KICKOFF + timedelta(days=len(matchbook))
        matchbook.append(
            _event(
                VenueName.MATCHBOOK,
                mb_id,
                home=mb_home,
                away=mb_away,
                title=f"{mb_home} v {mb_away}",
                kickoff=kickoff,
            )
        )
        polymarket.append(
            _event(
                VenueName.POLYMARKET,
                pm_id,
                home=pm_home,
                away=pm_away,
                title=title,
                kickoff=kickoff,
            )
        )
    report, _clusters = await _report_from_events(matchbook, polymarket, [], matcher)
    for mb_id, _pm_key, pm_id, mb_home, mb_away, pm_home, pm_away, title in cases:
        fixture, member = _find_member(report, mb_id)
        _assert_trail(member)
        assert member["provider_label"] == f"{mb_home} v {mb_away}"
        assert member["stage_trail"]["normalized_identity"]["home_team"] == mb_home
        assert member["stage_trail"]["provider_discovery"]["status"] == "returned"
        _pm_fixture, pm_member = _find_member(report, pm_id)
        assert pm_member["provider_label"] == title
        assert pm_member["stage_trail"]["normalized_identity"]["home_team"] == pm_home
        assert pm_member["stage_trail"]["normalized_identity"]["away_team"] == pm_away
        production = matcher.match(
            next(item.canonical for item in matchbook if item.source_event_id == mb_id),
            next(item.canonical for item in polymarket if item.source_event_id == pm_id),
        )
        if fixture["matched"]:
            assert production.matched is True
            assert fixture["event_match_confidence"] is not None
        else:
            missing = {item["venue"]: item for item in fixture["missing_venues"]}["polymarket"]
            assert missing["stage"] in {
                "candidate_scored_and_rejected",
                "no_candidate_pair_generated",
                "graph_assignment_fail_closed",
                "eligible_assigned_elsewhere",
                "candidate_generated_not_scored",
            }
            if missing["top_nearby_candidates"] and missing["top_nearby_candidates"][0]["confidence"] is not None:
                assert missing["top_nearby_candidates"][0]["confidence"] == pytest.approx(
                    production.confidence
                )


def test_scope_rejection_and_discovery_absence_are_distinct() -> None:
    from sports_hedge.application.target_competitions import filter_in_scope_events

    scoped = filter_in_scope_events(
        [
            {
                "id": "mb-out",
                "name": "Chelsea v Arsenal",
                "sport-name": "Soccer",
                "competition-name": "Not A Real League",
            },
        ],
        venue=VenueName.MATCHBOOK,
        selected_codes=["premier_league"],
    )
    assert scoped.rejected_events
    assert scoped.rejected_events[0]["scope_reason"]
    evidence = {
        "identity_evidence": "retained",
        "matcher_threshold": 0.8,
        "matcher_semantic_version": "identity-cache/test",
        "nodes": [
            {
                "venue": "matchbook",
                "source_event_id": "mb-1",
                "shard_id": "football/premier_league",
                "shard_loaded": True,
                "candidate_generation_evidence": "retained",
                "candidate_generation_reason": None,
                "provider_label": "South Korea v Ecuador",
                "provider_label_evidence": "retained",
                "raw_home": None,
                "raw_home_evidence": "unavailable",
                "raw_away": None,
                "raw_away_evidence": "unavailable",
                "sport": "football",
                "competition": "World Cup",
                "home_team": "South Korea",
                "away_team": "Ecuador",
                "kickoff_utc": KICKOFF.isoformat(),
                "normalization_evidence": "retained",
            }
        ],
        "clusters": [
            {
                "canonical_event_id": "mb-1",
                "member_keys": [["matchbook", "mb-1"]],
                "venues": ["matchbook"],
                "pair_kinds": [],
                "event_match_confidence": None,
                "event_match_threshold": 0.8,
                "provenance": None,
                "provenance_evidence": "unavailable",
                "shard_id": "football/premier_league",
            }
        ],
        "scored_pairs": [],
        "generated_pairs": [],
        "scope_rejections": scoped.rejected_events,
        "normalization_rejections": [
            {
                "venue": "kalshi",
                "source_event_id": "k-bad",
                "provider_label": "Broken",
                "provider_label_evidence": "retained",
                "reason": "normalization failed",
            }
        ],
        "meta": {
            **_meta(),
            "source_event_counts": {
                "raw_by_venue": {"matchbook": 2, "polymarket": 0, "kalshi": 1},
                "normalized_by_venue": {"matchbook": 1, "polymarket": 0, "kalshi": 0},
                "skipped_out_of_scope": scoped.skipped,
                "skipped_by_reason": scoped.skipped_by_reason,
            },
        },
    }
    report = build_universe_matching_report(evidence)
    fixture, member = _find_member(report, "mb-1")
    _assert_trail(member)
    assert member["stage_trail"]["graph_assignment"]["evidence"] == "unavailable"
    assert member["stage_trail"]["graph_assignment"]["reason"] == "production_did_not_retain_provenance"
    missing = {item["venue"]: item for item in fixture["missing_venues"]}
    assert missing["polymarket"]["stage"] == "provider_discovery_not_returned"
    assert missing["kalshi"]["stage"] == "normalization_rejected"
    assert report["scope_rejections"]
    scope_trail = report["scope_rejections"][0]["stage_trail"]
    assert scope_trail["competition_scope"]["status"] == "rejected"
    assert scope_trail["competition_scope"]["evidence"] == "retained"
    assert scope_trail["scored_candidates"]["evidence"] == "not_applicable"
    assert report["normalization_rejections"][0]["stage_trail"]["normalization"]["status"] == "rejected"
    assert report["meta"]["generation_state"] == "complete"
    partial = build_universe_matching_report({**evidence, "meta": {**evidence["meta"], **_meta(complete=False)}})
    assert partial["meta"]["generation_state"] == "partial"
    assert partial["meta"]["completeness"] == "deadline_leftover"


def test_report_size_is_bounded_for_a_large_synthetic_universe() -> None:
    nodes = []
    clusters = []
    scored = []
    kickoff = KICKOFF.isoformat()
    for index in range(40):
        for venue in ("matchbook", "polymarket"):
            source_id = f"{venue}-{index}"
            nodes.append(
                {
                    "venue": venue,
                    "source_event_id": source_id,
                    "shard_id": "football/premier_league",
                    "shard_loaded": True,
                    "candidate_generation_evidence": "retained",
                    "candidate_generation_reason": None,
                    "provider_label": f"Home {index} vs Away {index}",
                    "provider_label_evidence": "retained",
                    "raw_home": None,
                    "raw_home_evidence": "unavailable",
                    "raw_away": None,
                    "raw_away_evidence": "unavailable",
                    "sport": "football",
                    "competition": "Premier League",
                    "home_team": f"Home {index}",
                    "away_team": f"Away {index}",
                    "kickoff_utc": kickoff,
                    "normalization_evidence": "retained",
                }
            )
            clusters.append(
                {
                    "canonical_event_id": source_id,
                    "member_keys": [[venue, source_id]],
                    "venues": [venue],
                    "pair_kinds": [],
                    "event_match_confidence": None,
                    "event_match_threshold": 0.8,
                    "provenance": None,
                    "provenance_evidence": "unavailable",
                    "shard_id": "football/premier_league",
                }
            )
        for other in range(40):
            scored.append(
                {
                    "left_venue": "matchbook",
                    "left_source_event_id": f"matchbook-{index}",
                    "right_venue": "polymarket",
                    "right_source_event_id": f"polymarket-{other}",
                    "confidence": round(0.2 + (other % 5) * 0.01, 6),
                    "reasons": ["home_team_fuzzy"],
                    "matched": False,
                    "veto": False,
                }
            )
    report = build_universe_matching_report(
        {
            "identity_evidence": "retained",
            "matcher_threshold": 0.8,
            "nodes": nodes,
            "clusters": clusters,
            "scored_pairs": scored,
            "generated_pairs": [],
            "meta": _meta(),
        }
    )
    encoded = json.dumps(report, sort_keys=True)
    assert len(encoded) < 2_000_000
    for fixture in report["fixtures"]:
        for member in fixture["members"]:
            nearby = member["stage_trail"]["scored_candidates"]["top_nearby_candidates"]
            assert len(nearby) <= TOP_NEARBY_CANDIDATES
        for missing in fixture["missing_venues"]:
            assert len(missing["top_nearby_candidates"]) <= TOP_NEARBY_CANDIDATES
            if missing["venue"] in {"matchbook", "polymarket"} and fixture["members"][0]["venue"] != missing["venue"]:
                assert missing["omitted_candidate_count"] > 0


def test_download_endpoint_uses_retained_evidence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from sports_hedge.api.main import app
    from sports_hedge.application.live_refresh import get_live_refresh_coordinator

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider HTTP is not part of report download")

    monkeypatch.setattr(MatchbookClient, "list_events", boom)
    monkeypatch.setattr(PolymarketClient, "list_events", boom)
    monkeypatch.setattr(KalshiClient, "list_events", boom)
    coordinator = get_live_refresh_coordinator()
    previous = coordinator._universe_matching_evidence
    coordinator._universe_matching_evidence = {
        "identity_evidence": "unavailable",
        "identity_evidence_reason": "clustering_not_finished",
        "matcher_threshold": 0.8,
        "nodes": [],
        "clusters": [],
        "scored_pairs": [],
        "generated_pairs": [],
        "meta": _meta(generation_id=11),
    }
    try:
        client = TestClient(app)
        empty = client.get("/operations/universe-matching-report")
        assert empty.status_code == 200
        body = empty.json()
        assert body["meta"]["universe_generation_id"] == 11
        assert body["meta"]["identity_evidence"] == "unavailable"
        assert body["diagnostic_only"] is True
        assert "attachment" in empty.headers["content-disposition"]
        coordinator._universe_matching_evidence = None
        missing = client.get("/operations/universe-matching-report")
        assert missing.status_code == 404
    finally:
        coordinator._universe_matching_evidence = previous


def test_repeated_builds_are_deterministic() -> None:
    evidence = {
        "identity_evidence": "retained",
        "matcher_threshold": 0.8,
        "nodes": [],
        "clusters": [],
        "scored_pairs": [],
        "generated_pairs": [],
        "meta": _meta(),
    }
    assert build_universe_matching_report(evidence) == build_universe_matching_report(evidence)
