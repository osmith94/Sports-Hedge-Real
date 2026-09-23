"""Canonical competition/season identity hashing and inequality (Phase 1A).

Fixture ``canonical_match_id`` / ``EventMatcher`` are not used. Prices,
liquidity, home/away, and kickoff are forbidden in these keys.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from pydantic import ValidationError

from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.outrights import (
    FORBIDDEN_FIXTURE_FIELDS,
    FORBIDDEN_PRICE_FIELDS,
    NAME_ONLY_PARTICIPANT_PREFIXES,
    CanonicalCompetitionSeasonRef,
    CanonicalSeasonMarketIdentity,
    OutrightMarketFamily,
    OutrightSettlementFingerprint,
    ParticipantType,
)
from sports_hedge.normalization.text import normalize_text


class SeasonIdentityError(ValueError):
    """Fail-closed season identity construction."""


def canonical_season_subject_id(subject: CanonicalCompetitionSeasonRef) -> str:
    payload = "|".join(
        [
            "competition_season",
            normalize_text(subject.sport),
            normalize_text(subject.competition_code),
            subject.season_id.strip(),
            subject.event_scope.value,
        ]
    )
    return f"season:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def canonical_season_market_id(identity: CanonicalSeasonMarketIdentity) -> str:
    """Stable id for one season market + participant + fingerprint version.

    Venue, prices, and fixture fields are never part of this key.
    """

    payload = "|".join(
        [
            canonical_season_subject_id(identity.subject),
            identity.market_family.value,
            identity.participant_type.value,
            identity.participant_canonical_id.strip(),
            identity.settlement_fingerprint_version.strip(),
            (identity.expected_settlement_horizon or "").strip(),
        ]
    )
    return f"seasonmkt:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def season_identities_equal(
    left: CanonicalSeasonMarketIdentity,
    right: CanonicalSeasonMarketIdentity,
) -> bool:
    return canonical_season_market_id(left) == canonical_season_market_id(right)


def identity_mismatch_reasons(
    left: CanonicalSeasonMarketIdentity,
    right: CanonicalSeasonMarketIdentity,
) -> list[str]:
    reasons: list[str] = []
    if left.subject.sport != right.subject.sport:
        reasons.append("sport_mismatch")
    if left.subject.competition_code != right.subject.competition_code:
        reasons.append("competition_mismatch")
    if left.subject.season_id != right.subject.season_id:
        reasons.append("season_mismatch")
    if left.subject.event_scope != right.subject.event_scope:
        reasons.append("event_scope_mismatch")
    if left.market_family != right.market_family:
        reasons.append("market_family_mismatch")
    if left.participant_type != right.participant_type:
        reasons.append("participant_type_mismatch")
    if left.participant_canonical_id != right.participant_canonical_id:
        reasons.append("participant_mismatch")
    if left.settlement_fingerprint_version != right.settlement_fingerprint_version:
        reasons.append("settlement_fingerprint_version_mismatch")
    if (left.expected_settlement_horizon or "") != (right.expected_settlement_horizon or ""):
        reasons.append("completion_horizon_mismatch")
    return reasons


def settlement_mismatch_reasons(
    left: OutrightSettlementFingerprint,
    right: OutrightSettlementFingerprint,
) -> list[str]:
    reasons: list[str] = []
    if not left.is_complete() or not right.is_complete():
        reasons.append("incomplete_settlement_fingerprint")
    if left.winner_uniqueness != right.winner_uniqueness:
        reasons.append("winner_uniqueness_mismatch")
    if left.joint_winner_policy != right.joint_winner_policy:
        reasons.append("joint_winner_policy_mismatch")
    if left.stat_scope != right.stat_scope:
        reasons.append("stat_scope_mismatch")
    if left.official_resolution_source != right.official_resolution_source:
        reasons.append("official_resolution_source_mismatch")
    if left.exceptional_policy != right.exceptional_policy:
        reasons.append("exceptional_policy_mismatch")
    if left.rule_version != right.rule_version:
        reasons.append("rule_version_mismatch")
    if left.completion_horizon != right.completion_horizon:
        reasons.append("completion_horizon_mismatch")
    return list(dict.fromkeys(reasons))


def reject_forbidden_identity_payload(payload: dict[str, Any]) -> None:
    keys = {str(key).strip().casefold() for key in payload}
    fixture = sorted(keys & {item.casefold() for item in FORBIDDEN_FIXTURE_FIELDS})
    prices = sorted(keys & {item.casefold() for item in FORBIDDEN_PRICE_FIELDS})
    if fixture:
        raise SeasonIdentityError(f"fixture_fields_forbidden_on_competition_season:{','.join(fixture)}")
    if prices:
        raise SeasonIdentityError(f"price_fields_forbidden_on_competition_season:{','.join(prices)}")


def season_ref_from_payload(payload: dict[str, Any]) -> CanonicalCompetitionSeasonRef:
    reject_forbidden_identity_payload(payload)
    try:
        return CanonicalCompetitionSeasonRef.model_validate(
            {
                "sport": payload["sport"],
                "competition_code": payload["competition_code"],
                "season_id": payload["season_id"],
                "event_scope": _coerce_event_scope(payload.get("event_scope")),
            }
        )
    except (KeyError, ValidationError, SeasonIdentityError) as exc:
        if isinstance(exc, SeasonIdentityError):
            raise
        raise SeasonIdentityError("invalid_competition_season_ref") from exc


def _coerce_event_scope(value: Any) -> MarketScope:
    text = str(value or MarketScope.COMPETITION_SEASON.value).strip()
    lowered = text.replace("-", "_").casefold()
    if lowered in {"fixture", "fixture_match"}:
        raise SeasonIdentityError("fixture_scope_not_an_outright")
    if lowered in {"competition_season", "competitionseason"}:
        return MarketScope.COMPETITION_SEASON
    return MarketScope(text)


def season_market_identity_from_payload(payload: dict[str, Any]) -> CanonicalSeasonMarketIdentity:
    identity_payload = dict(payload.get("identity") or payload)
    reject_forbidden_identity_payload(identity_payload)
    participant_id = str(identity_payload.get("participant_canonical_id") or "").strip()
    if participant_id.casefold().startswith(NAME_ONLY_PARTICIPANT_PREFIXES):
        raise SeasonIdentityError("name_only_participant_id_forbidden")
    try:
        return CanonicalSeasonMarketIdentity(
            subject=season_ref_from_payload(identity_payload),
            market_family=OutrightMarketFamily(str(identity_payload["market_family"]).strip()),
            participant_type=ParticipantType(str(identity_payload["participant_type"]).strip().upper()),
            participant_canonical_id=participant_id,
            settlement_fingerprint_version=str(
                identity_payload.get("settlement_fingerprint_version")
                or identity_payload.get("rule_version")
                or ""
            ).strip(),
            expected_settlement_horizon=(
                None
                if identity_payload.get("expected_settlement_horizon") in (None, "")
                else str(identity_payload["expected_settlement_horizon"]).strip()
            ),
        )
    except SeasonIdentityError:
        raise
    except (KeyError, ValidationError, ValueError) as exc:
        raise SeasonIdentityError("invalid_season_market_identity") from exc
