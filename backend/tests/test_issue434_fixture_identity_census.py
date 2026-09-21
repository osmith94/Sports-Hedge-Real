"""Issue #434: unique-alias registries composing after #415.

Evidence: docs/fixture-identity/01_COVERAGE_ALIAS_CENSUS.md and
backend/tests/fixtures/fixture_identity_census/provider_observed_labels.json
(live read-only GET, 2026-09-21). This file uses deterministic fixture/demo
providers, not live quotes.

Unique aliases collapse. Ambiguous city tokens stay fail-closed unless
#415's competition-scoped generic_aliases has provider evidence. Paris,
Sporting, and Istanbul are not globalised and are not added as generics.
PAPER injects EventMatcher 0.80; the class default stays 0.92. Known
target-competition mismatches hard-veto at PAPER 0.80. No scanner/capture/
economics/settlement or provider-concurrency change. PAPER execution stays
disabled.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from sports_hedge.application.hot_identity import (
    HOT_KICKOFF_TOLERANCE,
    same_hot_scheduling_unit,
    scheduling_team_key,
)
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name, resolve_team_name_for_competition
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.facts.team_registry import (
    ARGENTINA_PRIMERA,
    BELGIAN_PRO_LEAGUE,
    BRASILEIRAO,
    CHAMPIONS_LEAGUE,
    COPA_DEL_REY,
    COPA_LIBERTADORES,
    COPPA_ITALIA,
    DFB_POKAL,
    EREDIVISIE,
    J1_LEAGUE,
    LEAGUE_ONE,
    LIGA_MX,
    LIGUE_1,
    MLS,
    PRIMEIRA_LIGA,
    SAUDI_PRO_LEAGUE,
    SCOTTISH_PREMIERSHIP,
    SUPER_LIG,
    alias_pairs,
    clubs_for,
    generic_aliases_for,
)
from sports_hedge.matching.events import EventMatcher, paper_event_matcher
from sports_hedge.normalization.text import normalize_text

KICKOFF = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


def _canonical(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str,
    source_event_id: str,
    kickoff: datetime = KICKOFF,
) -> CanonicalEvent:
    return CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )


def _raw_ratio(left: str, right: str) -> float:
    return SequenceMatcher(a=normalize_text(left), b=normalize_text(right)).ratio()


def test_paper_boundary_and_identity_thresholds_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance.total_seconds() == 300
    assert HOT_KICKOFF_TOLERANCE.total_seconds() == 300
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert settings.paper_event_match_threshold == 0.80
    paper = paper_event_matcher(settings)
    assert paper.threshold == 0.80
    assert paper.kickoff_tolerance.total_seconds() == 300


def test_unique_aliases_are_globally_unambiguous() -> None:
    by_alias: dict[str, set[str]] = defaultdict(set)
    for alias, canonical in alias_pairs():
        by_alias[normalize_text(alias)].add(normalize_text(canonical))
    collisions = {alias: sorted(names) for alias, names in by_alias.items() if len(names) > 1}
    assert collisions == {}


def test_new_domestic_registries_cover_current_senior_universes() -> None:
    assert len(clubs_for(SUPER_LIG)) == 18
    assert len(clubs_for(PRIMEIRA_LIGA)) == 18
    assert len(clubs_for(LIGUE_1)) == 18
    assert len(clubs_for(EREDIVISIE)) == 18
    assert len(clubs_for(SCOTTISH_PREMIERSHIP)) == 12
    assert len(clubs_for(BELGIAN_PRO_LEAGUE)) == 18
    assert len(clubs_for(LEAGUE_ONE)) == 24
    assert len(clubs_for(BRASILEIRAO)) == 20
    assert len(clubs_for(ARGENTINA_PRIMERA)) == 30
    assert len(clubs_for(SAUDI_PRO_LEAGUE)) == 18
    assert len(clubs_for(J1_LEAGUE)) >= 20
    assert {club.canonical_name for club in clubs_for(SUPER_LIG)} >= {
        "Galatasaray",
        "Fenerbahce",
        "Goztepe",
        "Corum",
    }
    assert {club.canonical_name for club in clubs_for(PRIMEIRA_LIGA)} >= {
        "Porto",
        "Benfica",
        "Sporting CP",
        "Nacional Madeira",
    }


def test_cups_reuse_domestic_tables_without_duplicating_club_identity() -> None:
    assert clubs_for(COPA_DEL_REY) == clubs_for("la_liga")
    assert clubs_for(DFB_POKAL) == clubs_for("bundesliga")
    assert clubs_for(COPPA_ITALIA) == clubs_for("serie_a")
    ucl_names = {club.canonical_name for club in clubs_for(CHAMPIONS_LEAGUE)}
    assert "Galatasaray" in ucl_names
    assert "Sporting CP" in ucl_names
    assert "Paris Saint-Germain" in ucl_names
    assert "Inter Miami" not in ucl_names
    libertadores = {club.canonical_name for club in clubs_for(COPA_LIBERTADORES)}
    assert "Flamengo" in libertadores
    assert "Boca Juniors" in libertadores
    assert "Barcelona" not in libertadores


def test_super_lig_provider_aliases_collapse() -> None:
    assert _raw_ratio("Goztepe Izmir", "Goztepe") < 0.92
    assert resolve_team_name("Goztepe Izmir") == resolve_team_name("Göztepe") == "goztepe"
    assert resolve_team_name("Caykur Rizespor") == resolve_team_name("Rizespor") == "rizespor"
    assert resolve_team_name("Rams Başakşehir FK") == "istanbul basaksehir"
    assert resolve_team_name("Kocaeli") == "kocaelispor"
    assert canonical_team_id("Goztepe Izmir") == canonical_team_id("Göztepe A.Ş.")
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Göztepe",
            "Çaykur Rizespor",
            competition="Turkish Süper Lig",
            source_event_id="mb-goz-riz",
        ),
        _canonical(
            VenueName.KALSHI,
            "Goztepe Izmir",
            "Rizespor",
            competition="Turkish Süper Lig",
            source_event_id="KXSUPERLIGGAME-26SEP20GOZRIZ",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    assert "away_team_fuzzy" not in result.reasons


def test_primeira_liga_provider_aliases_collapse() -> None:
    assert resolve_team_name("Porto") == resolve_team_name("FC Porto") == "porto"
    assert resolve_team_name("SL Benfica") == resolve_team_name("Sport Lisboa e Benfica") == "benfica"
    assert resolve_team_name("Sporting Lisbon") == "sporting cp"
    assert resolve_team_name("CD Nacional") == "nacional madeira"
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "FC Porto",
            "Sport Lisboa e Benfica",
            competition="Primeira Liga",
            source_event_id="mb-fcp-ben",
        ),
        _canonical(
            VenueName.KALSHI,
            "Porto",
            "SL Benfica",
            competition="Liga Portugal",
            source_event_id="KXLIGAPORTUGALGAME-26SEP20FCPBEN",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92


def test_ligue_1_game_and_ftts_siblings_collapse() -> None:
    assert resolve_team_name("Marseille") == resolve_team_name("Olympique Marseille") == "marseille"
    assert resolve_team_name("PSG") == resolve_team_name("Paris Saint-Germain") == "paris saint germain"
    result = EventMatcher().match(
        _canonical(
            VenueName.KALSHI,
            "Marseille",
            "PSG",
            competition="Ligue 1",
            source_event_id="KXLIGUE1GAME-26SEP20OLMPSG",
        ),
        _canonical(
            VenueName.KALSHI,
            "Olympique Marseille",
            "Paris Saint-Germain",
            competition="Ligue 1",
            source_event_id="KXLIGUE1FTTS-26SEP20OLMPSG",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    left = _canonical(
        VenueName.KALSHI,
        "Marseille",
        "PSG",
        competition="Ligue 1",
        source_event_id="game",
        kickoff=KICKOFF,
    )
    right = _canonical(
        VenueName.KALSHI,
        "Olympique Marseille",
        "Paris Saint-Germain",
        competition="Ligue 1",
        source_event_id="ftts",
        kickoff=KICKOFF + timedelta(minutes=5),
    )
    assert same_hot_scheduling_unit(left, right) is True


def test_generic_and_ambiguous_tokens_fail_closed() -> None:
    assert resolve_team_name("Istanbul") == "istanbul"
    assert resolve_team_name("Paris") == "paris"
    assert resolve_team_name("Sporting") == "sporting"
    assert resolve_team_name("Nacional") == "nacional"
    assert resolve_team_name("Brugge") == "brugge"
    assert resolve_team_name("Standard") == "standard"
    assert resolve_team_name("Union") == "union"
    assert resolve_team_name("Independiente") == "independiente"
    assert resolve_team_name("Tokyo") == "tokyo"
    assert resolve_team_name("Osaka") == "osaka"
    assert resolve_team_name("Yokohama") == "yokohama"
    assert resolve_team_name("Vitoria") == "vitoria"
    assert resolve_team_name("Sparta") == "sparta"
    assert resolve_team_name("AL Suqoor") == "al suqoor"
    assert resolve_team_name("Paris") != resolve_team_name("PSG")
    assert resolve_team_name("Sporting") != resolve_team_name("Sporting CP")
    assert resolve_team_name("Nacional") != resolve_team_name("CD Nacional")
    assert resolve_team_name("Sporting") != resolve_team_name("Sporting Kansas City")
    # #415's competition-scoped mechanism is available after composition, but
    # these tokens still lack provider evidence as unique-or-scoped aliases.
    assert resolve_team_name_for_competition("Paris", LIGUE_1) == "paris"
    assert resolve_team_name_for_competition("Sporting", PRIMEIRA_LIGA) == "sporting"
    assert resolve_team_name_for_competition("Istanbul", SUPER_LIG) == "istanbul"
    assert resolve_team_name_for_competition("Nacional", PRIMEIRA_LIGA) == "nacional"
    assert {normalize_text(alias) for alias, _canonical_name in generic_aliases_for(LIGUE_1)} == set()
    assert {normalize_text(alias) for alias, _canonical_name in generic_aliases_for(PRIMEIRA_LIGA)} == set()
    assert {normalize_text(alias) for alias, _canonical_name in generic_aliases_for(SUPER_LIG)} == set()
    assert {normalize_text(alias) for alias, _canonical_name in generic_aliases_for(CHAMPIONS_LEAGUE)} == set()


def test_issue415_competition_scoped_generics_remain_available() -> None:
    assert resolve_team_name("Miami") == "miami"
    assert resolve_team_name_for_competition("Miami", MLS) == "inter miami"
    assert resolve_team_name_for_competition("Leon", LIGA_MX) == "club leon"
    assert resolve_team_name_for_competition("Miami", CHAMPIONS_LEAGUE) == "miami"
    assert resolve_team_name_for_competition("Leon", CHAMPIONS_LEAGUE) == "leon"


def test_same_city_and_similar_name_clubs_stay_distinct() -> None:
    assert resolve_team_name("Dundee") != resolve_team_name("Dundee United")
    assert resolve_team_name("Club Brugge") != resolve_team_name("Cercle Brugge")
    assert resolve_team_name("Gent") != resolve_team_name("Genk")
    assert resolve_team_name("Paris FC") != resolve_team_name("PSG")
    assert resolve_team_name("Independiente Avellaneda") != resolve_team_name(
        "Independiente Rivadavia"
    )
    assert resolve_team_name("Estudiantes de La Plata") != resolve_team_name("Rio Cuarto")
    assert resolve_team_name("Gimnasia La Plata") != resolve_team_name("Gimnasia Mendoza")
    assert resolve_team_name("FC Tokyo") != resolve_team_name("Tokyo Verdy")
    assert resolve_team_name("Gamba Osaka") != resolve_team_name("Cerezo Osaka")
    assert resolve_team_name("Yokohama F Marinos") != resolve_team_name("Yokohama FC")
    assert resolve_team_name("Notts County") != resolve_team_name("Nottingham Forest")
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Dundee FC",
            "Celtic",
            competition="Scottish Premiership",
            source_event_id="mb-dundee",
        ),
        _canonical(
            VenueName.KALSHI,
            "Dundee United",
            "Celtic",
            competition="Scottish Premiership",
            source_event_id="kalshi-du",
        ),
    )
    assert result.matched is False
    assert "curated_team_mismatch" in result.reasons


def test_youth_women_and_reserves_fail_closed() -> None:
    assert resolve_team_name("Galatasaray U21") == "galatasaray u21"
    assert resolve_team_name("Chelsea Women") == "chelsea women"
    assert resolve_team_name("Ajax Vrouwen") == "ajax vrouwen"
    assert resolve_team_name("PAOK B") == "paok b"
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Galatasaray",
            "Fenerbahce",
            competition="Turkish Süper Lig",
            source_event_id="senior",
        ),
        _canonical(
            VenueName.KALSHI,
            "Galatasaray U21",
            "Fenerbahce",
            competition="Turkish Süper Lig",
            source_event_id="youth",
        ),
    )
    assert result.matched is False
    assert "participant_squad_category_mismatch" in result.reasons


def test_same_teams_and_kickoff_across_recognised_competitions_hard_veto_at_paper() -> None:
    left = _canonical(
        VenueName.MATCHBOOK,
        "Galatasaray",
        "Fenerbahce",
        competition="Turkish Süper Lig",
        source_event_id="super-lig",
    )
    right = _canonical(
        VenueName.KALSHI,
        "Galatasaray Istanbul",
        "Fenerbahce Istanbul",
        competition="UEFA Champions League",
        source_event_id="ucl",
    )
    paper = paper_event_matcher(Settings())
    assert paper.threshold == 0.80
    # Identical teams + kickoff with competition_score 0.0 is 0.90, which clears
    # PAPER 0.80 unless known target-code disagreement is a hard veto.
    assert round(0.35 + 0.35 + 0.10 * 0.0 + 0.20, 2) == 0.90
    result = paper.match(left, right)
    assert result.matched is False
    assert result.confidence == 0.0
    assert result.reasons == ["competition_mismatch"]
    assert paper.could_match(left, right) is False
    default = EventMatcher()
    assert default.threshold == 0.92
    assert default.match(left, right).matched is False
    assert default.could_match(left, right) is False


def test_five_minute_kickoff_tolerance_is_inclusive_and_hard() -> None:
    matcher = EventMatcher()
    inside = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Porto",
            "Benfica",
            competition="Primeira Liga",
            source_event_id="mb",
            kickoff=KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "FC Porto",
            "SL Benfica",
            competition="Primeira Liga",
            source_event_id="kalshi",
            kickoff=KICKOFF + timedelta(minutes=5),
        ),
    )
    outside = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Porto",
            "Benfica",
            competition="Primeira Liga",
            source_event_id="mb",
            kickoff=KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "FC Porto",
            "SL Benfica",
            competition="Primeira Liga",
            source_event_id="kalshi",
            kickoff=KICKOFF + timedelta(minutes=5, seconds=1),
        ),
    )
    assert inside.matched is True
    assert outside.matched is False
    assert "kickoff_outside_tolerance" in outside.reasons


def test_existing_top_league_aliases_remain_green() -> None:
    assert resolve_team_name("Man United") == "manchester united"
    assert resolve_team_name("Athletic Bilbao") == "athletic club"
    assert resolve_team_name("FC Bayern München") == "bayern munich"
    assert resolve_team_name("Inter Milan") == "inter"
    assert resolve_team_name("AC Milan") == "ac milan"
    assert scheduling_team_key("Brentford FC") == "brentford"


def test_j1_and_saudi_unique_forms_and_unknown_nickname() -> None:
    assert resolve_team_name("Vissel Kōbe") == "vissel kobe"
    assert resolve_team_name("Cerezo Ōsaka") == "cerezo osaka"
    assert resolve_team_name("Al Hilal") == "al hilal"
    assert resolve_team_name("Al Nassr Club") == "al nassr"
    assert resolve_team_name("Neom SC") == "neom"
    assert resolve_team_name("AL Suqoor") == "al suqoor"
