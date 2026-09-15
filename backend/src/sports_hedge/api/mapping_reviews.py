"""Narrow mapping-verification review API.

This is the Wave B #169 seam. Opportunity Monitor layout (#168) owns final
placement of the Verify action. This module does not change current-market
aggregation (#165) or audit Age/sorting (#163).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from sports_hedge.application.mapping_review import (
    MappingPromptBundle,
    MappingReviewProposal,
    MappingReviewService,
)
from sports_hedge.matching.learned_rules import (
    MappingReviewCandidate,
    MappingRule,
    MappingRuleSource,
    MappingVerdict,
)
from sports_hedge.persistence.mapping_rules import (
    SqliteMappingRuleStore,
    get_mapping_rule_store,
)

router = APIRouter(prefix="/paper/mapping-reviews", tags=["mapping-reviews"])


class MappingPromptRequest(BaseModel):
    candidate: MappingReviewCandidate


class MappingInterpretRequest(BaseModel):
    candidate: MappingReviewCandidate
    operator: str = "operator"
    source: MappingRuleSource = MappingRuleSource.OPERATOR_MANUAL
    chatgpt_text: str | None = None
    manual_verdict: MappingVerdict | None = None
    review_id: str | None = None


class MappingConfirmRequest(MappingInterpretRequest):
    operator_confirmed: bool = False


class MappingDisableRequest(BaseModel):
    operator: str = "operator"
    revoke: bool = False


def get_mapping_review_service(
    store: SqliteMappingRuleStore = Depends(get_mapping_rule_store),
) -> MappingReviewService:
    return MappingReviewService(store)


@router.post("/prompt")
def build_mapping_prompt(
    request: MappingPromptRequest,
    service: MappingReviewService = Depends(get_mapping_review_service),
) -> MappingPromptBundle:
    return service.build_prompt(request.candidate)


@router.post("/interpret")
def interpret_mapping_review(
    request: MappingInterpretRequest,
    service: MappingReviewService = Depends(get_mapping_review_service),
) -> MappingReviewProposal:
    return service.interpret(
        request.candidate,
        operator=request.operator,
        source=request.source,
        chatgpt_text=request.chatgpt_text,
        manual_verdict=request.manual_verdict,
        operator_confirmed=False,
        review_id=request.review_id,
    )


@router.post("/confirm")
def confirm_mapping_review(
    request: MappingConfirmRequest,
    service: MappingReviewService = Depends(get_mapping_review_service),
) -> MappingReviewProposal:
    if not request.operator_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="explicit_operator_confirmation_required",
        )
    proposal = service.confirm(
        request.candidate,
        operator=request.operator,
        operator_confirmed=True,
        source=request.source,
        chatgpt_text=request.chatgpt_text,
        manual_verdict=request.manual_verdict,
        review_id=request.review_id,
    )
    if proposal.activation_blocked_reason and proposal.proposed_rule is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=proposal.activation_blocked_reason,
        )
    return proposal


@router.get("/rules")
def list_mapping_rules(
    include_disabled: bool = True,
    store: SqliteMappingRuleStore = Depends(get_mapping_rule_store),
) -> list[MappingRule]:
    return store.list_rules(include_disabled=include_disabled)


@router.get("/rules/{rule_id}")
def get_mapping_rule(
    rule_id: str,
    store: SqliteMappingRuleStore = Depends(get_mapping_rule_store),
) -> MappingRule:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mapping_rule_not_found")
    return rule


@router.post("/rules/{rule_id}/disable")
def disable_mapping_rule(
    rule_id: str,
    request: MappingDisableRequest,
    service: MappingReviewService = Depends(get_mapping_review_service),
) -> MappingRule:
    try:
        return service.disable(rule_id, operator=request.operator, revoke=request.revoke)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mapping_rule_not_found",
        ) from exc


@router.get("/{review_id}")
def get_mapping_review(
    review_id: str,
    store: SqliteMappingRuleStore = Depends(get_mapping_rule_store),
) -> dict[str, Any]:
    row = store.get_review(review_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mapping_review_not_found")
    return row
