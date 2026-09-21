"""Phase 1A season equivalence is observation-only / fail-closed.

No path here may emit APPROVED_EQUIVALENT or PAPER_ASSUMED_EQUIVALENT.
EventMatcher is never invoked.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.domain.outrights import (
    CanonicalSeasonMarketIdentity,
    OutrightSettlementFingerprint,
    SeasonEquivalenceState,
)
from sports_hedge.outrights.identity import identity_mismatch_reasons, settlement_mismatch_reasons

FORBIDDEN_ADMISSION_STATES = frozenset(
    {
        "APPROVED_EQUIVALENT",
        "PAPER_ASSUMED_EQUIVALENT",
        "approved_equivalent",
        "paper_assumed_equivalent",
    }
)


class SeasonIdentityComparison(BaseModel):
    state: SeasonEquivalenceState
    reasons: list[str] = Field(default_factory=list)
    identities_equal: bool = False
    register_status: str = "UNKNOWN"

    @property
    def admitted(self) -> bool:
        return False


def compare_season_identities(
    left: CanonicalSeasonMarketIdentity,
    right: CanonicalSeasonMarketIdentity,
    *,
    left_settlement: OutrightSettlementFingerprint | None = None,
    right_settlement: OutrightSettlementFingerprint | None = None,
) -> SeasonIdentityComparison:
    """Compare season identities without admitting cross-venue equivalence."""

    reasons = identity_mismatch_reasons(left, right)
    if left.subject.event_scope.value == "FIXTURE_MATCH" or right.subject.event_scope.value == "FIXTURE_MATCH":
        reasons.append("fixture_scope_not_an_outright")
    if left_settlement is not None and right_settlement is not None:
        reasons.extend(settlement_mismatch_reasons(left_settlement, right_settlement))
        if left_settlement.joint_winner_policy == "sole_winner_alpha_tiebreak" and (
            right_settlement.joint_winner_policy
            in {"dead_heat_equal_share", "official_shared_award"}
            or right_settlement.joint_winner_policy != left_settlement.joint_winner_policy
        ):
            if "top_scorer_alpha_tiebreak_vs_shared_or_dead_heat" not in reasons:
                if left.market_family.value == "top_scorer":
                    reasons.append("top_scorer_alpha_tiebreak_vs_shared_or_dead_heat")
        if "matchbook_outright_ded_fact_unproven" in {
            left_settlement.joint_winner_policy,
            right_settlement.joint_winner_policy,
        }:
            reasons.append("matchbook_outright_ded_fact_unproven")
    unique_reasons = list(dict.fromkeys(reasons))
    equal = not identity_mismatch_reasons(left, right)
    if unique_reasons:
        return SeasonIdentityComparison(
            state=SeasonEquivalenceState.FAIL_CLOSED,
            reasons=unique_reasons,
            identities_equal=equal,
            register_status="UNKNOWN",
        )
    return SeasonIdentityComparison(
        state=SeasonEquivalenceState.OBSERVATION_ONLY,
        reasons=["structural_candidate_not_approved_equivalent", "observation_only_no_register_admission"],
        identities_equal=True,
        register_status="UNKNOWN",
    )


def register_status_for_season_row() -> str:
    return "UNKNOWN"
