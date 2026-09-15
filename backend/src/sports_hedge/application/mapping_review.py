"""Human-in-the-loop mapping verification.

ChatGPT text never activates a rule. Explicit operator confirmation is required.
Ambiguous / not-verified verdicts persist the review only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.learned_rules import (
    MappingReviewCandidate,
    MappingRule,
    MappingRuleSource,
    MappingSideEvidence,
    MappingVerdict,
    infer_learned_rule,
    sanitize_mapping_payload,
)
from sports_hedge.matching.markets import MarketMatcher
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
        if verdict is MappingVerdict.VERIFIED:
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
        else:
            blocked = f"verdict_{verdict.value}_activates_nothing"

        if verdict is MappingVerdict.VERIFIED and not operator_confirmed:
            blocked = "explicit_operator_confirmation_required"

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
        if proposal.verdict is not MappingVerdict.VERIFIED:
            return proposal
        if proposal.proposed_rule is None:
            return proposal

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
            confirmed_at=datetime.now(UTC),
            activated_rule_id=saved.rule_id,
            review_id=proposal.review_id,
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

    def disable(self, rule_id: str, *, operator: str, revoke: bool = False) -> MappingRule:
        return self.store.disable_rule(rule_id, operator=operator, revoke=revoke)


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
    left_raw: dict[str, Any] | None = None,
    right_raw: dict[str, Any] | None = None,
) -> MappingReviewCandidate:
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
