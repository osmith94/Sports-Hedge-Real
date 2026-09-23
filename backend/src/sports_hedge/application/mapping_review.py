"""Human-in-the-loop mapping verification.

ChatGPT text never activates a rule. Explicit operator confirmation is required.
Ambiguous / not-verified verdicts persist the review only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.matching.learned_rules import (
    MappingReviewCandidate,
    MappingRule,
    MappingRuleSource,
    MappingSideEvidence,
    MappingVerdict,
    activation_block_reason,
    candidate_structural_conflicts,
    infer_learned_rule,
    sanitize_mapping_payload,
)
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.persistence.mapping_rules import SqliteMappingRuleStore

_VERDICT_PATTERN = re.compile(
    r"\b(NOT\s+VERIFIED|VERIFIED|AMBIGUOUS)\b",
    re.IGNORECASE,
)


class MappingPromptBundle(BaseModel):
    prompt_text: str
    candidate: MappingReviewCandidate
    excluded_secret_fields: list[str] = Field(default_factory=list)


class MappingReviewProposal(BaseModel):
    review_id: str
    verdict: MappingVerdict
    operator_confirmed: bool = False
    proposed_rule: MappingRule | None = None
    prompt_text: str
    chatgpt_text: str | None = None
    activation_blocked_reason: str | None = None


class MappingReviewService:
    def __init__(self, store: SqliteMappingRuleStore) -> None:
        self.store = store

    def build_prompt(self, candidate: MappingReviewCandidate) -> MappingPromptBundle:
        sanitized = MappingReviewCandidate.model_validate(
            sanitize_mapping_payload(candidate.model_dump(mode="json"))
        )
        excluded = _secret_fields_dropped(candidate.model_dump(mode="json"))
        lines = [
            "You are assisting a Sports Hedge operator with canonical football market equivalence.",
            "Do not invent missing settlement semantics. Fail closed if unsure.",
            "Are these venue markets the same football event/settlement?",
            "Return exactly one of: VERIFIED / NOT VERIFIED / AMBIGUOUS.",
            "If VERIFIED, propose the narrowest deterministic venue-scoped alias/rule",
            "(suffix/prefix/name convention under competition/time/market-family/period/line/settlement guardrails).",
            "Do not propose a fuzzy join. Do not override settlement/period/line/outcome mismatch.",
            "",
        ]
        if sanitized.current_confidence is not None:
            lines.append(f"Current mapping confidence: {sanitized.current_confidence:.4f}")
        if sanitized.current_matched is not None:
            lines.append(f"Current matched: {sanitized.current_matched}")
        if sanitized.current_reasons:
            lines.append("Current match reasons: " + ", ".join(sanitized.current_reasons))
        if sanitized.conflicting_fields:
            lines.append("Conflicting/missing fields: " + ", ".join(sanitized.conflicting_fields))
        lines.append("")
        for index, side in enumerate(sanitized.sides, start=1):
            label = chr(ord("A") + index - 1)
            lines.extend(
                [
                    f"Venue {label}: {side.venue.value}",
                    f"  source_event_id: {side.source_event_id}",
                    f"  source_market_id: {side.source_market_id}",
                    f"  raw_event_name: {side.raw_event_name or ''}",
                    f"  raw_home_team: {side.raw_home_team}",
                    f"  raw_away_team: {side.raw_away_team}",
                    f"  raw_competition: {side.raw_competition}",
                    f"  kickoff_utc: {side.kickoff_utc.isoformat()}",
                    f"  raw_market_name: {side.raw_market_name or ''}",
                    f"  raw_market_type: {side.raw_market_type or ''}",
                    f"  raw_runner_labels: {', '.join(side.raw_runner_labels)}",
                    f"  normalized_family: {side.family or ''}",
                    f"  period: {side.period or ''}",
                    f"  line: {side.line or ''}",
                    f"  settlement_key: {side.settlement_key or ''}",
                    f"  settlement_scope: {side.settlement_scope or ''}",
                    f"  outcome_space: {', '.join(side.outcome_space)}",
                    f"  current_canonical_candidate: {side.current_canonical_candidate or ''}",
                    f"  side_confidence: {'' if side.confidence is None else f'{side.confidence:.4f}'}",
                    f"  side_reasons: {', '.join(side.match_reasons)}",
                    "",
                ]
            )
        lines.append("Answer with VERIFIED, NOT VERIFIED, or AMBIGUOUS, then a one-line proposed rule if VERIFIED.")
        return MappingPromptBundle(
            prompt_text="\n".join(lines).strip() + "\n",
            candidate=sanitized,
            excluded_secret_fields=excluded,
        )

    def interpret(
        self,
        candidate: MappingReviewCandidate,
        *,
        operator: str,
        source: MappingRuleSource = MappingRuleSource.OPERATOR_MANUAL,
        chatgpt_text: str | None = None,
        manual_verdict: MappingVerdict | None = None,
        operator_confirmed: bool = False,
        review_id: str | None = None,
    ) -> MappingReviewProposal:
        prompt = self.build_prompt(candidate)
        verdict = manual_verdict
        if verdict is None:
            verdict = parse_chatgpt_verdict(chatgpt_text)
        if verdict is None:
            verdict = MappingVerdict.AMBIGUOUS
        if chatgpt_text and source is MappingRuleSource.OPERATOR_MANUAL:
            source = MappingRuleSource.OPERATOR_VERIFIED_CHATGPT_ASSISTED

        proposed: MappingRule | None = None
        blocked: str | None = None
        structural_conflicts = candidate_structural_conflicts(candidate)
        if verdict is MappingVerdict.VERIFIED:
            if structural_conflicts:
                blocked = activation_block_reason(structural_conflicts)
            else:
                proposed = infer_learned_rule(
                    candidate.sides[0],
                    candidate.sides[1],
                    operator=operator,
                    source=source,
                    evidence=chatgpt_text or "operator_manual_evidence",
                    review_id=review_id,
                )
                if proposed is None:
                    blocked = "no_safe_reusable_or_fixture_rule_from_evidence"
                elif not operator_confirmed:
                    blocked = "explicit_operator_confirmation_required"
        else:
            blocked = f"verdict_{verdict.value}_activates_nothing"

        review = self.store.save_review(
            operator=operator,
            source=source,
            prompt_text=prompt.prompt_text,
            evidence=sanitize_mapping_payload(candidate.model_dump(mode="json")),
            verdict=verdict,
            chatgpt_text=chatgpt_text,
            proposed_rule=proposed,
            confirmed_at=None,
            activated_rule_id=None,
            review_id=review_id,
            activation_blocked_reason=blocked,
        )
        return MappingReviewProposal(
            review_id=review["review_id"],
            verdict=verdict,
            operator_confirmed=False,
            proposed_rule=proposed,
            prompt_text=prompt.prompt_text,
            chatgpt_text=chatgpt_text,
            activation_blocked_reason=blocked,
        )

    def confirm(
        self,
        candidate: MappingReviewCandidate,
        *,
        operator: str,
        operator_confirmed: bool,
        source: MappingRuleSource = MappingRuleSource.OPERATOR_MANUAL,
        chatgpt_text: str | None = None,
        manual_verdict: MappingVerdict | None = None,
        review_id: str | None = None,
    ) -> MappingReviewProposal:
        mismatched = self._reject_changed_review_evidence(candidate, review_id=review_id)
        if mismatched is not None:
            return mismatched
        proposal = self.interpret(
            candidate,
            operator=operator,
            source=source,
            chatgpt_text=chatgpt_text,
            manual_verdict=manual_verdict,
            operator_confirmed=operator_confirmed,
            review_id=review_id,
        )
        if not operator_confirmed:
            return proposal
        confirmed_at = datetime.now(UTC)
        if (
            proposal.verdict is not MappingVerdict.VERIFIED
            or proposal.proposed_rule is None
            or proposal.activation_blocked_reason is not None
        ):
            self.store.save_review(
                operator=operator,
                source=source,
                prompt_text=proposal.prompt_text,
                evidence=sanitize_mapping_payload(candidate.model_dump(mode="json")),
                verdict=proposal.verdict,
                chatgpt_text=chatgpt_text,
                proposed_rule=None,
                confirmed_at=confirmed_at,
                activated_rule_id=None,
                review_id=proposal.review_id,
                activation_blocked_reason=proposal.activation_blocked_reason,
            )
            return MappingReviewProposal(
                review_id=proposal.review_id,
                verdict=proposal.verdict,
                operator_confirmed=True,
                proposed_rule=None,
                prompt_text=proposal.prompt_text,
                chatgpt_text=chatgpt_text,
                activation_blocked_reason=proposal.activation_blocked_reason,
            )

        rule = proposal.proposed_rule.model_copy(
            update={"review_id": proposal.review_id, "enabled": True, "revoked": False}
        )
        saved = self.store.save_rule(rule, action="created")
        self.store.save_review(
            operator=operator,
            source=saved.source,
            prompt_text=proposal.prompt_text,
            evidence=sanitize_mapping_payload(candidate.model_dump(mode="json")),
            verdict=proposal.verdict,
            chatgpt_text=chatgpt_text,
            proposed_rule=saved,
            confirmed_at=confirmed_at,
            activated_rule_id=saved.rule_id,
            review_id=proposal.review_id,
            activation_blocked_reason=None,
        )
        return MappingReviewProposal(
            review_id=proposal.review_id,
            verdict=proposal.verdict,
            operator_confirmed=True,
            proposed_rule=saved,
            prompt_text=proposal.prompt_text,
            chatgpt_text=chatgpt_text,
            activation_blocked_reason=None,
        )

    def _reject_changed_review_evidence(
        self,
        candidate: MappingReviewCandidate,
        *,
        review_id: str | None,
    ) -> MappingReviewProposal | None:
        """Fail closed if Confirm tries to reuse a review against different evidence."""

        if not review_id:
            return None
        existing = self.store.get_review(review_id)
        if existing is None:
            return None
        stored = _stored_review_evidence(existing)
        incoming = sanitize_mapping_payload(candidate.model_dump(mode="json"))
        if stored == incoming:
            return None
        verdict = MappingVerdict.AMBIGUOUS
        raw_verdict = existing.get("verdict")
        if raw_verdict:
            try:
                verdict = MappingVerdict(raw_verdict)
            except ValueError:
                verdict = MappingVerdict.AMBIGUOUS
        return MappingReviewProposal(
            review_id=review_id,
            verdict=verdict,
            operator_confirmed=False,
            proposed_rule=None,
            prompt_text=existing.get("prompt_text") or "",
            chatgpt_text=existing.get("chatgpt_text"),
            activation_blocked_reason="review_evidence_changed",
        )

    def disable(self, rule_id: str, *, operator: str, revoke: bool = False) -> MappingRule:
        return self.store.disable_rule(rule_id, operator=operator, revoke=revoke)


def _stored_review_evidence(row: dict[str, Any]) -> Any:
    raw = row.get("evidence_json")
    if not raw:
        return None
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def parse_chatgpt_verdict(text: str | None) -> MappingVerdict | None:
    if not text or not text.strip():
        return None
    match = _VERDICT_PATTERN.search(text)
    if match is None:
        return MappingVerdict.AMBIGUOUS
    token = re.sub(r"\s+", " ", match.group(1).upper())
    if token == "VERIFIED":
        return MappingVerdict.VERIFIED
    if token == "NOT VERIFIED":
        return MappingVerdict.NOT_VERIFIED
    return MappingVerdict.AMBIGUOUS


def evidence_from_markets(
    left: CanonicalMarket,
    right: CanonicalMarket,
    *,
    matcher: MarketMatcher | None = None,
    match: MarketMatchResult | None = None,
    left_raw: dict[str, Any] | None = None,
    right_raw: dict[str, Any] | None = None,
) -> MappingReviewCandidate:
    if match is None:
        matcher = matcher or MarketMatcher()
        match = matcher.match(left, right)
    left_side = _side_from_market(left, raw=left_raw, match_reasons=match.reasons, confidence=match.confidence)
    right_side = _side_from_market(right, raw=right_raw, match_reasons=match.reasons, confidence=match.confidence)
    conflicts: list[str] = []
    if left.family != right.family:
        conflicts.append("market_family")
    if left.period != right.period:
        conflicts.append("period")
    if left.line != right.line:
        conflicts.append("line")
    if left.settlement.deterministic_key() != right.settlement.deterministic_key():
        conflicts.append("settlement")
    return MappingReviewCandidate(
        sides=[left_side, right_side],
        current_confidence=match.confidence,
        current_reasons=match.reasons,
        current_matched=match.matched,
        conflicting_fields=conflicts,
    )


def safe_mapping_review_candidate(
    candidate: MappingReviewCandidate | None,
) -> MappingReviewCandidate | None:
    """Return a candidate only when both venue sides are present for Verify."""

    if candidate is None or len(candidate.sides) < 2:
        return None
    return candidate


def evidence_from_snapshots(
    history: Sequence[MarketSnapshot],
    match: MarketMatchResult,
) -> MappingReviewCandidate | None:
    """Build current mapping-review evidence from current-market snapshots.

    This uses Fixture/MI current observations, never the append-only /paper/scans
    audit window. Incomplete sides fail closed to no candidate.
    """

    latest_by_venue: dict[str, MarketSnapshot] = {}
    outcomes_by_venue: dict[str, list[str]] = {}
    for snapshot in history:
        key = snapshot.venue.value
        previous = latest_by_venue.get(key)
        if previous is None or snapshot.observed_at >= previous.observed_at:
            latest_by_venue[key] = snapshot
        outcomes = outcomes_by_venue.setdefault(key, [])
        if snapshot.canonical_outcome not in outcomes:
            outcomes.append(snapshot.canonical_outcome)
    sides: list[MappingSideEvidence] = []
    for venue_key, snapshot in latest_by_venue.items():
        side = _side_from_snapshot(
            snapshot,
            match_reasons=match.reasons,
            confidence=match.confidence,
            outcome_space=outcomes_by_venue.get(venue_key, []),
        )
        if side is not None:
            sides.append(side)
    if len(sides) < 2:
        return None
    conflicts: list[str] = []
    left, right = sides[0], sides[1]
    if left.family and right.family and left.family != right.family:
        conflicts.append("market_family")
    if left.period and right.period and left.period != right.period:
        conflicts.append("period")
    if (left.line or "") != (right.line or ""):
        conflicts.append("line")
    if left.settlement_key and right.settlement_key and left.settlement_key != right.settlement_key:
        conflicts.append("settlement")
    return MappingReviewCandidate(
        sides=sides[:3],
        current_confidence=match.confidence,
        current_reasons=list(match.reasons),
        current_matched=match.matched,
        conflicting_fields=conflicts,
    )


def _side_from_snapshot(
    snapshot: MarketSnapshot,
    *,
    match_reasons: list[str],
    confidence: float,
    outcome_space: list[str],
) -> MappingSideEvidence | None:
    if not snapshot.source_event_id or not snapshot.source_market_id:
        return None
    if not snapshot.home_team or not snapshot.away_team:
        return None
    if snapshot.kickoff_utc is None:
        return None
    raw = sanitize_mapping_payload(snapshot.metadata if isinstance(snapshot.metadata, dict) else {})
    event_name = None
    market_name = None
    market_type = None
    runner_labels: list[str] = []
    if isinstance(raw, dict):
        event_name = _first_str(raw, ("name", "title", "event_name", "raw_event_name"))
        market_name = _first_str(raw, ("market_name", "question", "raw_market_name"))
        market_type = _first_str(raw, ("market_type", "sportsMarketType", "raw_market_type"))
        labels = raw.get("raw_runner_labels")
        if isinstance(labels, list):
            runner_labels = [str(item) for item in labels if str(item).strip()]
    return MappingSideEvidence(
        venue=snapshot.venue,
        source_event_id=snapshot.source_event_id,
        source_market_id=snapshot.source_market_id,
        raw_event_name=event_name,
        raw_home_team=snapshot.home_team,
        raw_away_team=snapshot.away_team,
        raw_competition=snapshot.competition or "",
        kickoff_utc=snapshot.kickoff_utc,
        raw_market_name=market_name,
        raw_market_type=market_type,
        raw_runner_labels=runner_labels,
        sport="football",
        family=snapshot.market_family.value,
        period=snapshot.period.value,
        line="" if snapshot.market_line is None else format(snapshot.market_line, "f"),
        settlement_key=snapshot.settlement_key,
        settlement_scope=snapshot.settlement_scope.value,
        outcome_space=outcome_space,
        current_canonical_candidate=f"{snapshot.home_team} vs {snapshot.away_team}",
        confidence=confidence,
        match_reasons=match_reasons,
    )


def _side_from_market(
    market: CanonicalMarket,
    *,
    raw: dict[str, Any] | None,
    match_reasons: list[str],
    confidence: float,
) -> MappingSideEvidence:
    raw = sanitize_mapping_payload(raw or {})
    event_name = None
    market_name = None
    market_type = None
    if isinstance(raw, dict):
        event_name = _first_str(raw, ("name", "title", "event_name", "raw_event_name"))
        market_name = _first_str(raw, ("market_name", "question", "raw_market_name"))
        market_type = _first_str(raw, ("market_type", "sportsMarketType", "raw_market_type"))
    return MappingSideEvidence(
        venue=market.source_venue,
        source_event_id=market.event.source_event_id,
        source_market_id=market.source_market_id,
        raw_event_name=event_name,
        raw_home_team=market.event.home_team,
        raw_away_team=market.event.away_team,
        raw_competition=market.event.competition,
        kickoff_utc=market.event.kickoff_utc,
        raw_market_name=market_name,
        raw_market_type=market_type,
        raw_runner_labels=[runner.label for runner in market.runners],
        sport=market.event.sport,
        family=market.family.value,
        period=market.period.value,
        line="" if market.line is None else format(market.line, "f"),
        settlement_key=market.settlement.deterministic_key(),
        settlement_scope=market.settlement.scope.value,
        outcome_space=sorted(runner.outcome.value for runner in market.runners),
        current_canonical_candidate=f"{market.event.home_team} vs {market.event.away_team}",
        confidence=confidence,
        match_reasons=match_reasons,
    )


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _secret_fields_dropped(payload: Any, prefix: str = "") -> list[str]:
    dropped: list[str] = []
    if isinstance(payload, dict):
        from sports_hedge.matching.learned_rules import key_looks_secret

        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key_looks_secret(str(key)):
                dropped.append(path)
                continue
            dropped.extend(_secret_fields_dropped(value, path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            dropped.extend(_secret_fields_dropped(item, f"{prefix}[{index}]"))
    return dropped
