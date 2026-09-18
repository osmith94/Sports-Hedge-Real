from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from json import loads
from uuid import uuid4

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.mapping_reviews import get_mapping_review_service
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.mapping_review import MappingReviewService, parse_chatgpt_verdict
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import (
    LearnedMappingApplicator,
    MappingProvenanceSource,
    MappingReviewCandidate,
    MappingRuleSource,
    MappingRuleType,
    MappingSideEvidence,
    MappingVerdict,
    candidate_structural_conflicts,
    infer_learned_rule,
    sanitize_mapping_payload,
    structural_evidence_conflicts,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.paper.audit import PaperScanRecord
from sports_hedge.persistence.mapping_rules import SqliteMappingRuleStore, get_mapping_rule_store
from sports_hedge.persistence.paper import SqlitePaperScanRepository


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def _event(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
    source_event_id: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id or f"{venue.value}-{home}",
    )


def _market(
    event: CanonicalEvent,
    *,
    extra_time: bool = False,
    family: MarketFamily = MarketFamily.MATCH_RESULT,
    period: FootballPeriod = FootballPeriod.FULL_TIME,
    source_market_id: str | None = None,
) -> CanonicalMarket:
    scope = SettlementScope.INCLUDING_EXTRA_TIME if extra_time else SettlementScope.REGULATION_TIME
    runners = [
        CanonicalRunner(source_runner_id="h", outcome=CanonicalOutcome.HOME, label="Home"),
        CanonicalRunner(source_runner_id="d", outcome=CanonicalOutcome.DRAW, label="Draw"),
        CanonicalRunner(source_runner_id="a", outcome=CanonicalOutcome.AWAY, label="Away"),
    ]
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        runners = [
            CanonicalRunner(source_runner_id="y", outcome=CanonicalOutcome.YES, label="Yes"),
            CanonicalRunner(source_runner_id="n", outcome=CanonicalOutcome.NO, label="No"),
        ]
    return CanonicalMarket(
        event=event,
        source_venue=event.source_venue,
        source_market_id=source_market_id or f"{event.source_venue.value}-mkt",
        family=family,
        period=period,
        settlement=SettlementFingerprint(
            scope=scope,
            period=period,
            extra_time_included=extra_time,
            penalties_included=False,
        ),
        runners=runners,
    )


def _side(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
    source_event_id: str = "evt",
    extra_time: bool = False,
    family: MarketFamily = MarketFamily.MATCH_RESULT,
    period: FootballPeriod = FootballPeriod.FULL_TIME,
) -> MappingSideEvidence:
    market = _market(
        _event(venue, home, away, competition=competition, kickoff=kickoff, source_event_id=source_event_id),
        extra_time=extra_time,
        family=family,
        period=period,
    )
    return MappingSideEvidence(
        venue=venue,
        source_event_id=market.event.source_event_id,
        source_market_id=market.source_market_id,
        raw_event_name=f"{home} vs {away}",
        raw_home_team=home,
        raw_away_team=away,
        raw_competition=competition,
        kickoff_utc=kickoff,
        raw_market_name="Match Result",
        raw_market_type="match_odds",
        raw_runner_labels=[runner.label for runner in market.runners],
        family=market.family.value,
        period=market.period.value,
        settlement_key=market.settlement.deterministic_key(),
        settlement_scope=market.settlement.scope.value,
        outcome_space=[runner.outcome.value for runner in market.runners],
        current_canonical_candidate=f"{home} vs {away}",
        confidence=0.92,
        match_reasons=["home_team_fuzzy"],
    )


def _candidate() -> MappingReviewCandidate:
    return MappingReviewCandidate(
        sides=[
            _side(VenueName.MATCHBOOK, "Leeds United", "Chelsea", source_event_id="mb-leeds"),
            _side(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                source_event_id="pm-leeds",
            ),
        ],
        current_confidence=0.9216,
        current_reasons=["home_team_fuzzy", "away_team_fuzzy"],
        current_matched=True,
        conflicting_fields=[],
    )


def _matcher_with_store(store: SqliteMappingRuleStore) -> MarketMatcher:
    applicator = LearnedMappingApplicator(store)
    return MarketMatcher(EventMatcher(learned_applicator=applicator))


def test_native_hundred_percent_does_not_need_learned_rule() -> None:
    result = MarketMatcher().match(
        _market(_event(VenueName.MATCHBOOK, "Newcastle United", "Chelsea")),
        _market(_event(VenueName.POLYMARKET, "Newcastle United", "Chelsea")),
    )
    assert result.matched is True
    assert result.confidence == 1.0
    assert result.provenance.mapping_source is MappingProvenanceSource.NATIVE_DETERMINISTIC
    assert result.provenance.rule_id is None


def test_below_hundred_fuzzy_fc_suffix_is_not_native_perfect() -> None:
    # Curated senior clubs may now resolve FC suffixes natively (issue 309).
    # Unknown remainders must stay fail-closed and not look native-perfect.
    result = MarketMatcher().match(
        _market(_event(VenueName.MATCHBOOK, "Unknownville", "Otherville")),
        _market(_event(VenueName.POLYMARKET, "Unknownville FC", "Otherville FC")),
    )
    assert result.confidence < 1.0
    assert result.provenance.mapping_source is MappingProvenanceSource.NATIVE_DETERMINISTIC


def test_infer_prefers_reusable_venue_suffix_over_fixture_alias() -> None:
    rule = infer_learned_rule(
        _side(VenueName.MATCHBOOK, "Leeds United", "Chelsea", source_event_id="mb-1"),
        _side(VenueName.POLYMARKET, "Leeds United FC", "Chelsea FC", source_event_id="pm-1"),
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        evidence="operator confirmed same Premier League fixture",
    )
    assert rule is not None
    assert rule.rule_type is MappingRuleType.VENUE_SUFFIX_STRIP
    assert rule.venue is VenueName.POLYMARKET
    assert rule.raw_pattern == "fc"
    assert rule.guardrails.competition_code == "premier_league"


def test_confirmed_reusable_rule_maps_second_fixture_same_venue() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    saved = service.confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
        source=MappingRuleSource.OPERATOR_MANUAL,
    )
    assert saved.operator_confirmed is True
    assert saved.proposed_rule is not None
    matcher = _matcher_with_store(store)
    first = matcher.match(
        _market(_event(VenueName.MATCHBOOK, "Leeds United", "Chelsea", source_event_id="mb-leeds")),
        _market(
            _event(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                source_event_id="pm-leeds",
            )
        ),
    )
    second = matcher.match(
        _market(
            _event(
                VenueName.MATCHBOOK,
                "Arsenal",
                "Tottenham Hotspur",
                source_event_id="mb-ars",
            )
        ),
        _market(
            _event(
                VenueName.POLYMARKET,
                "Arsenal FC",
                "Tottenham Hotspur FC",
                source_event_id="pm-ars",
            )
        ),
    )
    assert first.matched is True
    assert first.confidence == 1.0
    assert first.provenance.mapping_source is MappingProvenanceSource.OPERATOR_VERIFIED
    assert first.provenance.rule_id == saved.proposed_rule.rule_id
    assert first.provenance.rule_version == saved.proposed_rule.version
    assert "operator_verified_learned_alias" in first.reasons
    assert second.matched is True
    assert second.confidence == 1.0
    assert second.provenance.rule_id == saved.proposed_rule.rule_id
    store.close()


def test_reusable_rule_refuses_cross_competition_and_kickoff_guardrails() -> None:
    store = SqliteMappingRuleStore()
    MappingReviewService(store).confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    matcher = _matcher_with_store(store)
    championship = matcher.match(
        _market(
            _event(
                VenueName.MATCHBOOK,
                "Leeds United",
                "Chelsea",
                competition="Premier League",
                source_event_id="mb-ch",
            )
        ),
        _market(
            _event(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                competition="Championship",
                source_event_id="pm-ch",
            )
        ),
    )
    assert championship.matched is False
    assert championship.provenance.mapping_source is MappingProvenanceSource.NATIVE_DETERMINISTIC

    kickoff = matcher.match(
        _market(_event(VenueName.MATCHBOOK, "Leeds United", "Chelsea", source_event_id="mb-k")),
        _market(
            _event(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                kickoff=KICKOFF + timedelta(minutes=12),
                source_event_id="pm-k",
            )
        ),
    )
    assert kickoff.matched is False
    assert "kickoff_outside_tolerance" in kickoff.reasons
    store.close()


def test_settlement_mismatch_cannot_be_overridden_by_naming_rule() -> None:
    store = SqliteMappingRuleStore()
    MappingReviewService(store).confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    matcher = _matcher_with_store(store)
    result = matcher.match(
        _market(_event(VenueName.MATCHBOOK, "Leeds United", "Chelsea"), extra_time=False),
        _market(
            _event(VenueName.POLYMARKET, "Leeds United FC", "Chelsea FC"),
            extra_time=True,
        ),
    )
    assert result.matched is False
    assert "settlement_mismatch" in result.reasons
    assert result.provenance.mapping_source is MappingProvenanceSource.OPERATOR_VERIFIED
    store.close()


def test_verified_confirm_blocks_activation_on_settlement_mismatch() -> None:
    store = SqliteMappingRuleStore()
    candidate = MappingReviewCandidate(
        sides=[
            _side(VenueName.MATCHBOOK, "Leeds United", "Chelsea", source_event_id="mb-set"),
            _side(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                source_event_id="pm-set",
                extra_time=True,
            ),
        ],
        current_confidence=0.92,
        current_reasons=["settlement_mismatch"],
        current_matched=False,
        conflicting_fields=["settlement"],
    )
    assert "settlement" in structural_evidence_conflicts(candidate.sides[0], candidate.sides[1])
    proposal = MappingReviewService(store).confirm(
        candidate,
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    assert proposal.verdict is MappingVerdict.VERIFIED
    assert proposal.operator_confirmed is True
    assert proposal.proposed_rule is None
    assert proposal.activation_blocked_reason is not None
    assert "settlement" in proposal.activation_blocked_reason
    assert store.list_enabled() == []
    review = store.get_review(proposal.review_id)
    assert review is not None
    assert review["activated_rule_id"] is None
    assert review["activation_blocked_reason"] == proposal.activation_blocked_reason
    assert review["verdict"] == "verified"
    store.close()


def test_verified_confirm_blocks_activation_on_family_period_outcome_mismatch() -> None:
    store = SqliteMappingRuleStore()
    candidate = MappingReviewCandidate(
        sides=[
            _side(
                VenueName.MATCHBOOK,
                "Leeds United",
                "Chelsea",
                source_event_id="mb-fam",
                family=MarketFamily.MATCH_RESULT,
                period=FootballPeriod.FULL_TIME,
            ),
            _side(
                VenueName.POLYMARKET,
                "Leeds United FC",
                "Chelsea FC",
                source_event_id="pm-fam",
                family=MarketFamily.BOTH_TEAMS_TO_SCORE,
                period=FootballPeriod.FIRST_HALF,
            ),
        ],
        current_confidence=0.88,
        current_reasons=["market_family_mismatch", "period_mismatch", "outcome_space_mismatch"],
        current_matched=False,
        conflicting_fields=["market_family", "period", "outcome_space"],
    )
    conflicts = structural_evidence_conflicts(candidate.sides[0], candidate.sides[1])
    assert "market_family" in conflicts
    assert "period" in conflicts
    assert "outcome_space" in conflicts
    proposal = MappingReviewService(store).confirm(
        candidate,
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    assert proposal.proposed_rule is None
    assert proposal.activation_blocked_reason is not None
    reason = proposal.activation_blocked_reason
    assert reason.startswith("structural_conflicts:")
    assert "market_family" in reason
    assert "period" in reason
    assert "outcome_space" in reason
    assert store.list_enabled() == []
    review = store.get_review(proposal.review_id)
    assert review is not None
    assert review["activated_rule_id"] is None
    assert review["activation_blocked_reason"] == reason
    store.close()


def test_verified_confirm_activates_despite_benign_naming_conflicts() -> None:
    store = SqliteMappingRuleStore()
    candidate = MappingReviewCandidate(
        sides=_candidate().sides,
        current_confidence=0.9216,
        current_reasons=["home_team_fuzzy", "away_team_fuzzy"],
        current_matched=True,
        conflicting_fields=["home_team", "raw_event_name"],
    )
    assert candidate_structural_conflicts(candidate) == []
    proposal = MappingReviewService(store).confirm(
        candidate,
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
        source=MappingRuleSource.OPERATOR_MANUAL,
    )
    assert proposal.verdict is MappingVerdict.VERIFIED
    assert proposal.operator_confirmed is True
    assert proposal.activation_blocked_reason is None
    assert proposal.proposed_rule is not None
    assert proposal.proposed_rule.rule_type is MappingRuleType.VENUE_SUFFIX_STRIP
    assert proposal.proposed_rule.venue is VenueName.POLYMARKET
    assert proposal.proposed_rule.raw_pattern == "fc"
    assert proposal.proposed_rule.enabled is True
    review = store.get_review(proposal.review_id)
    assert review is not None
    assert review["activated_rule_id"] == proposal.proposed_rule.rule_id
    assert review["activation_blocked_reason"] is None
    evidence = loads(review["evidence_json"])
    assert evidence["conflicting_fields"] == ["home_team", "raw_event_name"]
    enabled = store.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].rule_id == proposal.proposed_rule.rule_id
    store.close()


def test_prompt_contains_provenance_and_excludes_secrets() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    bundle = service.build_prompt(_candidate())
    assert "Leeds United FC" in bundle.prompt_text
    assert "source_event_id: mb-leeds" in bundle.prompt_text
    assert "source_market_id:" in bundle.prompt_text
    assert "settlement_key:" in bundle.prompt_text
    assert "VERIFIED / NOT VERIFIED / AMBIGUOUS" in bundle.prompt_text
    assert "password" not in bundle.prompt_text.casefold()
    assert "super-secret" not in bundle.prompt_text
    assert "Bearer" not in bundle.prompt_text
    sanitized = sanitize_mapping_payload(
        {"password": "x", "api_key": "y", "home": "Leeds", "token": "z"}
    )
    assert "password" not in sanitized
    assert "api_key" not in sanitized
    assert sanitized["home"] == "Leeds"
    store.close()


def test_verified_requires_explicit_confirmation_and_chatgpt_alone_activates_nothing() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    proposal = service.interpret(
        _candidate(),
        operator="oliver",
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        operator_confirmed=False,
    )
    assert proposal.verdict is MappingVerdict.VERIFIED
    assert proposal.activation_blocked_reason == "explicit_operator_confirmation_required"
    assert store.list_enabled() == []
    store.close()


def test_interpret_then_confirm_threads_the_same_review_id() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    interpreted = service.interpret(
        _candidate(),
        operator="oliver",
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        operator_confirmed=False,
    )
    assert interpreted.verdict is MappingVerdict.VERIFIED
    assert interpreted.activation_blocked_reason == "explicit_operator_confirmation_required"
    assert interpreted.proposed_rule is not None
    assert store.list_enabled() == []

    confirmed = service.confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        manual_verdict=MappingVerdict.VERIFIED,
        review_id=interpreted.review_id,
    )
    assert confirmed.review_id == interpreted.review_id
    assert confirmed.activation_blocked_reason is None
    assert confirmed.proposed_rule is not None
    assert confirmed.proposed_rule.review_id == interpreted.review_id
    review = store.get_review(interpreted.review_id)
    assert review is not None
    assert review["activated_rule_id"] == confirmed.proposed_rule.rule_id
    enabled = store.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].review_id == interpreted.review_id
    assert enabled[0].rule_id == confirmed.proposed_rule.rule_id
    store.close()


def test_confirm_does_not_overwrite_review_when_candidate_changes() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    candidate_a = _candidate()
    interpreted = service.interpret(
        candidate_a,
        operator="oliver",
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        operator_confirmed=False,
    )
    review_a = store.get_review(interpreted.review_id)
    assert review_a is not None
    evidence_a = review_a["evidence_json"]

    candidate_b = _candidate()
    candidate_b.sides[1].source_event_id = "pm-leeds-refreshed"
    candidate_b.current_confidence = 0.88
    mismatched = service.confirm(
        candidate_b,
        operator="oliver",
        operator_confirmed=True,
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        manual_verdict=MappingVerdict.VERIFIED,
        review_id=interpreted.review_id,
    )
    assert mismatched.activation_blocked_reason == "review_evidence_changed"
    assert mismatched.proposed_rule is None
    assert mismatched.operator_confirmed is False
    assert store.list_enabled() == []
    unchanged = store.get_review(interpreted.review_id)
    assert unchanged is not None
    assert unchanged["evidence_json"] == evidence_a
    assert unchanged["activated_rule_id"] is None

    confirmed_a = service.confirm(
        candidate_a,
        operator="oliver",
        operator_confirmed=True,
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        manual_verdict=MappingVerdict.VERIFIED,
        review_id=interpreted.review_id,
    )
    assert confirmed_a.review_id == interpreted.review_id
    assert confirmed_a.proposed_rule is not None
    assert confirmed_a.proposed_rule.review_id == interpreted.review_id

    interpreted_b = service.interpret(
        candidate_b,
        operator="oliver",
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        operator_confirmed=False,
    )
    assert interpreted_b.review_id != interpreted.review_id
    confirmed_b = service.confirm(
        candidate_b,
        operator="oliver",
        operator_confirmed=True,
        chatgpt_text="VERIFIED. Strip FC suffix on Polymarket team names.",
        manual_verdict=MappingVerdict.VERIFIED,
        review_id=interpreted_b.review_id,
    )
    assert confirmed_b.review_id == interpreted_b.review_id
    assert confirmed_b.proposed_rule is not None
    assert confirmed_b.proposed_rule.review_id == interpreted_b.review_id
    assert store.get_review(interpreted.review_id)["activated_rule_id"] == confirmed_a.proposed_rule.rule_id
    store.close()


def test_ambiguous_and_not_verified_activate_nothing() -> None:
    store = SqliteMappingRuleStore()
    service = MappingReviewService(store)
    ambiguous = service.interpret(
        _candidate(),
        operator="oliver",
        chatgpt_text="AMBIGUOUS — could be a different Leeds.",
    )
    denied = service.confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        chatgpt_text="NOT VERIFIED. Different settlement.",
    )
    assert ambiguous.verdict is MappingVerdict.AMBIGUOUS
    assert denied.verdict is MappingVerdict.NOT_VERIFIED
    assert store.list_enabled() == []
    assert store.get_review(ambiguous.review_id) is not None
    assert store.get_review(ambiguous.review_id)["activated_rule_id"] is None
    store.close()


def test_prompt_generation_is_local_and_off_executable_quote_critical_path() -> None:
    collector_source = inspect.getsource(ReadOnlyCrossVenueCollector)
    paper_scan_source = inspect.getsource(PaperScanService)
    prompt_source = inspect.getsource(MappingReviewService.build_prompt)

    assert "build_prompt" not in collector_source
    assert "MappingReviewService" not in collector_source
    assert "build_prompt" not in paper_scan_source
    assert "httpx" not in prompt_source
    assert "openai" not in prompt_source.casefold()
    assert "chat/completions" not in prompt_source.casefold()


def test_parse_chatgpt_verdicts() -> None:
    assert parse_chatgpt_verdict("The pair is VERIFIED.") is MappingVerdict.VERIFIED
    assert parse_chatgpt_verdict("NOT VERIFIED because extra time differs.") is MappingVerdict.NOT_VERIFIED
    assert parse_chatgpt_verdict("AMBIGUOUS") is MappingVerdict.AMBIGUOUS
    assert parse_chatgpt_verdict("no structured token") is MappingVerdict.AMBIGUOUS


def test_rule_survives_restart(tmp_path) -> None:
    path = tmp_path / "mapping-rules.sqlite"
    store = SqliteMappingRuleStore(path)
    MappingReviewService(store).confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    rule_id = store.list_enabled()[0].rule_id
    store.close()
    reopened = SqliteMappingRuleStore(path)
    enabled = reopened.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].rule_id == rule_id
    reopened.close()


def test_disable_and_revoke_stop_applying_without_rewriting_audit(tmp_path) -> None:
    store = SqliteMappingRuleStore()
    proposal = MappingReviewService(store).confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    assert proposal.proposed_rule is not None
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    record = PaperScanRecord(
        record_id=str(uuid4()),
        scanned_at=OBSERVED,
        canonical_event_id="evt:historical",
        canonical_market_id="mkt:historical",
        competition="Premier League",
        home_team="Leeds United",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
        source_market_ids=["mb", "pm"],
        mapping_confidence=0.96,
        is_arbitrage=False,
        eligible_for_paper_simulation=False,
        rejection_reasons=["mapping_confidence_below_threshold"],
        decision_json='{"historical": true}',
    )
    audit.append_scan(record)
    MappingReviewService(store).disable(proposal.proposed_rule.rule_id, operator="oliver", revoke=True)
    matcher = _matcher_with_store(store)
    result = matcher.match(
        _market(_event(VenueName.MATCHBOOK, "Unknownville", "Otherville")),
        _market(_event(VenueName.POLYMARKET, "Unknownville FC", "Otherville FC")),
    )
    assert result.confidence < 1.0
    assert result.provenance.mapping_source is MappingProvenanceSource.NATIVE_DETERMINISTIC
    rows = audit.list_scans()
    assert len(rows) == 1
    assert rows[0].record_id == record.record_id
    assert rows[0].mapping_confidence == 0.96
    assert rows[0].decision_json == '{"historical": true}'
    assert store.get_rule(proposal.proposed_rule.rule_id).revoked is True
    assert store.list_audit(proposal.proposed_rule.rule_id)
    store.close()
    audit.close()


def test_learned_mapping_does_not_bypass_paper_qualification_gates() -> None:
    store = SqliteMappingRuleStore()
    MappingReviewService(store).confirm(
        _candidate(),
        operator="oliver",
        operator_confirmed=True,
        manual_verdict=MappingVerdict.VERIFIED,
    )
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence, mapping_rule_store=store)
    matchbook = MatchbookObservationBuilder().build(
        {
            "id": 1001,
            "name": "Leeds United vs Chelsea",
            "start": KICKOFF.isoformat(),
            "competition-name": "Premier League",
        },
        {
            "id": 2001,
            "name": "Both Teams To Score",
            "runners": [
                {
                    "id": 301,
                    "name": "Yes",
                    "prices": [
                        {"side": "back", "odds": "2.20", "available-amount": "100"},
                        {"side": "lay", "odds": "2.22", "available-amount": "100"},
                    ],
                },
                {
                    "id": 302,
                    "name": "No",
                    "prices": [
                        {"side": "back", "odds": "1.80", "available-amount": "100"},
                        {"side": "lay", "odds": "1.82", "available-amount": "100"},
                    ],
                },
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
            "id": "pm-btts",
            "question": "Both teams to score?",
            "sportsMarketType": "both teams to score",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "yes-token": {
                "asset_id": "yes-token",
                "bids": [{"price": "0.49", "size": "250"}],
                "asks": [{"price": "0.51", "size": "250"}],
            },
            "no-token": {
                "asset_id": "no-token",
                "bids": [{"price": "0.41", "size": "300"}],
                "asks": [{"price": "0.43", "size": "160"}],
            },
        },
        observed_at=OBSERVED,
        quote_age_ms=180,
    )
    try:
        decision = service.scan_pair(matchbook, polymarket)
        assert decision.market_match.matched is True
        assert decision.market_match.confidence == 1.0
        assert (
            decision.market_match.provenance.mapping_source
            is MappingProvenanceSource.OPERATOR_VERIFIED
        )
        assert decision.eligible_for_paper_simulation is False
        assert any(
            reason.startswith("missing_venue_cost:") or reason.startswith("missing_fx_rate:")
            for reason in decision.rejection_reasons
        )
    finally:
        repository.close()
        store.close()


def test_mapping_review_api_confirm_and_prompt(tmp_path) -> None:
    store = SqliteMappingRuleStore(tmp_path / "mapping-api.sqlite")
    app.dependency_overrides[get_mapping_rule_store] = lambda: store
    app.dependency_overrides[get_mapping_review_service] = lambda: MappingReviewService(store)
    client = TestClient(app)
    try:
        payload = {"candidate": _candidate().model_dump(mode="json")}
        prompt = client.post("/paper/mapping-reviews/prompt", json=payload)
        assert prompt.status_code == 200
        assert "Leeds United FC" in prompt.json()["prompt_text"]
        assert "password" not in prompt.json()["prompt_text"].casefold()
        blocked = client.post(
            "/paper/mapping-reviews/confirm",
            json={**payload, "operator": "oliver", "manual_verdict": "verified"},
        )
        assert blocked.status_code == 400
        saved = client.post(
            "/paper/mapping-reviews/confirm",
            json={
                **payload,
                "operator": "oliver",
                "manual_verdict": "verified",
                "operator_confirmed": True,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["operator_confirmed"] is True
        assert body["proposed_rule"]["rule_type"] == "venue_suffix_strip"
        rules = client.get("/paper/mapping-reviews/rules")
        assert rules.status_code == 200
        assert len(rules.json()) == 1
    finally:
        app.dependency_overrides.clear()
        store.close()


def test_mapping_review_api_interpret_then_confirm_same_review(tmp_path) -> None:
    store = SqliteMappingRuleStore(tmp_path / "mapping-interpret-confirm.sqlite")
    app.dependency_overrides[get_mapping_rule_store] = lambda: store
    app.dependency_overrides[get_mapping_review_service] = lambda: MappingReviewService(store)
    client = TestClient(app)
    try:
        payload = {
            "candidate": _candidate().model_dump(mode="json"),
            "operator": "oliver",
            "chatgpt_text": "VERIFIED. Strip FC suffix on Polymarket team names.",
            "manual_verdict": "verified",
        }
        interpreted = client.post("/paper/mapping-reviews/interpret", json=payload)
        assert interpreted.status_code == 200
        interpret_body = interpreted.json()
        assert interpret_body["verdict"] == "verified"
        assert interpret_body["activation_blocked_reason"] == "explicit_operator_confirmation_required"
        review_id = interpret_body["review_id"]
        assert review_id
        assert store.list_enabled() == []

        confirmed = client.post(
            "/paper/mapping-reviews/confirm",
            json={**payload, "operator_confirmed": True, "review_id": review_id},
        )
        assert confirmed.status_code == 200
        confirm_body = confirmed.json()
        assert confirm_body["review_id"] == review_id
        assert confirm_body["proposed_rule"]["review_id"] == review_id
        fetched = client.get(f"/paper/mapping-reviews/{review_id}")
        assert fetched.status_code == 200
        assert fetched.json()["activated_rule_id"] == confirm_body["proposed_rule"]["rule_id"]
        rules = client.get("/paper/mapping-reviews/rules")
        assert rules.status_code == 200
        assert len(rules.json()) == 1
        assert rules.json()[0]["review_id"] == review_id
    finally:
        app.dependency_overrides.clear()
        store.close()
