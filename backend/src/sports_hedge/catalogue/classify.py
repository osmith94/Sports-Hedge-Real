"""Deterministic Tenet 20 classification. Confidence is never executable permission.

Incomplete settlement is REVIEW_REQUIRED even when MarketMatcher currently matches
Matchbook↔Kalshi GAMEWIN-unknown ordinary 1X2. Production solver/paper admission
requires APPROVED_EQUIVALENT via the shared HOT/UNIVERSE catalogue gate.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.complete_set import (
    runner_outcomes,
    solver_model_for_pair,
)
from sports_hedge.catalogue.states import (
    CENSUS_V1_FAMILIES,
    REQUIRED_OUTCOMES,
    CatalogueApprovalState,
    CatalogueArchetype,
    family_to_archetype,
)
from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    MarketFamily,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.matching.ordinary_1x2 import (
    allow_unknown_settlement_for_ordinary_1x2,
    settlement_fingerprints_contradict,
)
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)

DATA_CLASS_FIXTURE = "deterministic_fixture"
CATALOGUE_SHARED_BY = ("hot", "universe")


class CataloguePairAssessment(BaseModel):
    """Pairwise catalogue verdict. Scan-lane independent. Paper-only."""

    state: CatalogueApprovalState
    reason: str
    archetype: CatalogueArchetype | None = None
    venue_pair: str | None = None
    solver_model: str | None = None
    matcher_matched: bool = False
    matcher_admits_unknown_1x2: bool = False
    settlement_complete: bool = False
    known_conflict_with_current_matcher: bool = False
    notes: list[str] = Field(default_factory=list)
    data_class: str = DATA_CLASS_FIXTURE
    catalogue_shared_by: tuple[str, ...] = CATALOGUE_SHARED_BY
    execution_eligible: bool = False


class PayloadSide(BaseModel):
    venue: VenueName
    event: dict[str, Any]
    markets: list[dict[str, Any]]
    series: dict[str, Any] | None = None


def venue_pair_key(left: VenueName, right: VenueName) -> str:
    names = {left, right}
    if names == {VenueName.MATCHBOOK, VenueName.KALSHI}:
        return "matchbook_kalshi"
    if names == {VenueName.MATCHBOOK, VenueName.POLYMARKET}:
        return "matchbook_polymarket"
    if names == {VenueName.KALSHI, VenueName.POLYMARKET}:
        return "kalshi_polymarket"
    return "_".join(sorted(item.value for item in names))


def classify_normalization_error(exc: Exception) -> tuple[CatalogueApprovalState, str]:
    message = str(exc).casefold()
    if "integer/quarter" in message or "deferred" in message:
        return CatalogueApprovalState.UNSUPPORTED, "venue_deferred_until_rules_proven"
    if "not inferred" in message:
        return CatalogueApprovalState.UNSUPPORTED, "explicitly_not_inferred"
    if "unsupported" in message:
        return CatalogueApprovalState.UNSUPPORTED, "unsupported_venue_market"
    if "requires" in message:
        return (
            CatalogueApprovalState.REVIEW_REQUIRED,
            "plausible_archetype_incomplete_evidence",
        )
    return CatalogueApprovalState.REVIEW_REQUIRED, "normalization_failed_closed"


def normalize_payload_side(side: PayloadSide) -> CanonicalMarket:
    if side.venue is VenueName.MATCHBOOK:
        event = MatchbookNormalizer().normalize_event(side.event)
        markets = [MatchbookNormalizer().normalize_market(event, item) for item in side.markets]
    elif side.venue is VenueName.POLYMARKET:
        event = PolymarketNormalizer().normalize_event(side.event)
        markets = PolymarketNormalizer().assemble_canonical_markets(event, side.markets)
    elif side.venue is VenueName.KALSHI:
        event = KalshiNormalizer().normalize_event(side.event, series=side.series)
        markets = KalshiNormalizer().assemble_canonical_markets(
            event, side.markets, series=side.series
        )
    else:
        raise VenueNormalizationError(f"Unsupported census venue: {side.venue}")
    if len(markets) != 1:
        raise VenueNormalizationError(
            f"{side.venue.value} census side assembled {len(markets)} markets; expected 1"
        )
    return markets[0]


def classify_payload_pair(
    left: PayloadSide,
    right: PayloadSide,
) -> CataloguePairAssessment:
    notes: list[str] = []
    try:
        left_market = normalize_payload_side(left)
    except (VenueNormalizationError, ValueError) as exc:
        left_state, left_reason = classify_normalization_error(exc)
        try:
            normalize_payload_side(right)
        except (VenueNormalizationError, ValueError) as right_exc:
            right_state, right_reason = classify_normalization_error(right_exc)
            state = (
                CatalogueApprovalState.UNSUPPORTED
                if CatalogueApprovalState.UNSUPPORTED in {left_state, right_state}
                else CatalogueApprovalState.REVIEW_REQUIRED
            )
            return CataloguePairAssessment(
                state=state,
                reason=f"{left_reason}|{right_reason}",
                venue_pair=venue_pair_key(left.venue, right.venue),
                notes=[str(exc), str(right_exc)],
            )
        return CataloguePairAssessment(
            state=left_state,
            reason=left_reason,
            venue_pair=venue_pair_key(left.venue, right.venue),
            notes=[str(exc)],
        )
    try:
        right_market = normalize_payload_side(right)
    except (VenueNormalizationError, ValueError) as exc:
        state, reason = classify_normalization_error(exc)
        notes.append(str(exc))
        return CataloguePairAssessment(
            state=state,
            reason=reason,
            archetype=_archetype_from_market(left_market),
            venue_pair=venue_pair_key(left.venue, right.venue),
            notes=notes,
        )
    return classify_pair(left_market, right_market)


def classify_pair(left: CanonicalMarket, right: CanonicalMarket) -> CataloguePairAssessment:
    """Classify market-contract equivalence. Fixture identity remains separate."""

    matcher = MarketMatcher().match(left, right)
    unknown_1x2 = allow_unknown_settlement_for_ordinary_1x2(left, right)
    solver_model = solver_model_for_pair(left, right)
    pair = venue_pair_key(left.source_venue, right.source_venue)
    complete = (
        left.settlement.is_economically_complete() and right.settlement.is_economically_complete()
    )
    archetype = _archetype_from_markets(left, right)
    state, reason, notes = _economic_state(left, right)
    conflict = (
        state is not CatalogueApprovalState.APPROVED_EQUIVALENT
        and matcher.matched
        and solver_model is not None
    )
    if conflict:
        notes.append(
            "legacy_matcher_may_still_match; catalogue_blocks_solver_admission"
        )
    return CataloguePairAssessment(
        state=state,
        reason=reason,
        archetype=archetype,
        venue_pair=pair,
        solver_model=solver_model,
        matcher_matched=matcher.matched,
        matcher_admits_unknown_1x2=unknown_1x2,
        settlement_complete=complete,
        known_conflict_with_current_matcher=conflict,
        notes=notes,
        execution_eligible=False,
    )


def _archetype_from_market(market: CanonicalMarket) -> CatalogueArchetype | None:
    integer = None
    if market.family is MarketFamily.TOTAL_GOALS:
        integer = line_push_possible(market.line) is True
    return family_to_archetype(market.family, integer_line=integer)


def _archetype_from_markets(
    left: CanonicalMarket, right: CanonicalMarket
) -> CatalogueArchetype | None:
    left_arch = _archetype_from_market(left)
    right_arch = _archetype_from_market(right)
    if left_arch == right_arch:
        return left_arch
    return left_arch or right_arch


def _economic_state(
    left: CanonicalMarket, right: CanonicalMarket
) -> tuple[CatalogueApprovalState, str, list[str]]:
    notes: list[str] = []
    left_in = left.family in CENSUS_V1_FAMILIES
    right_in = right.family in CENSUS_V1_FAMILIES
    if not left_in and not right_in:
        return CatalogueApprovalState.UNSUPPORTED, "both_outside_census_v1_catalogue", notes
    if not left_in or not right_in:
        return (
            CatalogueApprovalState.UNSUPPORTED,
            "one_side_outside_census_v1_catalogue",
            notes,
        )
    if left.family != right.family:
        return CatalogueApprovalState.KNOWN_CONTRADICTION, "market_family_mismatch", notes
    if left.period != right.period:
        return CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH, "period_mismatch", notes
    if left.line != right.line:
        return CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH, "line_mismatch", notes
    if settlement_fingerprints_contradict(left.settlement, right.settlement):
        return CatalogueApprovalState.KNOWN_CONTRADICTION, "settlement_mismatch", notes

    left_outcomes = runner_outcomes(left)
    right_outcomes = runner_outcomes(right)
    required = REQUIRED_OUTCOMES[left.family]
    if left_outcomes != right_outcomes:
        if left.family is MarketFamily.FIRST_TEAM_TO_SCORE:
            left_no = CanonicalOutcome.NO_GOAL in left_outcomes
            right_no = CanonicalOutcome.NO_GOAL in right_outcomes
            if left_no != right_no:
                return (
                    CatalogueApprovalState.KNOWN_CONTRADICTION,
                    "ftts_no_goal_contract_mismatch",
                    notes,
                )
        if left.family is MarketFamily.MATCH_RESULT:
            return (
                CatalogueApprovalState.REVIEW_REQUIRED,
                "match_result_outcome_space_incomplete_or_mismatched",
                notes,
            )
        return CatalogueApprovalState.KNOWN_CONTRADICTION, "outcome_space_mismatch", notes
    if CanonicalOutcome.OTHER in left_outcomes:
        return CatalogueApprovalState.REVIEW_REQUIRED, "unmapped_runner_outcomes", notes
    if left_outcomes != required:
        return CatalogueApprovalState.REVIEW_REQUIRED, "incomplete_outcome_set", notes

    if not left.settlement.is_economically_complete() or not right.settlement.is_economically_complete():
        notes.append("incomplete_settlement_is_review_required_not_confidence")
        return CatalogueApprovalState.REVIEW_REQUIRED, "incomplete_settlement", notes
    if left.settlement.deterministic_key() != right.settlement.deterministic_key():
        return CatalogueApprovalState.KNOWN_CONTRADICTION, "settlement_key_mismatch", notes
    if left.family is MarketFamily.TOTAL_GOALS and line_push_possible(left.line) is None:
        return CatalogueApprovalState.REVIEW_REQUIRED, "unproven_split_line_push", notes
    if left.family is MarketFamily.TEAM_TOTAL:
        notes.append("canonical_market_has_no_team_scope_parameter")
        return (
            CatalogueApprovalState.REVIEW_REQUIRED,
            "team_scope_not_extracted_on_canonical_market",
            notes,
        )
    if left.family is MarketFamily.ASIAN_HANDICAP:
        return CatalogueApprovalState.UNSUPPORTED, "unproven_handicap_semantics", notes
    if left.family is MarketFamily.DOUBLE_CHANCE:
        return CatalogueApprovalState.UNSUPPORTED, "double_chance_solver_not_modelled", notes

    solver_model = solver_model_for_pair(left, right)
    if solver_model is None:
        return (
            CatalogueApprovalState.REVIEW_REQUIRED,
            "solver_cannot_model_settlement_states",
            notes,
        )
    return CatalogueApprovalState.APPROVED_EQUIVALENT, "approved_equivalent", notes
