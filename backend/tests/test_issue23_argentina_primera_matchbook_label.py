"""Issue #23: Matchbook country-prefixed Argentine top-flight competition label.

Evidence (not live quotes):

- `docs/fixture-identity/01_COVERAGE_ALIAS_CENSUS.md` and
  `provider_observed_labels.json` (read-only GET, 2026-09-21): Matchbook
  competition ``Argentina Liga Profesional de Fútbol`` already listed as
  argentina_primera match events. Examples are Primera clubs (Aldosivi,
  Atlético Tucumán, Barracas Central, Independiente Rivadavia, Lanús,
  Estudiantes de La Plata).
- UNIVERSE matching report generation 2, 2026-10-09 17:43:34 UTC: 13 raw
  Matchbook events with that exact label rejected at competition_scope as
  ``unknown_or_ambiguous_competition`` while ``argentina_primera`` was
  selected. ``Argentina Primera B Metropolitana`` (2 events) is a different
  competition.

This file uses deterministic fixture/demo payloads. It does not call
providers, place orders, or enable execution. Market eligibility /
settlement / pricing remain a later stage.

The change is one exact alias. PAPER EventMatcher stays 0.80; class default
stays 0.92. Identity-match counts below are only for this local sample with
shared kickoffs — not a claim that all 13 live UNIVERSE fixtures matched.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.target_competitions import (
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    TARGET_COMPETITIONS,
    UNKNOWN_COMPETITION,
    filter_in_scope_events,
    resolve_target_competition,
    scope_matchbook_event,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher, paper_event_matcher
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import MatchbookNormalizer, PolymarketNormalizer

OBSERVED_MATCHBOOK_LABEL = "Argentina Liga Profesional de Fútbol"
KICKOFF = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
SELECTED = ("argentina_primera",)

# 13 representative Matchbook events sharing the observed competition label.
# Pairings 1-4 follow Issue #23 examples; 5-7 follow the 2026-09-21 census;
# 8-13 complete a 13-event qualification sample from curated Primera clubs.
# Kickoffs are synthetic and identical across paired venues so identity
# scoring is isolated from unknown live clocks.
MATCHBOOK_SAMPLE: tuple[tuple[str, str], ...] = (
    ("mb-01", "Racing Club vs Belgrano"),
    ("mb-02", "Tigre vs Banfield"),
    ("mb-03", "Instituto de Córdoba vs Boca Juniors"),
    ("mb-04", "Rosario Central vs Independiente Rivadavia"),
    ("mb-05", "Aldosivi vs Atlético Tucumán"),
    ("mb-06", "Barracas Central vs Independiente Rivadavia"),
    ("mb-07", "Lanus vs Estudiantes de La Plata"),
    ("mb-08", "River Plate vs San Lorenzo"),
    ("mb-09", "Vélez Sarsfield vs Huracán"),
    ("mb-10", "Newell's Old Boys vs Platense"),
    ("mb-11", "Talleres vs Unión"),
    ("mb-12", "Gimnasia y Esgrima de La Plata vs Defensa y Justicia"),
    ("mb-13", "Argentinos Juniors vs Sarmiento"),
)

POLYMARKET_OVERLAP: tuple[tuple[str, str, str], ...] = (
    ("mb-01", "pm-01", "Racing Club vs. CA Belgrano"),
    ("mb-02", "pm-02", "Tigre vs. Banfield"),
    ("mb-03", "pm-03", "Instituto vs. Boca Juniors"),
    ("mb-04", "pm-04", "Rosario Central vs. Independiente Rivadavia"),
    ("mb-05", "pm-05", "Aldosivi vs. Atlético Tucumán"),
    ("mb-06", "pm-06", "Barracas Central vs. Independiente Rivadavia"),
    ("mb-07", "pm-07", "Lanús vs. Estudiantes de La Plata"),
)

NEGATIVE_LABELS = (
    "Argentina Primera B Metropolitana",
    "Argentina Primera Nacional",
    "Copa Argentina",
    "Argentina Copa de la Liga",
    "Argentina Reserva",
    "Argentina Liga Profesional Femenina",
    "Primera B Nacional",
    "Argentina",
)


def _matchbook_event(event_id: str, name: str, *, competition: str = OBSERVED_MATCHBOOK_LABEL) -> dict:
    return {
        "id": event_id,
        "name": name,
        "start": "2026-10-10T00:00:00.000Z",
        "status": "open",
        "sport-id": 15,
        "meta-tags": [
            {"id": 15, "name": "Soccer", "type": "SPORT", "url-name": "soccer"},
            {
                "id": 1190793990860999,
                "name": competition,
                "type": "COMPETITION",
                "url-name": "argentina-liga-profesional-de-futbol",
            },
        ],
    }


def _polymarket_event(event_id: str, title: str) -> dict:
    return {
        "id": event_id,
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "startTime": "2026-10-10T00:00:00Z",
        "sport": {"sport": "arg", "series": "10312"},
        "series": [{"id": "10312", "title": "Primera División Argentina"}],
    }


def test_paper_mode_execution_and_identity_thresholds_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_event_match_threshold == 0.80
    assert EventMatcher().threshold == 0.92
    assert paper_event_matcher(settings).threshold == 0.80
    assert EventMatcher().kickoff_tolerance.total_seconds() == 300
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert OPERATOR_COMPETITION_REGISTRY_VERSION == 9
    assert PRINCIPAL_OPERATOR_COMPETITION_COUNT == 37
    assert len(TARGET_COMPETITIONS) == 37


def test_observed_matchbook_label_is_exact_argentina_primera_alias() -> None:
    observed = resolve_target_competition(OBSERVED_MATCHBOOK_LABEL)
    assert observed is not None
    assert observed.code.value == "argentina_primera"
    assert resolve_target_competition("Liga Profesional") is observed
    assert resolve_target_competition("Liga Profesional de Fútbol") is observed
    assert resolve_target_competition("Primera División Argentina") is observed
    assert resolve_target_competition("liga profesional 2026") is observed
    assert normalize_text(OBSERVED_MATCHBOOK_LABEL) == "argentina liga profesional de futbol"
    assert resolve_target_competition("Argentina Liga Profesional") is None
    assert resolve_target_competition("Argentina Liga Profesional de Futbol Femenino") is None


def test_competition_alias_index_stays_unique_after_country_prefix() -> None:
    by_alias: dict[str, set[str]] = {}
    for item in TARGET_COMPETITIONS:
        for alias in (item.display_name, item.code.value, *item.aliases):
            key = normalize_text(alias)
            by_alias.setdefault(key, set()).add(item.code.value)
    collisions = {alias: sorted(codes) for alias, codes in by_alias.items() if len(codes) > 1}
    assert collisions == {}
    assert by_alias[normalize_text(OBSERVED_MATCHBOOK_LABEL)] == {"argentina_primera"}


def test_scope_admits_observed_label_only_when_argentina_primera_selected() -> None:
    payload = _matchbook_event("mb-01", "Racing Club vs Belgrano")
    default = scope_matchbook_event(payload)
    assert default.allowed is False
    assert default.reason == "out_of_scope_competition"
    selected = scope_matchbook_event(payload, selected_codes=SELECTED)
    assert selected.allowed is True
    assert selected.competition is not None
    assert selected.competition.code.value == "argentina_primera"
    assert selected.label == OBSERVED_MATCHBOOK_LABEL
    unselected = scope_matchbook_event(payload, selected_codes=["premier_league"])
    assert unselected.allowed is False
    assert unselected.reason == "out_of_scope_competition"


def test_lower_division_cup_and_ambiguous_labels_remain_rejected() -> None:
    for label in NEGATIVE_LABELS:
        assert resolve_target_competition(label) is None
        decision = scope_matchbook_event(
            _matchbook_event("neg", "Home vs Away", competition=label),
            selected_codes=SELECTED,
        )
        assert decision.allowed is False
        assert decision.reason == UNKNOWN_COMPETITION


def test_thirteen_observed_labels_qualify_and_neighbors_stay_out() -> None:
    primera = [_matchbook_event(event_id, name) for event_id, name in MATCHBOOK_SAMPLE]
    neighbors = [
        _matchbook_event("b1", "Tristán Suárez vs Atlanta", competition="Argentina Primera Nacional"),
        _matchbook_event(
            "b2",
            "Colegiales vs Deportivo Armenio",
            competition="Argentina Primera B Metropolitana",
        ),
        _matchbook_event(
            "b3",
            "Almirante Brown vs Talleres Remedios",
            competition="Argentina Primera B Metropolitana",
        ),
        _matchbook_event("cup", "Boca Juniors vs Independiente", competition="Copa Argentina"),
    ]
    batch = [*primera, *neighbors]
    after = filter_in_scope_events(batch, venue=VenueName.MATCHBOOK, selected_codes=SELECTED)
    assert len(after.allowed) == 13
    assert after.skipped == 4
    assert after.skipped_by_reason == {UNKNOWN_COMPETITION: 4}
    allowed_ids = {str(event["id"]) for event in after.allowed}
    assert allowed_ids == {event_id for event_id, _name in MATCHBOOK_SAMPLE}

    # Baseline on the same batch if the country-prefixed label were still
    # unknown: every Primera row would join the four neighbors as unknown.
    unknown_label_batch = [
        _matchbook_event(event_id, name, competition="Argentina Liga Profesional")
        for event_id, name in MATCHBOOK_SAMPLE
    ] + neighbors
    baseline = filter_in_scope_events(
        unknown_label_batch,
        venue=VenueName.MATCHBOOK,
        selected_codes=SELECTED,
    )
    assert baseline.allowed == []
    assert baseline.skipped == 17
    assert baseline.skipped_by_reason == {UNKNOWN_COMPETITION: 17}


def test_qualified_events_normalise_and_some_identities_match_at_paper_080() -> None:
    mb_norm = MatchbookNormalizer()
    pm_norm = PolymarketNormalizer()
    settings = Settings()
    paper = paper_event_matcher(settings)
    conservative = EventMatcher()
    assert paper.threshold == 0.80
    assert conservative.threshold == 0.92

    mb_by_id = {
        event_id: mb_norm.normalize_event(_matchbook_event(event_id, name))
        for event_id, name in MATCHBOOK_SAMPLE
    }
    for event in mb_by_id.values():
        assert event.competition == OBSERVED_MATCHBOOK_LABEL
        assert event.kickoff_utc == KICKOFF
        assert resolve_target_competition(event.competition) is not None
        assert resolve_target_competition(event.competition).code.value == "argentina_primera"

    paper_matches = 0
    conservative_matches = 0
    unmatched: list[str] = []
    for mb_id, pm_id, pm_title in POLYMARKET_OVERLAP:
        mb_event = mb_by_id[mb_id]
        pm_event = pm_norm.normalize_event(_polymarket_event(pm_id, pm_title))
        assert pm_event.kickoff_utc == mb_event.kickoff_utc
        assert resolve_target_competition(pm_event.competition).code.value == "argentina_primera"
        paper_result = paper.match(mb_event, pm_event)
        conservative_result = conservative.match(mb_event, pm_event)
        if paper_result.matched:
            paper_matches += 1
        else:
            unmatched.append(f"{mb_event.home_team} vs {mb_event.away_team} -> {pm_title}")
        if conservative_result.matched:
            conservative_matches += 1
        if mb_id == "mb-03":
            assert paper_result.matched is True
            assert conservative_result.matched is False
            assert paper_result.confidence == conservative_result.confidence
            assert paper_result.confidence < 0.92
            assert "home_team_fuzzy" in paper_result.reasons

    # Qualification is 13/13. Identity matching is scored only for the seven
    # overlapping titles with identical synthetic kickoffs. Do not treat this
    # as 13 live UNIVERSE matches: remaining MB rows have no PM pair here, and
    # live kickoff parity was not captured in-repo.
    #
    # PAPER 0.80 admits all seven overlapping pairs. Class default 0.92 rejects
    # Instituto de Córdoba vs Instituto (0.8672, home_team_fuzzy). Team aliases
    # are out of scope for this competition-label patch.
    assert paper_matches == 7
    assert conservative_matches == 6
    assert unmatched == []
    unpaired = [
        event_id
        for event_id, _name in MATCHBOOK_SAMPLE
        if event_id not in {row[0] for row in POLYMARKET_OVERLAP}
    ]
    assert unpaired == ["mb-08", "mb-09", "mb-10", "mb-11", "mb-12", "mb-13"]


def test_kickoff_outside_five_minutes_and_wrong_competition_still_fail() -> None:
    mb = MatchbookNormalizer().normalize_event(_matchbook_event("mb-02", "Tigre vs Banfield"))
    pm_payload = _polymarket_event("pm-02", "Tigre vs. Banfield")
    pm_payload["startTime"] = "2026-10-10T00:06:00Z"
    pm_late = PolymarketNormalizer().normalize_event(pm_payload)
    paper = paper_event_matcher(Settings())
    late = paper.match(mb, pm_late)
    assert late.matched is False
    assert "kickoff_outside_tolerance" in late.reasons

    pm_cup = PolymarketNormalizer().normalize_event(
        {
            "id": "pm-cup",
            "title": "Tigre vs. Banfield",
            "slug": "tigre-vs-banfield-copa",
            "startTime": "2026-10-10T00:00:00Z",
            "series": [{"id": "99999", "title": "Copa Argentina"}],
        }
    )
    # Unresolved Copa Argentina is not a known-code mismatch; team/kickoff
    # similarity can still score. A registered different code must veto.
    pm_epl = PolymarketNormalizer().normalize_event(
        {
            "id": "pm-epl",
            "title": "Tigre vs. Banfield",
            "slug": "tigre-vs-banfield-epl",
            "startTime": "2026-10-10T00:00:00Z",
            "sport": {"sport": "epl", "series": "10188"},
            "series": [{"id": "10188", "title": "Premier League"}],
        }
    )
    mismatch = paper.match(mb, pm_epl)
    assert mismatch.matched is False
    assert "competition_mismatch" in mismatch.reasons
    assert resolve_target_competition(pm_cup.competition) is None
