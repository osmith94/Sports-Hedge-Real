"""Deterministic operator-verified mapping rules.

Learned aliases are venue-scoped transformations applied inside the existing
EventMatcher / MarketMatcher seam. They are not a second identity system and
must never override settlement, period, line, or outcome-model mismatch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

from sports_hedge.application.target_competitions import resolve_target_competition
from sports_hedge.domain.football import CanonicalEvent, CanonicalMarket
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name
from sports_hedge.normalization.identity import kickoff_bucket
from sports_hedge.normalization.text import normalize_text

SAFE_TEAM_SUFFIX_TOKENS = frozenset({"fc", "cf", "afc", "sc"})
SAFE_TEAM_PREFIX_TOKENS = frozenset({"fc", "cf", "afc"})

SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "cookie",
    "bearer",
    "mfa",
    "username",
    "session",
)


class MappingRuleType(StrEnum):
    VENUE_SUFFIX_STRIP = "venue_suffix_strip"
    VENUE_PREFIX_STRIP = "venue_prefix_strip"
    VENUE_NAME_CONVENTION = "venue_name_convention"
    VENUE_MARKET_LABEL_CONVENTION = "venue_market_label_convention"
    FIXTURE_SPECIFIC_ALIAS = "fixture_specific_alias"


class MappingRuleSource(StrEnum):
    OPERATOR_VERIFIED_CHATGPT_ASSISTED = "operator_verified_chatgpt_assisted"
    OPERATOR_MANUAL = "operator_manual"


class MappingFieldScope(StrEnum):
    TEAM_NAME = "team_name"
    COMPETITION = "competition"
    EVENT_NAME = "event_name"
    MARKET_LABEL = "market_label"


class MappingVerdict(StrEnum):
    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    AMBIGUOUS = "ambiguous"


class MappingProvenanceSource(StrEnum):
    NATIVE_DETERMINISTIC = "native_deterministic"
    OPERATOR_VERIFIED = "operator_verified"


class MappingGuardrails(BaseModel):
    sport: str = "football"
    competition_code: str | None = None
    competition_label: str | None = None
    market_family: str | None = None
    period: str | None = None
    line: str | None = None
    settlement_key: str | None = None
    outcome_space: list[str] = Field(default_factory=list)
    kickoff_tolerance_seconds: int = 300

    @field_validator("sport")
    @classmethod
    def normalize_sport(cls, value: str) -> str:
        normalized = normalize_text(value) or "football"
        return normalized


class MappingProvenance(BaseModel):
    mapping_source: MappingProvenanceSource = MappingProvenanceSource.NATIVE_DETERMINISTIC
    rule_id: str | None = None
    rule_version: int | None = None
    rule_type: MappingRuleType | None = None
    applied_rule_ids: list[str] = Field(default_factory=list)

    def operator_verified(self) -> bool:
        return self.mapping_source is MappingProvenanceSource.OPERATOR_VERIFIED


class MappingRule(BaseModel):
    rule_id: str
    version: int = Field(default=1, ge=1)
    enabled: bool = True
    revoked: bool = False
    created_at: datetime
    updated_at: datetime | None = None
    operator: str
    source: MappingRuleSource
    rule_type: MappingRuleType
    venue: VenueName
    field_scope: MappingFieldScope
    raw_pattern: str
    canonical_transformation: str
    guardrails: MappingGuardrails
    evidence: str
    evidence_raw: dict[str, Any] = Field(default_factory=dict)
    review_id: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def normalize_times_and_pattern(self) -> "MappingRule":
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=UTC)
        if self.updated_at is not None and self.updated_at.tzinfo is None:
            self.updated_at = self.updated_at.replace(tzinfo=UTC)
        self.raw_pattern = self.raw_pattern.strip()
        self.canonical_transformation = self.canonical_transformation.strip()
        self.operator = self.operator.strip() or "unknown_operator"
        if self.revoked:
            self.enabled = False
        return self

    def is_active(self) -> bool:
        return self.enabled and not self.revoked


class AppliedLearnedRule(BaseModel):
    rule_id: str
    version: int
    rule_type: MappingRuleType
    venue: VenueName
    field_scope: MappingFieldScope
    original: str
    transformed: str


class MappingSideEvidence(BaseModel):
    venue: VenueName
    source_event_id: str
    source_market_id: str
    raw_event_name: str | None = None
    raw_home_team: str
    raw_away_team: str
    raw_competition: str
    kickoff_utc: datetime
    raw_market_name: str | None = None
    raw_market_type: str | None = None
    raw_runner_labels: list[str] = Field(default_factory=list)
    sport: str = "football"
    family: str | None = None
    period: str | None = None
    line: str | None = None
    settlement_key: str | None = None
    settlement_scope: str | None = None
    outcome_space: list[str] = Field(default_factory=list)
    current_canonical_candidate: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    match_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_kickoff_tz(self) -> "MappingSideEvidence":
        if self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        return self


class MappingReviewCandidate(BaseModel):
    sides: list[MappingSideEvidence] = Field(min_length=2, max_length=3)
    current_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    current_reasons: list[str] = Field(default_factory=list)
    current_matched: bool | None = None
    conflicting_fields: list[str] = Field(default_factory=list)


def stable_rule_id(
    *,
    venue: VenueName,
    rule_type: MappingRuleType,
    field_scope: MappingFieldScope,
    raw_pattern: str,
    guardrails: MappingGuardrails,
) -> str:
    payload = "|".join(
        [
            venue.value,
            rule_type.value,
            field_scope.value,
            normalize_text(raw_pattern),
            guardrails.sport,
            guardrails.competition_code or "",
            str(guardrails.kickoff_tolerance_seconds),
            guardrails.market_family or "",
            guardrails.period or "",
            guardrails.line or "",
            guardrails.settlement_key or "",
            ",".join(guardrails.outcome_space),
        ]
    )
    return f"maprule:{sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def competition_code_for(label: str) -> str | None:
    resolved = resolve_target_competition(label)
    if resolved is None:
        return None
    return resolved.code.value


def _tokens(value: str) -> list[str]:
    normalized = normalize_text(value)
    return normalized.split() if normalized else []


def _suffix_delta(raw: str, counterpart: str) -> str | None:
    left = _tokens(raw)
    right = _tokens(counterpart)
    if len(left) <= len(right) or left[: len(right)] != right:
        return None
    extra = left[len(right) :]
    if len(extra) != 1:
        return None
    return extra[0]


def _prefix_delta(raw: str, counterpart: str) -> str | None:
    left = _tokens(raw)
    right = _tokens(counterpart)
    if len(left) <= len(right) or left[-len(right) :] != right:
        return None
    extra = left[: len(left) - len(right)]
    if len(extra) != 1:
        return None
    return extra[0]


def _supplied_text(value: str | None) -> str:
    return normalize_text(value or "")


# Only these declared conflicting_fields may augment computed structural blockers.
# Naming / label / entity-alias discrepancies stay visible evidence but do not
# independently prevent VERIFIED + confirmed activation.
STRUCTURAL_BLOCKER_FIELDS = frozenset(
    {
        "sport",
        "competition",
        "kickoff",
        "market_family",
        "family",
        "period",
        "line",
        "settlement",
        "settlement_key",
        "settlement_scope",
        "outcome_space",
        "outcome_model",
    }
)
_STRUCTURAL_BLOCKER_ALIASES = {
    "family": "market_family",
    "settlement_key": "settlement",
    "outcome_model": "outcome_space",
}


def canonical_structural_blocker(field: str) -> str | None:
    key = field.strip().lower()
    if not key or key not in STRUCTURAL_BLOCKER_FIELDS:
        return None
    return _STRUCTURAL_BLOCKER_ALIASES.get(key, key)


def declared_structural_conflicts(fields: Sequence[str]) -> list[str]:
    found: list[str] = []
    for item in fields:
        canonical = canonical_structural_blocker(item or "")
        if canonical is not None:
            found.append(canonical)
    return found


def _optional_text_conflict(name: str, left: str | None, right: str | None) -> list[str]:
    left_text = _supplied_text(left)
    right_text = _supplied_text(right)
    if not left_text and not right_text:
        return []
    if left_text != right_text:
        return [name]
    return []


def structural_evidence_conflicts(
    left: MappingSideEvidence,
    right: MappingSideEvidence,
    *,
    kickoff_tolerance: timedelta = timedelta(minutes=5),
) -> list[str]:
    """Return structural mismatches that must block learned-rule activation.

    Naming evidence may be fuzzy. Competition, kickoff, and any supplied market
    family/period/line/settlement/outcome semantics must already agree.
    """

    conflicts: list[str] = []
    if _supplied_text(left.sport) != _supplied_text(right.sport):
        conflicts.append("sport")

    left_code = competition_code_for(left.raw_competition)
    right_code = competition_code_for(right.raw_competition)
    if left_code is not None or right_code is not None:
        if left_code != right_code:
            conflicts.append("competition")
    elif _supplied_text(left.raw_competition) != _supplied_text(right.raw_competition):
        conflicts.append("competition")

    kickoff_delta = abs((left.kickoff_utc - right.kickoff_utc).total_seconds())
    if kickoff_delta > kickoff_tolerance.total_seconds():
        conflicts.append("kickoff")

    conflicts.extend(_optional_text_conflict("market_family", left.family, right.family))
    conflicts.extend(_optional_text_conflict("period", left.period, right.period))
    conflicts.extend(_optional_text_conflict("line", left.line, right.line))
    conflicts.extend(_optional_text_conflict("settlement", left.settlement_key, right.settlement_key))
    conflicts.extend(
        _optional_text_conflict("settlement_scope", left.settlement_scope, right.settlement_scope)
    )

    left_outcomes = sorted({_supplied_text(item) for item in left.outcome_space if _supplied_text(item)})
    right_outcomes = sorted(
        {_supplied_text(item) for item in right.outcome_space if _supplied_text(item)}
    )
    if left_outcomes or right_outcomes:
        if left_outcomes != right_outcomes:
            conflicts.append("outcome_space")
    return conflicts


def candidate_structural_conflicts(
    candidate: MappingReviewCandidate,
    *,
    kickoff_tolerance: timedelta = timedelta(minutes=5),
) -> list[str]:
    found: list[str] = []
    sides = list(candidate.sides)
    for index, left in enumerate(sides):
        for right in sides[index + 1 :]:
            found.extend(
                structural_evidence_conflicts(left, right, kickoff_tolerance=kickoff_tolerance)
            )
    declared = declared_structural_conflicts(candidate.conflicting_fields)
    return sorted(set(found).union(declared))


def activation_block_reason(conflicts: Sequence[str]) -> str:
    unique = sorted({item for item in conflicts if item})
    return "structural_conflicts:" + ",".join(unique)


def infer_guardrails(
    left: MappingSideEvidence,
    right: MappingSideEvidence,
    *,
    kickoff_tolerance: timedelta = timedelta(minutes=5),
) -> MappingGuardrails:
    code = competition_code_for(left.raw_competition)
    if code is None:
        code = competition_code_for(right.raw_competition)
    left_outcomes = sorted({_supplied_text(item) for item in left.outcome_space if _supplied_text(item)})
    right_outcomes = sorted(
        {_supplied_text(item) for item in right.outcome_space if _supplied_text(item)}
    )
    outcomes = left_outcomes if left_outcomes == right_outcomes else []
    settlement = left.settlement_key if left.settlement_key == right.settlement_key else None
    family = left.family if left.family == right.family else None
    period = left.period if left.period == right.period else None
    line = left.line if left.line == right.line else None
    return MappingGuardrails(
        sport=left.sport or right.sport or "football",
        competition_code=code,
        competition_label=left.raw_competition,
        market_family=family,
        period=period,
        line=line,
        settlement_key=settlement,
        outcome_space=outcomes,
        kickoff_tolerance_seconds=int(kickoff_tolerance.total_seconds()),
    )


def infer_learned_rule(
    left: MappingSideEvidence,
    right: MappingSideEvidence,
    *,
    operator: str,
    source: MappingRuleSource,
    evidence: str,
    review_id: str | None = None,
    kickoff_tolerance: timedelta = timedelta(minutes=5),
) -> MappingRule | None:
    """Return the narrowest safe reusable venue-scoped rule supported by evidence."""

    if structural_evidence_conflicts(left, right, kickoff_tolerance=kickoff_tolerance):
        return None
    guardrails = infer_guardrails(left, right, kickoff_tolerance=kickoff_tolerance)
    created = datetime.now(UTC)

    def _build(
        *,
        venue: VenueName,
        rule_type: MappingRuleType,
        field_scope: MappingFieldScope,
        raw_pattern: str,
        canonical_transformation: str,
        notes: str,
    ) -> MappingRule:
        return MappingRule(
            rule_id=stable_rule_id(
                venue=venue,
                rule_type=rule_type,
                field_scope=field_scope,
                raw_pattern=raw_pattern,
                guardrails=guardrails,
            ),
            created_at=created,
            updated_at=created,
            operator=operator,
            source=source,
            rule_type=rule_type,
            venue=venue,
            field_scope=field_scope,
            raw_pattern=raw_pattern,
            canonical_transformation=canonical_transformation,
            guardrails=guardrails,
            evidence=evidence,
            evidence_raw={
                "left_venue": left.venue.value,
                "right_venue": right.venue.value,
                "left_source_event_id": left.source_event_id,
                "right_source_event_id": right.source_event_id,
            },
            review_id=review_id,
            notes=notes,
        )

    # Prefer transforming the venue whose labels carry extra tokens.
    for venue_side, counterpart in ((left, right), (right, left)):
        home_suffix = _suffix_delta(venue_side.raw_home_team, counterpart.raw_home_team)
        away_suffix = _suffix_delta(venue_side.raw_away_team, counterpart.raw_away_team)
        suffixes = {token for token in (home_suffix, away_suffix) if token is not None}
        if len(suffixes) == 1:
            token = next(iter(suffixes))
            if token in SAFE_TEAM_SUFFIX_TOKENS:
                return _build(
                    venue=venue_side.venue,
                    rule_type=MappingRuleType.VENUE_SUFFIX_STRIP,
                    field_scope=MappingFieldScope.TEAM_NAME,
                    raw_pattern=token,
                    canonical_transformation="strip_suffix",
                    notes="Reusable venue team-suffix strip inferred from both-or-one-side FC-style evidence.",
                )
        home_prefix = _prefix_delta(venue_side.raw_home_team, counterpart.raw_home_team)
        away_prefix = _prefix_delta(venue_side.raw_away_team, counterpart.raw_away_team)
        prefixes = {token for token in (home_prefix, away_prefix) if token is not None}
        if len(prefixes) == 1:
            token = next(iter(prefixes))
            if token in SAFE_TEAM_PREFIX_TOKENS:
                return _build(
                    venue=venue_side.venue,
                    rule_type=MappingRuleType.VENUE_PREFIX_STRIP,
                    field_scope=MappingFieldScope.TEAM_NAME,
                    raw_pattern=token,
                    canonical_transformation="strip_prefix",
                    notes="Reusable venue team-prefix strip inferred from evidence.",
                )

    left_home = normalize_text(left.raw_home_team)
    right_home = normalize_text(right.raw_home_team)
    left_away = normalize_text(left.raw_away_team)
    right_away = normalize_text(right.raw_away_team)
    if left_home != right_home or left_away != right_away:
        kickoff = kickoff_bucket(left.kickoff_utc).isoformat()
        pattern = (
            f"home={left_home}|away={left_away}|kickoff={kickoff}|src={left.source_event_id}"
        )
        transformation = f"home={right_home}|away={right_away}"
        return _build(
            venue=left.venue,
            rule_type=MappingRuleType.FIXTURE_SPECIFIC_ALIAS,
            field_scope=MappingFieldScope.TEAM_NAME,
            raw_pattern=pattern,
            canonical_transformation=transformation,
            notes="Fallback fixture-specific alias; no safe reusable venue suffix/prefix was supported by evidence.",
        )

    left_label = normalize_text(left.raw_market_name or "")
    right_label = normalize_text(right.raw_market_name or "")
    if left_label and right_label and left_label != right_label:
        if left.family and left.family == right.family:
            return _build(
                venue=left.venue,
                rule_type=MappingRuleType.VENUE_MARKET_LABEL_CONVENTION,
                field_scope=MappingFieldScope.MARKET_LABEL,
                raw_pattern=left_label,
                canonical_transformation=right_label,
                notes="Venue market-label convention under already-equivalent family/period/settlement evidence.",
            )
    return None


def apply_text_rule(rule: MappingRule, value: str) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return normalized
    if rule.rule_type is MappingRuleType.VENUE_SUFFIX_STRIP:
        suffix = normalize_text(rule.raw_pattern)
        if suffix and normalized.endswith(f" {suffix}"):
            stripped = normalized[: -(len(suffix) + 1)].strip()
            return stripped or normalized
        return normalized
    if rule.rule_type is MappingRuleType.VENUE_PREFIX_STRIP:
        prefix = normalize_text(rule.raw_pattern)
        if prefix and normalized.startswith(f"{prefix} "):
            stripped = normalized[len(prefix) + 1 :].strip()
            return stripped or normalized
        return normalized
    if rule.rule_type is MappingRuleType.VENUE_NAME_CONVENTION:
        source = normalize_text(rule.raw_pattern)
        target = normalize_text(rule.canonical_transformation)
        if normalized == source and target:
            return target
        return normalized
    return normalized


def participant_identity_preserved(original: str, transformed: str) -> bool:
    """True when a learned rename is a naming variant, not a different club.

    Operator mappings may strip safe FC-style tokens or apply curated aliases.
    They must not replace Arsenal with Chelsea (or any disjoint participant).
    """

    left = resolve_team_name(original)
    right = resolve_team_name(transformed)
    if not left and not right:
        return True
    if left == right:
        return True
    left_tokens = set(_tokens(left)) - SAFE_TEAM_SUFFIX_TOKENS - SAFE_TEAM_PREFIX_TOKENS
    right_tokens = set(_tokens(right)) - SAFE_TEAM_SUFFIX_TOKENS - SAFE_TEAM_PREFIX_TOKENS
    if left_tokens and right_tokens and (left_tokens <= right_tokens or right_tokens <= left_tokens):
        return True
    if len(left) >= 4 and len(right) >= 4 and (left in right or right in left):
        return True
    return False


def _parse_fixture_fields(payload: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in payload.split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


class LearnedMappingApplicator:
    """Apply enabled learned rules to venue labels before native matching."""

    def __init__(
        self,
        store: Any | None = None,
        *,
        rules: Sequence[MappingRule] | None = None,
    ) -> None:
        self._store = store
        self._fixed_rules = list(rules) if rules is not None else None

    def enabled_rules(self) -> list[MappingRule]:
        if self._fixed_rules is not None:
            return [rule for rule in self._fixed_rules if rule.is_active()]
        if self._store is None:
            return []
        return list(self._store.list_enabled())

    def resolve_teams(
        self,
        event: CanonicalEvent,
        counterpart: CanonicalEvent,
        *,
        market: CanonicalMarket | None = None,
        counterpart_market: CanonicalMarket | None = None,
    ) -> tuple[str, str, list[AppliedLearnedRule]]:
        home = event.home_team
        away = event.away_team
        applied: list[AppliedLearnedRule] = []
        for rule in _prioritized(self.enabled_rules()):
            if not self._team_rule_applies(rule, event, counterpart, market, counterpart_market):
                continue
            new_home = self._apply_team(rule, event, "home", home)
            new_away = self._apply_team(rule, event, "away", away)
            if not participant_identity_preserved(home, new_home) or not participant_identity_preserved(
                away, new_away
            ):
                continue
            if new_home != normalize_text(home) or new_away != normalize_text(away):
                applied.append(
                    AppliedLearnedRule(
                        rule_id=rule.rule_id,
                        version=rule.version,
                        rule_type=rule.rule_type,
                        venue=rule.venue,
                        field_scope=rule.field_scope,
                        original=f"{home}|{away}",
                        transformed=f"{new_home}|{new_away}",
                    )
                )
                home, away = new_home, new_away
        return resolve_team_name(home), resolve_team_name(away), applied

    def _apply_team(self, rule: MappingRule, event: CanonicalEvent, side: str, current: str) -> str:
        if rule.rule_type is MappingRuleType.FIXTURE_SPECIFIC_ALIAS:
            pattern = _parse_fixture_fields(rule.raw_pattern)
            transformation = _parse_fixture_fields(rule.canonical_transformation)
            home_ok = normalize_text(event.home_team) == pattern.get("home", "")
            away_ok = normalize_text(event.away_team) == pattern.get("away", "")
            src_ok = pattern.get("src") in {None, "", event.source_event_id}
            kickoff_ok = True
            expected_kickoff = pattern.get("kickoff")
            if expected_kickoff:
                kickoff_ok = kickoff_bucket(event.kickoff_utc).isoformat() == expected_kickoff
            if not (home_ok and away_ok and src_ok and kickoff_ok):
                return normalize_text(current)
            mapped = transformation.get(side)
            if not mapped:
                return normalize_text(current)
            if not participant_identity_preserved(current, mapped):
                return normalize_text(current)
            return mapped
        return apply_text_rule(rule, current)

    def _team_rule_applies(
        self,
        rule: MappingRule,
        event: CanonicalEvent,
        counterpart: CanonicalEvent,
        market: CanonicalMarket | None,
        counterpart_market: CanonicalMarket | None,
    ) -> bool:
        if not rule.is_active():
            return False
        if rule.venue is not event.source_venue:
            return False
        if rule.field_scope is MappingFieldScope.MARKET_LABEL:
            return False
        if rule.field_scope not in {MappingFieldScope.TEAM_NAME, MappingFieldScope.EVENT_NAME}:
            if rule.field_scope is MappingFieldScope.COMPETITION:
                return self._structural_event_guardrails(rule, event, counterpart)
            return False
        if not self._structural_event_guardrails(rule, event, counterpart):
            return False
        # Market-label-only rules never reach here. Team naming rules may still
        # apply when market context later fails settlement — MarketMatcher reports
        # the economic mismatch instead of hiding it behind event_mismatch.
        del market, counterpart_market
        return True

    @staticmethod
    def _structural_event_guardrails(
        rule: MappingRule,
        event: CanonicalEvent,
        counterpart: CanonicalEvent,
    ) -> bool:
        if normalize_text(event.sport) != rule.guardrails.sport:
            return False
        if rule.guardrails.competition_code:
            event_code = competition_code_for(event.competition)
            counterpart_code = competition_code_for(counterpart.competition)
            if event_code != rule.guardrails.competition_code:
                return False
            if counterpart_code != rule.guardrails.competition_code:
                return False
        delta = abs((event.kickoff_utc - counterpart.kickoff_utc).total_seconds())
        if delta > rule.guardrails.kickoff_tolerance_seconds:
            return False
        return True


def _prioritized(rules: Sequence[MappingRule]) -> list[MappingRule]:
    rank = {
        MappingRuleType.VENUE_SUFFIX_STRIP: 0,
        MappingRuleType.VENUE_PREFIX_STRIP: 1,
        MappingRuleType.VENUE_NAME_CONVENTION: 2,
        MappingRuleType.VENUE_MARKET_LABEL_CONVENTION: 3,
        MappingRuleType.FIXTURE_SPECIFIC_ALIAS: 4,
    }
    return sorted(rules, key=lambda rule: (rank.get(rule.rule_type, 9), rule.created_at, rule.rule_id))


def provenance_from_applied(applied: Sequence[AppliedLearnedRule]) -> MappingProvenance:
    if not applied:
        return MappingProvenance()
    first = applied[0]
    return MappingProvenance(
        mapping_source=MappingProvenanceSource.OPERATOR_VERIFIED,
        rule_id=first.rule_id,
        rule_version=first.version,
        rule_type=first.rule_type,
        applied_rule_ids=[item.rule_id for item in applied],
    )


def economic_mismatch_reasons(left: CanonicalMarket, right: CanonicalMarket) -> list[str]:
    reasons: list[str] = []
    if left.family != right.family:
        reasons.append("market_family_mismatch")
    if left.period != right.period:
        reasons.append("period_mismatch")
    if left.line != right.line:
        reasons.append("line_mismatch")
    left_complete = left.settlement.is_economically_complete()
    right_complete = right.settlement.is_economically_complete()
    if not left_complete or not right_complete:
        reasons.append("incomplete_settlement")
    elif left.settlement.deterministic_key() != right.settlement.deterministic_key():
        reasons.append("settlement_mismatch")
    left_outcomes = {runner.outcome for runner in left.runners}
    right_outcomes = {runner.outcome for runner in right.runners}
    if left_outcomes != right_outcomes:
        reasons.append("outcome_space_mismatch")
    return reasons


def key_looks_secret(key: str) -> bool:
    lowered = normalize_text(key).replace(" ", "_")
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def sanitize_mapping_payload(value: Any) -> Any:
    """Drop credentials/secrets from prompt/review payloads."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key_looks_secret(str(key)):
                continue
            cleaned[str(key)] = sanitize_mapping_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_mapping_payload(item) for item in value]
    if isinstance(value, str):
        lowered = value.casefold()
        if any(fragment in lowered for fragment in ("bearer ", "api_key=", "password=")):
            return "[redacted]"
        return value
    return value
