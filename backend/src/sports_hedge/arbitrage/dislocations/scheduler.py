from __future__ import annotations

from sports_hedge.arbitrage.dislocations.engine import evaluate_candidate
from sports_hedge.arbitrage.dislocations.models import (
    BurstPriorityDecision,
    RateBudget,
    ScanCandidate,
    ScanSchedule,
    ScheduledCandidate,
)


def schedule_scans(
    candidates: list[ScanCandidate],
    budget: RateBudget,
    *,
    max_quote_age_ms: int | None = None,
) -> ScanSchedule:
    """Deterministically rank candidates and consume at most ``budget`` scans."""

    if not budget.authorised_public_feeds_only:
        raise ValueError("Scheduler may only target authorised/public feeds")

    kwargs: dict[str, int] = {}
    if max_quote_age_ms is not None:
        kwargs["max_quote_age_ms"] = max_quote_age_ms

    decisions = [
        evaluate_candidate(candidate, budget=budget, **kwargs) for candidate in candidates
    ]
    ordered = sorted(decisions, key=_ranking_key)
    limit = budget.available_scans
    selected: list[ScheduledCandidate] = []
    deferred: list[ScheduledCandidate] = []
    for index, decision in enumerate(ordered, start=1):
        if len(selected) < limit:
            selected.append(
                ScheduledCandidate(
                    rank=index,
                    selected=True,
                    budget_reason="within_rate_budget",
                    decision=decision,
                )
            )
        else:
            deferred.append(
                ScheduledCandidate(
                    rank=index,
                    selected=False,
                    budget_reason="rate_budget_exhausted",
                    decision=decision,
                )
            )
    return ScanSchedule(
        selected=selected,
        deferred=deferred,
        budget_limit=limit,
        selected_count=len(selected),
    )


def _ranking_key(decision: BurstPriorityDecision) -> tuple[object, ...]:
    return (
        -decision.composite_score,
        decision.canonical_event_id,
        decision.canonical_market_id,
    )
