from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.mapping_review import (
    MappingReviewService,
    evidence_from_markets,
    evidence_from_snapshots,
)
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthScanResult
from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.learned_rules import (
    MappingProvenance,
    MappingProvenanceSource,
    MappingReviewCandidate,
    MappingSideEvidence,
    MappingVerdict,
)
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.audit import PaperScanRecord
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.persistence.mapping_rules import SqliteMappingRuleStore
from sports_hedge.persistence.paper import SqlitePaperScanRepository

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
TRIGGER = Decimal("0.01")


def _side(
    venue: VenueName,
    home: str,
    away: str,
    *,
    source_event_id: str,
    source_market_id: str,
) -> MappingSideEvidence:
    return MappingSideEvidence(
        venue=venue,
        source_event_id=source_event_id,
        source_market_id=source_market_id,
        raw_home_team=home,
        raw_away_team=away,
        raw_competition="Premier League",
        kickoff_utc=KICKOFF,
        family=MarketFamily.MATCH_RESULT.value,
        period=FootballPeriod.FULL_TIME.value,
        current_canonical_candidate=f"{home} vs {away}",
    )


def _candidate(*, confidence: float = 0.96) -> MappingReviewCandidate:
    return MappingReviewCandidate(
        sides=[
            _side(VenueName.MATCHBOOK, "Leeds United", "Chelsea", source_event_id="mb-leeds", source_market_id="mb-mkt"),
            _side(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                source_event_id="pm-leeds",
                source_market_id="pm-mkt",
            ),
        ],
        current_confidence=confidence,
        current_reasons=["home_team_fuzzy"],
        current_matched=True,
    )


def _decision(
    *,
    confidence: float = 0.96,
    provenance: MappingProvenance | None = None,
    candidate: MappingReviewCandidate | None = None,
    reasons: list[str] | None = None,
    market_id: str = "mkt-wave-d",
) -> PaperScanDecision:
    return PaperScanDecision(
        scanned_at=OBSERVED,
        canonical_event_id="evt-leeds-chelsea",
        canonical_market_id=market_id,
        market_match=MarketMatchResult(
            matched=True,
            confidence=confidence,
            reasons=reasons or ["home_team_fuzzy"],
            provenance=provenance or MappingProvenance(),
        ),
        mapping_review_candidate=candidate,
        quote_age_ms=120,
        quote_age_basis="source",
        minimum_net_edge=TRIGGER,
        fx_snapshots=[
            FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
        ],
        depth_scan=DepthScanResult(
            solution=ArbitrageSolution(
                is_arbitrage=True,
                implied_probability_sum=Decimal("0.992"),
                total_stake=Decimal("90"),
                guaranteed_return=Decimal("91.23"),
                guaranteed_profit=Decimal("1.23"),
                roi=Decimal("0.008"),
                stakes=[
                    ArbitrageStake(
                        outcome="home",
                        venue=VenueName.MATCHBOOK,
                        source_market_id="mb-mkt",
                        stake=Decimal("50"),
                        net_decimal_odds=Decimal("2.10"),
                        state_return=Decimal("105"),
                    ),
                    ArbitrageStake(
                        outcome="away",
                        venue=VenueName.POLYMARKET,
                        source_market_id="pm-mkt",
                        stake=Decimal("40"),
                        net_decimal_odds=Decimal("2.05"),
                        state_return=Decimal("82"),
                    ),
                ],
            ),
            selected_quotes=[
                DepthQuoteCandidate(
                    outcome="home",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb-mkt",
                    source_runner_id="h",
                    gross_weighted_odds=Decimal("2.12"),
                    net_decimal_odds=Decimal("2.10"),
                    cumulative_depth=Decimal("80"),
                    levels_consumed=1,
                ),
                DepthQuoteCandidate(
                    outcome="away",
                    venue=VenueName.POLYMARKET,
                    source_market_id="pm-mkt",
                    source_runner_id="a",
                    gross_weighted_odds=Decimal("2.05"),
                    net_decimal_odds=Decimal("2.05"),
                    cumulative_depth=Decimal("60"),
                    levels_consumed=1,
                ),
            ],
        ),
    )


def _history() -> list[MarketSnapshot]:
    return [
        MarketSnapshot(
            observed_at=OBSERVED,
            venue=VenueName.MATCHBOOK,
            canonical_event_id="evt-leeds-chelsea",
            canonical_market_id="mkt-wave-d",
            canonical_outcome="home",
            market_family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            settlement_scope=SettlementScope.REGULATION_TIME,
            settlement_key="reg",
            competition="Premier League",
            home_team="Leeds United",
            away_team="Chelsea",
            source_event_id="mb-leeds",
            source_market_id="mb-mkt",
            kickoff_utc=KICKOFF,
            decimal_odds=Decimal("2.1"),
            metadata={"native_currency": "GBP", "raw_event_name": "Leeds United vs Chelsea"},
        ),
        MarketSnapshot(
            observed_at=OBSERVED,
            venue=VenueName.POLYMARKET,
            canonical_event_id="evt-leeds-chelsea",
            canonical_market_id="mkt-wave-d",
            canonical_outcome="away",
            market_family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            settlement_scope=SettlementScope.REGULATION_TIME,
            settlement_key="reg",
            competition="Premier League",
            home_team="Leeds United FC",
            away_team="Chelsea FC",
            source_event_id="pm-leeds",
            source_market_id="pm-mkt",
            kickoff_utc=KICKOFF,
            decimal_odds=Decimal("2.05"),
            metadata={"native_currency": "USD", "password": "should-not-leak", "raw_event_name": "Leeds United FC vs Chelsea FC"},
        ),
    ]


def test_adapter_copies_current_market_match_not_audit() -> None:
    mapped = observation_from_paper_decision(
        _decision(confidence=0.96, candidate=_candidate()),
        _history(),
    )
    assert mapped is not None
    assert mapped.mapping_confidence == 0.96
    assert mapped.mapping_matched is True
    assert mapped.mapping_reasons == ["home_team_fuzzy"]
    assert mapped.mapping_provenance is not None
    assert mapped.mapping_provenance.mapping_source is MappingProvenanceSource.NATIVE_DETERMINISTIC
    assert mapped.mapping_review_candidate is not None
    assert len(mapped.mapping_review_candidate.sides) == 2
    assert [leg.source_runner_id for leg in mapped.legs] == ["h", "a"]
    assert {leg.source_market_id for leg in mapped.legs} == {"mb-mkt", "pm-mkt"}


def test_native_hundred_percent_is_labelled_and_carries_candidate() -> None:
    mapped = observation_from_paper_decision(
        _decision(
            confidence=1.0,
            reasons=["teams_equivalent"],
            provenance=MappingProvenance(mapping_source=MappingProvenanceSource.NATIVE_DETERMINISTIC),
            candidate=_candidate(confidence=1.0),
        ),
        _history(),
    )
    assert mapped is not None
    assert mapped.mapping_confidence == 1.0
    assert mapped.mapping_provenance.mapping_source is MappingProvenanceSource.NATIVE_DETERMINISTIC


def test_learned_rule_provenance_stays_operator_verified() -> None:
    mapped = observation_from_paper_decision(
        _decision(
            confidence=1.0,
            reasons=["operator_verified_suffix"],
            provenance=MappingProvenance(
                mapping_source=MappingProvenanceSource.OPERATOR_VERIFIED,
                rule_id="maprule:abc",
                rule_version=2,
            ),
            candidate=_candidate(confidence=1.0),
        ),
        _history(),
    )
    assert mapped is not None
    assert mapped.mapping_confidence == 1.0
    assert mapped.mapping_provenance.mapping_source is MappingProvenanceSource.OPERATOR_VERIFIED
    assert mapped.mapping_provenance.rule_id == "maprule:abc"
    assert mapped.mapping_provenance.rule_version == 2


def test_missing_current_mapping_evidence_is_unavailable() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(
        WatchObservation(
            observed_at=OBSERVED,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-no-mapping",
            home_team="Leeds United",
            away_team="Chelsea",
            market_family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            legs=[
                WatchLeg(
                    outcome="home",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb",
                    currency="GBP",
                    gbp_stake=Decimal("40"),
                    net_decimal_odds=Decimal("2.1"),
                    cumulative_depth_gbp=Decimal("40"),
                ),
                WatchLeg(
                    outcome="away",
                    venue=VenueName.POLYMARKET,
                    source_market_id="pm",
                    currency="USD",
                    gbp_stake=Decimal("40"),
                    net_decimal_odds=Decimal("2.05"),
                    cumulative_depth_gbp=Decimal("40"),
                ),
            ],
            trigger_net_edge=TRIGGER,
            current_net_edge=Decimal("0.008"),
            solver_is_arbitrage=True,
            quote_age_ms=120,
            limiting_depth_gbp=Decimal("40"),
        )
    )
    assert opportunity.mapping_confidence is None
    assert opportunity.mapping_review_candidate is None


def test_stale_mi_history_does_not_invent_a_verify_candidate_when_decision_has_none() -> None:
    stale_observed = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    stale_history = [
        snapshot.model_copy(update={"observed_at": stale_observed}) for snapshot in _history()
    ]
    historical_candidate = evidence_from_snapshots(
        stale_history,
        MarketMatchResult(matched=True, confidence=0.96, reasons=["home_team_fuzzy"]),
    )
    assert historical_candidate is not None
    assert len(historical_candidate.sides) == 2

    mapped = observation_from_paper_decision(
        _decision(confidence=0.96, candidate=None),
        stale_history,
    )
    assert mapped is not None
    assert mapped.mapping_confidence == 0.96
    assert mapped.mapping_review_candidate is None


def test_incomplete_snapshots_do_not_invent_a_verify_candidate() -> None:
    match = MarketMatchResult(matched=True, confidence=0.94, reasons=["home_team_fuzzy"])
    incomplete = [
        MarketSnapshot(
            observed_at=OBSERVED,
            venue=VenueName.MATCHBOOK,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-incomplete",
            canonical_outcome="home",
            market_family=MarketFamily.MATCH_RESULT,
            decimal_odds=Decimal("2.1"),
            metadata={"native_currency": "GBP"},
        )
    ]
    assert evidence_from_snapshots(incomplete, match) is None


def test_watchlist_persists_current_mapping_and_near_api_exposes_it() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        mapped = observation_from_paper_decision(
            _decision(confidence=0.96, candidate=_candidate()),
            _history(),
        )
        assert mapped is not None
        stored = service.observe(mapped)
        assert stored.mapping_confidence == 0.96
        reloaded = repository.get(stored.opportunity_id)
        assert reloaded is not None
        assert reloaded.mapping_confidence == 0.96
        assert reloaded.mapping_review_candidate is not None
        body = client.get("/paper/watchlist/near").json()
        assert body
        row = next(item for item in body if item["canonical_market_id"] == "mkt-wave-d")
        assert row["mapping_confidence"] == 0.96
        assert row["mapping_review_candidate"]["sides"][0]["source_event_id"] == "mb-leeds"
        assert row["legs"][0]["source_runner_id"] == "h"
    finally:
        app.dependency_overrides.clear()


def test_audit_scan_row_is_not_used_to_fill_watchlist_mapping() -> None:
    audit = SqlitePaperScanRepository()
    audit.append_scan(
        PaperScanRecord(
            scanned_at=OBSERVED,
            canonical_event_id="evt-historical",
            canonical_market_id="mkt-no-mapping",
            competition="Premier League",
            home_team="Leeds United",
            away_team="Chelsea",
            kickoff_utc=KICKOFF,
            market_family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
            source_market_ids=["mb", "pm"],
            mapping_confidence=0.11,
            is_arbitrage=False,
            eligible_for_paper_simulation=False,
            rejection_reasons=["historical_only"],
            decision_json='{"audit": true}',
        )
    )
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(
        WatchObservation(
            observed_at=OBSERVED,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-no-mapping",
            trigger_net_edge=TRIGGER,
            current_net_edge=Decimal("0.008"),
            solver_is_arbitrage=True,
            quote_age_ms=80,
            legs=[
                WatchLeg(
                    outcome="home",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb",
                    currency="GBP",
                    net_decimal_odds=Decimal("2.1"),
                    cumulative_depth_gbp=Decimal("40"),
                )
            ],
        )
    )
    scans = audit.list_scans()
    assert scans[0].mapping_confidence == 0.11
    assert opportunity.mapping_confidence is None
    assert opportunity.mapping_review_candidate is None
    audit.close()


def test_three_selected_solver_legs_are_all_carried() -> None:
    decision = _decision(candidate=_candidate())
    decision.depth_scan.selected_quotes.append(
        DepthQuoteCandidate(
            outcome="draw",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-mkt",
            source_runner_id="d",
            gross_weighted_odds=Decimal("3.4"),
            net_decimal_odds=Decimal("3.40"),
            cumulative_depth=Decimal("50"),
            levels_consumed=1,
        )
    )
    mapped = observation_from_paper_decision(decision, _history())
    assert mapped is not None
    assert [leg.outcome for leg in mapped.legs] == ["home", "away", "draw"]
    assert all(leg.source_market_id for leg in mapped.legs)


def test_generated_prompt_contains_current_evidence_and_excludes_secrets() -> None:
    candidate = evidence_from_snapshots(
        _history(),
        MarketMatchResult(matched=True, confidence=0.96, reasons=["home_team_fuzzy"]),
    )
    assert candidate is not None
    prompt = MappingReviewService(SqliteMappingRuleStore()).build_prompt(candidate)
    assert "Leeds United FC" in prompt.prompt_text
    assert "mb-leeds" in prompt.prompt_text
    assert "password" not in prompt.prompt_text.casefold()
    assert "should-not-leak" not in prompt.prompt_text


def test_scan_pair_attaches_current_review_candidate() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    matchbook = MatchbookObservationBuilder().build(
        {
            "id": 1001,
            "name": "Leeds United vs Chelsea",
            "start": KICKOFF.isoformat(),
            "competition-name": "Premier League",
        },
        {
            "id": 2001,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Leeds", "prices": [{"side": "back", "odds": "2.20", "available-amount": "100"}]},
                {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}]},
                {"id": 3, "name": "Chelsea", "prices": [{"side": "back", "odds": "3.80", "available-amount": "90"}]},
            ],
        },
        observed_at=OBSERVED,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        {
            "id": "pm-leeds",
            "title": "Leeds United FC vs Chelsea FC",
            "startTime": KICKOFF.isoformat(),
            "competition": "Premier League",
        },
        {
            "id": "pm-mr",
            "question": "Match result?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Leeds United FC", "Draw", "Chelsea FC"]',
            "clobTokenIds": '["home-token", "draw-token", "away-token"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "home-token": {"asset_id": "home-token", "bids": [{"price": "0.45", "size": "200"}], "asks": [{"price": "0.47", "size": "200"}]},
            "draw-token": {"asset_id": "draw-token", "bids": [{"price": "0.28", "size": "150"}], "asks": [{"price": "0.30", "size": "150"}]},
            "away-token": {"asset_id": "away-token", "bids": [{"price": "0.24", "size": "180"}], "asks": [{"price": "0.26", "size": "140"}]},
        },
        observed_at=OBSERVED,
        quote_age_ms=180,
    )
    try:
        decision = service.scan_pair(matchbook, polymarket)
        assert decision.mapping_review_candidate is not None
        assert len(decision.mapping_review_candidate.sides) == 2
        assert decision.market_match.confidence == decision.mapping_review_candidate.current_confidence
        rebuilt = evidence_from_markets(
            matchbook.market,
            polymarket.market,
            matcher=service.market_matcher,
            left_raw=matchbook.metadata,
            right_raw=polymarket.metadata,
        )
        assert rebuilt.current_confidence == decision.market_match.confidence
    finally:
        repository.close()


def test_ambiguous_confirm_does_not_activate_and_structural_mismatch_stays_blocked() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    ambiguous = service.confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.AMBIGUOUS,
    )
    assert ambiguous.proposed_rule is None
    assert store.list_enabled() == []

    settlement_conflict = _candidate()
    settlement_conflict.conflicting_fields = ["settlement"]
    blocked = service.confirm(
        settlement_conflict,
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    assert blocked.proposed_rule is None
    assert blocked.activation_blocked_reason
    assert store.list_enabled() == []
    store.close()
