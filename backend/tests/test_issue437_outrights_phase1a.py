"""Outrights Phase 1A (#437): COMPETITION_SEASON identity + observation catalogue.

Exact native IDs are captured public evidence from 2026-09-20 (Phase 0A census).
Not owner-live quotes. Not modelled probabilities. Not demo soccer fixtures.
PAPER / read-only. No venue writes. No register admission.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    catalogue_row_supports_paper_eligibility,
    derived_price_engine_working_set,
    required_outcomes_for_key,
)
from sports_hedge.application.collector import DEFAULT_PROVIDER_CONCURRENCY as COLLECTOR_CONCURRENCY
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.models import VenueName
from sports_hedge.domain.outrights import (
    CanonicalCompetitionSeasonRef,
    CanonicalSeasonMarketIdentity,
    OutrightMarketFamily,
    OutrightSettlementFingerprint,
    ParticipantType,
    SeasonEquivalenceState,
)
from sports_hedge.facts.identity import canonical_match_id, canonical_team_id
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    CANONICAL_TOTAL_GOALS_FT,
    VENUE_NATIVE_ARCHETYPES,
)
from sports_hedge.matching.events import EventMatcher
from sports_hedge.outrights.catalogue import persist_season_observation, season_observation_key
from sports_hedge.outrights.equivalence import FORBIDDEN_ADMISSION_STATES, compare_season_identities
from sports_hedge.outrights.identity import (
    SeasonIdentityError,
    canonical_season_market_id,
    season_identities_equal,
    season_market_identity_from_payload,
    season_ref_from_payload,
)
from sports_hedge.outrights.native_ids import (
    MissingNativeIdError,
    extract_kalshi_listing,
    extract_matchbook_listing,
    extract_polymarket_listing,
    parse_clob_token_ids,
)
from sports_hedge.outrights.participants import (
    SeasonParticipantError,
    epl_team_participant_id,
    nfl_season_team_id,
    venue_native_player_id,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
EVIDENCE = json.loads(
    (Path(__file__).parent / "fixtures/outrights/phase1a_native_ids.json").read_text()
)
ARSENAL = EVIDENCE["targets"]["epl_champion_arsenal"]
RAMS = EVIDENCE["targets"]["super_bowl_rams"]
HAALAND = EVIDENCE["targets"]["epl_top_scorer_haaland"]


def _season_identity(
    *,
    sport: str,
    competition_code: str,
    season_id: str,
    family: OutrightMarketFamily,
    participant_type: ParticipantType,
    participant_canonical_id: str,
    fingerprint_version: str,
    horizon: str | None = None,
) -> CanonicalSeasonMarketIdentity:
    return CanonicalSeasonMarketIdentity(
        subject=CanonicalCompetitionSeasonRef(
            sport=sport,
            competition_code=competition_code,
            season_id=season_id,
        ),
        market_family=family,
        participant_type=participant_type,
        participant_canonical_id=participant_canonical_id,
        settlement_fingerprint_version=fingerprint_version,
        expected_settlement_horizon=horizon,
    )


def _epl_champion(season: str = "2026/27", club: str = "Arsenal") -> CanonicalSeasonMarketIdentity:
    return _season_identity(
        sport="football",
        competition_code="premier_league",
        season_id=season,
        family=OutrightMarketFamily.COMPETITION_WINNER,
        participant_type=ParticipantType.TEAM,
        participant_canonical_id=epl_team_participant_id(club),
        fingerprint_version="kalshi-kxpremierleague-27-2026-09-20",
        horizon="2027-06-13T21:00:00Z",
    )


def _super_bowl(team_label: str = "Los Angeles Rams") -> CanonicalSeasonMarketIdentity:
    return _season_identity(
        sport="american_football",
        competition_code="nfl",
        season_id="NFL-2026-SB-LXI",
        family=OutrightMarketFamily.COMPETITION_WINNER,
        participant_type=ParticipantType.TEAM,
        participant_canonical_id=nfl_season_team_id(team_label),
        fingerprint_version="kalshi-kxsb-27-2026-09-20",
        horizon="super_bowl_official_result",
    )


def _top_scorer(*, venue: str, native_id: str, fingerprint: str, horizon: str) -> CanonicalSeasonMarketIdentity:
    return _season_identity(
        sport="football",
        competition_code="premier_league",
        season_id="2026/27",
        family=OutrightMarketFamily.TOP_SCORER,
        participant_type=ParticipantType.PLAYER,
        participant_canonical_id=venue_native_player_id(venue=venue, native_id=native_id),
        fingerprint_version=fingerprint,
        horizon=horizon,
    )


def _fingerprint(**overrides: str) -> OutrightSettlementFingerprint:
    payload = {
        "winner_uniqueness": "exactly_one",
        "joint_winner_policy": "exactly_one_official",
        "stat_scope": "official_league_champion",
        "official_resolution_source": "premier_league",
        "exceptional_policy": "follow_official_table_including_deductions_and_curtailment",
        "rule_version": "v0",
        "completion_horizon": "official_champion_declared",
    }
    payload.update(overrides)
    return OutrightSettlementFingerprint(**payload)


def test_competition_season_ref_rejects_home_away_kickoff() -> None:
    with pytest.raises(ValidationError):
        CanonicalCompetitionSeasonRef.model_validate(
            {
                "sport": "football",
                "competition_code": "premier_league",
                "season_id": "2026/27",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "kickoff_utc": "2026-09-20T14:00:00+00:00",
            }
        )
    with pytest.raises(SeasonIdentityError, match="fixture_fields_forbidden"):
        season_ref_from_payload(
            {
                "sport": "football",
                "competition_code": "premier_league",
                "season_id": "2026/27",
                "home_team": "Arsenal",
            }
        )


def test_price_and_liquidity_fields_forbidden_on_season_identity() -> None:
    with pytest.raises(SeasonIdentityError, match="price_fields_forbidden"):
        season_market_identity_from_payload(
            {
                "sport": "football",
                "competition_code": "premier_league",
                "season_id": "2026/27",
                "market_family": "competition_winner",
                "participant_type": "TEAM",
                "participant_canonical_id": epl_team_participant_id("Arsenal"),
                "settlement_fingerprint_version": "v0",
                "odds": 3.5,
            }
        )


def test_exact_season_inequality_never_joins() -> None:
    left = _epl_champion("2025/26")
    right = _epl_champion("2026/27")
    assert not season_identities_equal(left, right)
    result = compare_season_identities(left, right)
    assert result.state is SeasonEquivalenceState.FAIL_CLOSED
    assert "season_mismatch" in result.reasons
    assert result.admitted is False
    assert result.register_status == "UNKNOWN"


def test_competition_winner_is_not_top_scorer() -> None:
    champion = _epl_champion()
    scorer = _top_scorer(
        venue="kalshi",
        native_id=HAALAND["kalshi"]["soccer_player_uuid"],
        fingerprint="kalshi-eplleader-27goal-2026-09-20",
        horizon="2027-06-08T02:00:00Z",
    )
    result = compare_season_identities(champion, scorer)
    assert result.state is SeasonEquivalenceState.FAIL_CLOSED
    assert "market_family_mismatch" in result.reasons
    assert canonical_season_market_id(champion) != canonical_season_market_id(scorer)


def test_nfl_game_fixture_is_not_super_bowl_season_winner() -> None:
    fixture = CanonicalEvent(
        sport="american_football",
        competition="NFL",
        home_team="Kansas City Chiefs",
        away_team="Philadelphia Eagles",
        kickoff_utc=datetime(2027, 2, 14, 23, 30, tzinfo=UTC),
        source_venue=VenueName.KALSHI,
        source_event_id="KXNFLGAME-27FEB14KCPHI",
    )
    season = _super_bowl("Philadelphia Eagles")
    matcher = EventMatcher()
    counterpart = CanonicalEvent(
        sport="american_football",
        competition="NFL",
        home_team="Kansas City Chiefs",
        away_team="Philadelphia Eagles",
        kickoff_utc=fixture.kickoff_utc,
        source_venue=VenueName.MATCHBOOK,
        source_event_id="nfl-fixture",
    )
    assert matcher.match(fixture, counterpart).matched is True
    with pytest.raises(SeasonIdentityError, match="fixture_scope_not_an_outright"):
        season_ref_from_payload(
            {
                "sport": fixture.sport,
                "competition_code": "nfl",
                "season_id": "NFL-2026-SB-LXI",
                "event_scope": "FIXTURE_MATCH",
            }
        )
    assert season.subject.event_scope is MarketScope.COMPETITION_SEASON
    assert season.market_family is OutrightMarketFamily.COMPETITION_WINNER


def test_team_and_player_namespaces_do_not_collide() -> None:
    team = _epl_champion()
    colliding_player = _season_identity(
        sport="football",
        competition_code="premier_league",
        season_id="2026/27",
        family=OutrightMarketFamily.TOP_SCORER,
        participant_type=ParticipantType.PLAYER,
        participant_canonical_id=team.participant_canonical_id,
        fingerprint_version="v0",
    )
    result = compare_season_identities(team, colliding_player)
    assert result.state is SeasonEquivalenceState.FAIL_CLOSED
    assert "participant_type_mismatch" in result.reasons
    assert canonical_season_market_id(team) != canonical_season_market_id(colliding_player)


def test_name_only_player_ids_fail_closed() -> None:
    with pytest.raises((ValidationError, SeasonIdentityError, SeasonParticipantError)):
        CanonicalSeasonMarketIdentity(
            subject=CanonicalCompetitionSeasonRef(
                sport="football",
                competition_code="premier_league",
                season_id="2026/27",
            ),
            market_family=OutrightMarketFamily.TOP_SCORER,
            participant_type=ParticipantType.PLAYER,
            participant_canonical_id="name:son heung min",
            settlement_fingerprint_version="v0",
        )


def test_nyg_is_not_nyj() -> None:
    giants = nfl_season_team_id("NYG")
    jets = nfl_season_team_id("NYJ")
    assert giants != jets
    with pytest.raises(SeasonParticipantError, match="generic_city_participant_veto"):
        nfl_season_team_id("New York")
    left = _super_bowl("New York Giants")
    right = _super_bowl("New York Jets")
    result = compare_season_identities(left, right)
    assert "participant_mismatch" in result.reasons


def test_missing_polymarket_clob_token_ids_fail_closed() -> None:
    with pytest.raises(MissingNativeIdError, match="missing_polymarket_clob_token_ids"):
        parse_clob_token_ids(None)
    with pytest.raises(MissingNativeIdError, match="placeholder"):
        parse_clob_token_ids(["yes-token", "no-token"])
    with pytest.raises(MissingNativeIdError):
        extract_polymarket_listing(
            {
                "event": {"id": "659518", "slug": ARSENAL["polymarket"]["slug"]},
                "market": {"id": "2771324", "clobTokenIds": []},
            }
        )
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        identity = _epl_champion()
        with pytest.raises((MissingNativeIdError, ValidationError)):
            persist_season_observation(
                store,
                identity=identity,
                listing=extract_polymarket_listing(
                    {
                        "event": {"id": "659518"},
                        "market": {"id": "2771324", "clobTokenIds": ["todo", "fake"]},
                    }
                ),
                now=NOW,
            )
    finally:
        store.close()


def test_real_polymarket_clob_tokens_persist_for_epl_champion() -> None:
    listing = extract_polymarket_listing(
        {
            "event": {
                "id": ARSENAL["polymarket"]["event_id"],
                "slug": ARSENAL["polymarket"]["slug"],
                "ticker": ARSENAL["polymarket"]["ticker"],
            },
            "market": {
                "id": ARSENAL["polymarket"]["market_id"],
                "conditionId": ARSENAL["polymarket"]["condition_id"],
                "clobTokenIds": ARSENAL["polymarket"]["clob_token_ids"],
            },
        }
    )
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        row = persist_season_observation(store, identity=_epl_champion(), listing=listing, now=NOW)
        assert row.market_scope is MarketScope.COMPETITION_SEASON
        assert row.source_venue is VenueName.POLYMARKET
        assert row.polymarket_event_id == "659518"
        assert row.polymarket_market_id == "2771324"
        assert row.polymarket_clob_token_ids == ARSENAL["polymarket"]["clob_token_ids"]
        assert row.home_canonical is None
        assert row.away_canonical is None
        assert row.kickoff_utc is None
        assert catalogue_row_supports_paper_eligibility(row, None) is False
        assert derived_price_engine_working_set([row]) == []
        assert row.register_canonical_key.startswith("OBSERVATION:")
    finally:
        store.close()


def test_exact_native_ids_persist_per_venue_without_equivalence() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        kalshi = persist_season_observation(
            store,
            identity=_epl_champion(),
            listing=extract_kalshi_listing(
                {
                    "event": {
                        "event_ticker": ARSENAL["kalshi"]["event_ticker"],
                        "series_ticker": ARSENAL["kalshi"]["series_ticker"],
                    },
                    "market": {"ticker": ARSENAL["kalshi"]["market_ticker"]},
                }
            ),
            now=NOW,
        )
        matchbook = persist_season_observation(
            store,
            identity=_epl_champion(),
            listing=extract_matchbook_listing(
                {
                    "event": {"id": ARSENAL["matchbook"]["event_id"]},
                    "market": {"id": ARSENAL["matchbook"]["market_id"], "market-type": "outright"},
                    "runner": {
                        "id": ARSENAL["matchbook"]["runner_id"],
                        "event-participant-id": ARSENAL["matchbook"]["event_participant_id"],
                    },
                }
            ),
            now=NOW,
        )
        assert kalshi.canonical_event_id == matchbook.canonical_event_id
        assert kalshi.register_canonical_key != matchbook.register_canonical_key
        comparison = compare_season_identities(_epl_champion(), _epl_champion())
        assert comparison.state is SeasonEquivalenceState.OBSERVATION_ONLY
        assert comparison.register_status == "UNKNOWN"
        assert comparison.admitted is False
        assert comparison.state.value not in FORBIDDEN_ADMISSION_STATES
        assert not any(state in comparison.reasons for state in FORBIDDEN_ADMISSION_STATES)
    finally:
        store.close()


def test_top_scorer_stays_observation_only_across_venues() -> None:
    kalshi_id = _top_scorer(
        venue="kalshi",
        native_id=HAALAND["kalshi"]["soccer_player_uuid"],
        fingerprint="kalshi-eplleader-27goal-2026-09-20",
        horizon="2027-06-08T02:00:00Z",
    )
    polymarket_id = _top_scorer(
        venue="polymarket",
        native_id=HAALAND["polymarket"]["market_id"],
        fingerprint="polymarket-871869-2026-09-20",
        horizon="2027-06-14T23:59:00-04:00",
    )
    result = compare_season_identities(
        kalshi_id,
        polymarket_id,
        left_settlement=_fingerprint(
            winner_uniqueness="possibly_many",
            joint_winner_policy="dead_heat_equal_share",
            stat_scope="premier_league_goals_only",
            official_resolution_source="official_league_statistics_then_dead_heat",
            exceptional_policy="season_ends_early_closes_early",
            rule_version="kalshi-eplleader-27goal-2026-09-20",
            completion_horizon="2027-06-08T02:00:00Z",
        ),
        right_settlement=_fingerprint(
            winner_uniqueness="exactly_one",
            joint_winner_policy="sole_winner_alpha_tiebreak",
            stat_scope="premier_league_goals_only",
            official_resolution_source="premier_league_stats_plus_alpha_tiebreak",
            exceptional_policy="cancel_or_incomplete_by_2027-06-14_resolves_other",
            rule_version="polymarket-871869-2026-09-20",
            completion_horizon="2027-06-14T23:59:00-04:00",
        ),
    )
    assert result.state is SeasonEquivalenceState.FAIL_CLOSED
    assert "top_scorer_alpha_tiebreak_vs_shared_or_dead_heat" in result.reasons
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        row = persist_season_observation(
            store,
            identity=polymarket_id,
            listing=extract_polymarket_listing(
                {
                    "event": {
                        "id": HAALAND["polymarket"]["event_id"],
                        "slug": HAALAND["polymarket"]["slug"],
                        "ticker": HAALAND["polymarket"]["ticker"],
                    },
                    "market": {
                        "id": HAALAND["polymarket"]["market_id"],
                        "conditionId": HAALAND["polymarket"]["condition_id"],
                        "clobTokenIds": HAALAND["polymarket"]["clob_token_ids"],
                    },
                }
            ),
            now=NOW,
        )
        assert row.family == "top_scorer"
        assert derived_price_engine_working_set([row]) == []
    finally:
        store.close()


def test_super_bowl_observation_persists_exact_ids() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        row = persist_season_observation(
            store,
            identity=_super_bowl(),
            listing=extract_kalshi_listing(
                {
                    "event": {
                        "event_ticker": RAMS["kalshi"]["event_ticker"],
                        "series_ticker": RAMS["kalshi"]["series_ticker"],
                    },
                    "market": {"ticker": RAMS["kalshi"]["market_ticker"]},
                }
            ),
            now=NOW,
        )
        assert row.season_id == "NFL-2026-SB-LXI"
        assert row.kalshi_event_ticker == "KXSB-27"
        assert row.kalshi_market_tickers == ["KXSB-27-LAR"]
        conference = _season_identity(
            sport="american_football",
            competition_code="nfl",
            season_id="NFL-2026-SB-LXI",
            family=OutrightMarketFamily.CONFERENCE_WINNER,
            participant_type=ParticipantType.TEAM,
            participant_canonical_id=nfl_season_team_id("LAR"),
            fingerprint_version="v0",
        )
        assert "market_family_mismatch" in compare_season_identities(
            _super_bowl(), conference
        ).reasons
    finally:
        store.close()


def test_fixture_event_matcher_behaviour_unchanged() -> None:
    matcher = EventMatcher()
    left = CanonicalEvent(
        sport="football",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        source_venue=VenueName.MATCHBOOK,
        source_event_id="mb-1",
    )
    inside = left.model_copy(
        update={
            "source_venue": VenueName.KALSHI,
            "source_event_id": "ks-1",
            "kickoff_utc": left.kickoff_utc + timedelta(minutes=4),
        }
    )
    outside = left.model_copy(
        update={
            "source_venue": VenueName.KALSHI,
            "source_event_id": "ks-2",
            "kickoff_utc": left.kickoff_utc + timedelta(minutes=6),
        }
    )
    assert matcher.match(left, inside).matched is True
    outside_match = matcher.match(left, outside)
    assert outside_match.matched is False
    assert outside_match.reasons == ["kickoff_outside_tolerance"]


def test_canonical_match_id_fixture_hashes_unchanged() -> None:
    match_id = canonical_match_id(
        competition_code="premier_league",
        season="2025/26",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=datetime(2025, 8, 16, 17, 30, tzinfo=UTC),
    )
    assert match_id.startswith("match:")
    assert match_id == canonical_match_id(
        competition_code="premier_league",
        season="2025/26",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=datetime(2025, 8, 16, 17, 30, tzinfo=UTC),
    )
    season_id = canonical_season_market_id(_epl_champion())
    assert season_id.startswith("seasonmkt:")
    assert season_id != match_id


def test_approved_match_register_is_not_extended() -> None:
    keys = {item.canonical_key for item in VENUE_NATIVE_ARCHETYPES}
    assert keys == {
        CANONICAL_MATCH_RESULT_FT,
        CANONICAL_BTTS_FT,
        CANONICAL_TOTAL_GOALS_FT,
        CANONICAL_FTTS_FT,
    }
    assert season_observation_key(
        market_family="competition_winner", venue=VenueName.KALSHI
    ) not in keys


def test_provider_concurrency_unchanged() -> None:
    expected = {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }
    assert DEFAULT_PROVIDER_CONCURRENCY == expected
    assert COLLECTOR_CONCURRENCY == expected


def test_fixture_catalogue_rows_still_default_to_fixture_match() -> None:
    row = ApprovedMarketCatalogueRow(
        catalogue_row_id="amc-fixture",
        register_canonical_key=CANONICAL_MATCH_RESULT_FT,
        canonical_event_id="evt-fixture",
        competition="Premier League",
        home_canonical="Arsenal",
        away_canonical="Chelsea",
        kickoff_utc=NOW,
        matchbook_event_id="1",
        matchbook_market_id="2",
        matchbook_runner_ids=[
            OutcomeNativeId(outcome=outcome, native_id=f"mb-{outcome}")
            for outcome in required_outcomes_for_key(CANONICAL_MATCH_RESULT_FT)
        ],
        kalshi_event_ticker="KXEPLGAME-X",
        kalshi_market_tickers=["KXEPLGAME-X-GAME"],
        family="match_result",
        period="full_time",
        required_outcomes=required_outcomes_for_key(CANONICAL_MATCH_RESULT_FT),
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=NOW,
    )
    assert row.market_scope is MarketScope.FIXTURE_MATCH
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        stored = store.upsert_catalogue_row(row)
        assert stored.market_scope is MarketScope.FIXTURE_MATCH
        working = derived_price_engine_working_set([stored])
        assert len(working) == 1
        assert working[0].catalogue_row_id == "amc-fixture"
    finally:
        store.close()


def test_season_row_cannot_reuse_fixture_fields() -> None:
    with pytest.raises(ValidationError, match="fixture_fields_forbidden"):
        ApprovedMarketCatalogueRow(
            catalogue_row_id="amc-bad",
            register_canonical_key="OBSERVATION:competition_winner:kalshi",
            canonical_event_id="seasonmkt:bad",
            home_canonical="Arsenal",
            away_canonical="Chelsea",
            kickoff_utc=NOW,
            first_catalogued_at=NOW,
            market_scope=MarketScope.COMPETITION_SEASON,
            source_venue=VenueName.KALSHI,
            season_id="2026/27",
            competition_code="premier_league",
            participant_type="TEAM",
            participant_canonical_id=canonical_team_id("Arsenal"),
            settlement_fingerprint_version="v0",
        )


def test_epl_team_ids_reuse_football_registry() -> None:
    assert epl_team_participant_id("Arsenal") == canonical_team_id("Arsenal")
    assert epl_team_participant_id("Arsenal") == epl_team_participant_id("arsenal")
