from __future__ import annotations

import json
from hashlib import sha256

from sports_hedge.event_intelligence.errors import EventIntelligenceMappingError
from sports_hedge.event_intelligence.models import (
    EventIntelligenceFact,
    EventIntelligenceQualityFlag,
    EventIntelligenceRecord,
    EventIntelligenceType,
    EventPhase,
    EventSubject,
    ProvenanceClass,
    SourceKind,
    VerificationStatus,
)
from sports_hedge.normalization.text import normalize_text

_TYPE_ALIASES: dict[str, EventIntelligenceType] = {
    "team_sheet_released": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "team sheet released": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "team_sheet": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "team sheet": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "teamsheet": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "starting xi": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "startingxi": EventIntelligenceType.TEAM_SHEET_RELEASED,
    "player_out": EventIntelligenceType.PLAYER_OUT,
    "player out": EventIntelligenceType.PLAYER_OUT,
    "omitted": EventIntelligenceType.PLAYER_OUT,
    "player_in": EventIntelligenceType.PLAYER_IN,
    "player in": EventIntelligenceType.PLAYER_IN,
    "injury_news": EventIntelligenceType.INJURY_NEWS,
    "injury news": EventIntelligenceType.INJURY_NEWS,
    "injury": EventIntelligenceType.INJURY_NEWS,
    "manager_news": EventIntelligenceType.MANAGER_NEWS,
    "manager news": EventIntelligenceType.MANAGER_NEWS,
    "lineup_change": EventIntelligenceType.LINEUP_CHANGE,
    "lineup change": EventIntelligenceType.LINEUP_CHANGE,
    "journalist_report": EventIntelligenceType.JOURNALIST_REPORT,
    "journalist report": EventIntelligenceType.JOURNALIST_REPORT,
    "news_article": EventIntelligenceType.NEWS_ARTICLE,
    "news article": EventIntelligenceType.NEWS_ARTICLE,
    "club_announcement": EventIntelligenceType.CLUB_ANNOUNCEMENT,
    "club announcement": EventIntelligenceType.CLUB_ANNOUNCEMENT,
}

_AMBIGUOUS_TYPES: dict[str, str] = {
    "lineup": "team_sheet_released versus lineup_change",
    "lineups": "team_sheet_released versus lineup_change",
    "news": "journalist_report versus news_article versus manager_news",
    "report": "journalist_report versus news_article",
    "card": "in-play match fact; not event-intelligence news",
    "red_card": "in-play match fact; not event-intelligence news",
    "yellow_card": "in-play match fact; not event-intelligence news",
    "goal": "in-play match fact; not event-intelligence news",
    "penalty": "in-play match fact; not event-intelligence news",
    "substitution": "in-play match fact; not event-intelligence news",
}

_IN_PLAY_REJECTED = {
    "goal",
    "red_card",
    "redcard",
    "yellow_card",
    "yellowcard",
    "penalty",
    "substitution",
    "half_time",
    "full_time",
    "kickoff",
}

_PLAYER_TYPES = {
    EventIntelligenceType.PLAYER_OUT,
    EventIntelligenceType.PLAYER_IN,
    EventIntelligenceType.INJURY_NEWS,
}


def normalize_fact(fact: EventIntelligenceFact) -> EventIntelligenceRecord:
    event_type = map_event_type(fact.event_type)
    canonical_event_id = _required_canonical_event_id(fact)
    title = (fact.title or "").strip()
    if not title:
        raise EventIntelligenceMappingError(
            "title",
            "unmapped",
            "Event title is required and must not be guessed",
        )
    subject = fact.subject.model_copy(deep=True)
    quality_flags = _subject_quality_flags(event_type, subject)
    if EventIntelligenceQualityFlag.AMBIGUOUS_SUBJECT in quality_flags:
        raise EventIntelligenceMappingError(
            "subject",
            "ambiguous",
            "Subject identity is ambiguous; refuse to guess team/player",
        )
    if event_type in _PLAYER_TYPES and not subject.player_id and not subject.player_label:
        raise EventIntelligenceMappingError(
            "subject.player",
            "unmapped",
            "Player-linked event requires an explicit player id or label; identity is not inferred",
        )
    verification = _verification_status(event_type, subject, quality_flags)
    source_kind = fact.source_kind or _source_kind_for_provenance(fact.provenance_class)
    raw_payload = dict(fact.raw_payload or fact.payload)
    raw_hash = _payload_hash(raw_payload)
    record = EventIntelligenceRecord(
        event_intelligence_id="pending",
        canonical_event_id=canonical_event_id,
        event_type=event_type,
        phase=EventPhase.PRE_MATCH,
        published_at=fact.published_at,
        ingested_at=fact.ingested_at,
        source_name=fact.source_name.strip(),
        source_kind=source_kind,
        source_event_id=fact.source_event_id.strip(),
        source_url=_optional(fact.source_url),
        source_reference=_optional(fact.source_reference),
        title=title,
        subject=subject,
        payload=dict(fact.payload),
        provenance_class=fact.provenance_class,
        verification_status=verification,
        quality_flags=quality_flags,
        confidence=1.0 if fact.confidence is None else fact.confidence,
        raw_payload_hash=raw_hash,
        raw_payload=raw_payload,
        content_fingerprint="pending",
        causal_claim=False,
        temporal_context_only=True,
    )
    fingerprint = content_fingerprint(record)
    record.content_fingerprint = fingerprint
    record.event_intelligence_id = deterministic_id(
        record.source_name, record.source_event_id, fingerprint
    )
    return record


def map_event_type(raw: str) -> EventIntelligenceType:
    token = normalize_text(raw)
    if not token:
        raise EventIntelligenceMappingError("event_type", "unmapped", "Event type is required")
    collapsed = token.replace(" ", "_")
    if token in _AMBIGUOUS_TYPES or collapsed in _IN_PLAY_REJECTED:
        detail = _AMBIGUOUS_TYPES.get(token) or _AMBIGUOUS_TYPES.get(collapsed)
        if collapsed in _IN_PLAY_REJECTED:
            raise EventIntelligenceMappingError(
                "event_type",
                "rejected_in_play",
                (
                    f"Type '{raw}' is an in-play match fact. Event intelligence stores "
                    "pre-match/news context only and will not attach score events to a fixture."
                ),
            )
        raise EventIntelligenceMappingError(
            "event_type",
            "ambiguous",
            f"Type '{raw}' is ambiguous ({detail}); refuse to guess",
        )
    mapped = _TYPE_ALIASES.get(token) or _TYPE_ALIASES.get(collapsed)
    if mapped is not None:
        return mapped
    try:
        return EventIntelligenceType(collapsed)
    except ValueError as exc:
        raise EventIntelligenceMappingError(
            "event_type",
            "unmapped",
            f"Type '{raw}' is not a supported event-intelligence type",
        ) from exc


def content_fingerprint(record: EventIntelligenceRecord) -> str:
    payload = {
        "canonical_event_id": record.canonical_event_id,
        "event_type": record.event_type.value,
        "payload": record.payload,
        "published_at": record.published_at.isoformat(),
        "source_reference": record.source_reference,
        "source_url": record.source_url,
        "subject": record.subject.model_dump(),
        "title": record.title,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(blob.encode()).hexdigest()[:32]


def deterministic_id(source_name: str, source_event_id: str, fingerprint: str) -> str:
    key = f"{source_name.strip().casefold()}:{source_event_id.strip()}|{fingerprint}"
    return f"ei:{sha256(key.encode()).hexdigest()[:24]}"


def source_key(source_name: str, source_event_id: str) -> str:
    return f"{source_name.strip().casefold()}:{source_event_id.strip()}"


def identity_tuple(record: EventIntelligenceRecord) -> tuple[str, str | None, str | None, str | None]:
    subject = record.subject
    team = subject.team_id or (normalize_text(subject.team_label) if subject.team_label else None)
    player = subject.player_id or (
        normalize_text(subject.player_label) if subject.player_label else None
    )
    manager = subject.manager_id or (
        normalize_text(subject.manager_label) if subject.manager_label else None
    )
    return (record.canonical_event_id, team, player, manager)


def _required_canonical_event_id(fact: EventIntelligenceFact) -> str:
    value = (fact.canonical_event_id or "").strip()
    if not value:
        raise EventIntelligenceMappingError(
            "canonical_event_id",
            "unmapped",
            "Canonical fixture id is required; team/player labels are not used to guess it",
        )
    return value


def _subject_quality_flags(
    event_type: EventIntelligenceType,
    subject: EventSubject,
) -> list[EventIntelligenceQualityFlag]:
    flags: list[EventIntelligenceQualityFlag] = []
    if subject.team_label and not subject.team_id:
        flags.append(EventIntelligenceQualityFlag.LABEL_ONLY_SUBJECT)
    if subject.player_label and not subject.player_id:
        if EventIntelligenceQualityFlag.LABEL_ONLY_SUBJECT not in flags:
            flags.append(EventIntelligenceQualityFlag.LABEL_ONLY_SUBJECT)
    if event_type in _PLAYER_TYPES and not subject.player_id:
        flags.append(EventIntelligenceQualityFlag.MISSING_PLAYER_IDENTITY)
    if event_type in {
        EventIntelligenceType.TEAM_SHEET_RELEASED,
        EventIntelligenceType.LINEUP_CHANGE,
        EventIntelligenceType.MANAGER_NEWS,
    } and not subject.team_id and not subject.team_label:
        flags.append(EventIntelligenceQualityFlag.MISSING_TEAM_IDENTITY)
    if _looks_ambiguous_player(subject.player_label):
        flags.append(EventIntelligenceQualityFlag.AMBIGUOUS_SUBJECT)
    return flags


def _looks_ambiguous_player(label: str | None) -> bool:
    if not label:
        return False
    lowered = label.casefold()
    if " or " in lowered or "/" in label or ";" in label:
        return True
    return False


def _verification_status(
    event_type: EventIntelligenceType,
    subject: EventSubject,
    flags: list[EventIntelligenceQualityFlag],
) -> VerificationStatus:
    if EventIntelligenceQualityFlag.AMBIGUOUS_SUBJECT in flags:
        return VerificationStatus.UNRESOLVED
    if event_type in _PLAYER_TYPES and not subject.player_id:
        return VerificationStatus.UNRESOLVED
    if EventIntelligenceQualityFlag.MISSING_TEAM_IDENTITY in flags:
        return VerificationStatus.UNRESOLVED
    if EventIntelligenceQualityFlag.LABEL_ONLY_SUBJECT in flags:
        return VerificationStatus.UNVERIFIED
    return VerificationStatus.VERIFIED


def _source_kind_for_provenance(provenance: ProvenanceClass) -> SourceKind:
    return {
        ProvenanceClass.FIXTURE_TEST: SourceKind.FIXTURE_TEST,
        ProvenanceClass.HISTORICAL_IMPORTED: SourceKind.HISTORICAL_IMPORT,
        ProvenanceClass.LIVE_READONLY_EXTERNAL: SourceKind.LIVE_READONLY_EXTERNAL,
    }[provenance]


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _payload_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(blob.encode()).hexdigest()
