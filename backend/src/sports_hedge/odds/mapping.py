from __future__ import annotations

from hashlib import sha256

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint
from sports_hedge.facts.catalog import competition_from_label
from sports_hedge.facts.identity import build_match_ref
from sports_hedge.odds.models import (
    MappingException,
    OddsObservation,
    RawOddsRecord,
    assign_quality_tier,
    observation_id_for,
    payload_hash,
)


class OddsMappingError(ValueError):
    """Raised when a record cannot be mapped without guessing."""


def map_raw_record(record: RawOddsRecord) -> OddsObservation:
    """Deterministic mapping. Ambiguous or incomplete identity fails closed."""

    issues = _identity_issues(record)
    if issues:
        raise OddsMappingError(issues[0][1])
    assert record.competition is not None
    assert record.home_team is not None
    assert record.away_team is not None
    assert record.kickoff_utc is not None
    assert record.market_family is not None
    assert record.period is not None
    assert record.selection is not None

    match = build_match_ref(
        competition=record.competition,
        home_team=record.home_team,
        away_team=record.away_team,
        kickoff_utc=record.kickoff_utc,
        season=record.season,
        kickoff_precision=record.kickoff_precision,
    )
    settlement = record.settlement or SettlementFingerprint(
        period=record.period,
        line=record.line,
    )
    semantics_complete = (
        record.semantics_complete
        if record.semantics_complete is not None
        else _fingerprint_complete(settlement)
    )
    settlement_key = settlement.deterministic_key() if semantics_complete else None
    has_odds = record.decimal_odds is not None
    quality = assign_quality_tier(
        has_odds=has_odds,
        quote_type=record.quote_type,
        source_provided_timestamp=record.observed_at is not None,
        liquidity=record.liquidity,
        venue_kind=record.venue_kind,
    )
    observation_id = observation_id_for(
        source=record.source,
        source_market_id=record.source_market_id,
        selection=record.selection,
        side=record.side,
        quote_type=record.quote_type,
        observed_at=record.observed_at,
        line=record.line,
    )
    return OddsObservation(
        observation_id=observation_id,
        canonical_match_id=match.canonical_match_id,
        source=record.source.strip().casefold(),
        source_market_id=record.source_market_id,
        source_reference=record.source_reference or record.source_match_id,
        venue=record.venue,
        bookmaker=record.bookmaker,
        venue_kind=record.venue_kind,
        market_family=record.market_family,
        period=record.period,
        line=record.line,
        selection=record.selection.strip().casefold(),
        side=record.side,
        decimal_odds=record.decimal_odds,
        observed_at=record.observed_at,
        quote_type=record.quote_type,
        spread=record.spread,
        liquidity=record.liquidity,
        commission_known=record.commission_known,
        source_url=record.source_url,
        retrieved_at=record.retrieved_at,
        raw_payload_hash=payload_hash(record.raw_payload) if record.raw_payload else None,
        quality_tier=quality,
        confidence=record.mapping_confidence if record.mapping_confidence is not None else 1.0,
        semantics_complete=semantics_complete,
        settlement_key=settlement_key,
        competition_code=match.competition_code,
        season=match.season,
        home_team=match.home_team,
        away_team=match.away_team,
        kickoff_utc=match.kickoff_utc,
        kickoff_precision=match.kickoff_precision,
        metadata={
            "source_match_id": record.source_match_id,
            "home_goals": record.home_goals,
            "away_goals": record.away_goals,
        },
    )


def mapping_exception_for(record: RawOddsRecord, error: OddsMappingError) -> MappingException:
    field, reason, detail = _identity_issues(record)[0] if _identity_issues(record) else (
        "record",
        "mapping_failed",
        str(error),
    )
    digest = sha256(f"{record.source}|{record.source_reference}|{reason}|{detail}".encode()).hexdigest()[:24]
    return MappingException(
        exception_id=f"map:{digest}",
        source=record.source.strip().casefold(),
        source_reference=record.source_reference
        or record.source_market_id
        or record.source_match_id
        or "unknown",
        reason=reason,
        detail=detail,
        field=field,
        retrieved_at=record.retrieved_at,
        raw_payload_hash=payload_hash(record.raw_payload) if record.raw_payload else None,
    )


def _identity_issues(record: RawOddsRecord) -> list[tuple[str, str, str]]:
    issues: list[tuple[str, str, str]] = []
    if not record.competition:
        issues.append(("competition", "missing_competition", "competition is required"))
    elif (
        competition_from_label(record.competition, record.season or "2025/26") is None
        and competition_from_label(record.competition) is None
    ):
        issues.append(
            (
                "competition",
                "unknown_competition",
                f"cannot map competition {record.competition!r} without guessing",
            )
        )
    if not record.home_team or not record.away_team:
        issues.append(("teams", "missing_teams", "home_team and away_team are required"))
    elif record.home_team.strip().casefold() == record.away_team.strip().casefold():
        issues.append(("teams", "ambiguous_teams", "home and away teams are identical"))
    if record.kickoff_utc is None:
        issues.append(("kickoff_utc", "missing_kickoff", "kickoff is required for canonical match identity"))
    if record.market_family is None or record.market_family == MarketFamily.UNKNOWN:
        issues.append(("market_family", "unknown_market_family", "market family is unknown"))
    if record.period is None or record.period == FootballPeriod.UNKNOWN:
        issues.append(("period", "unknown_period", "period is unknown"))
    if not record.selection:
        issues.append(("selection", "missing_selection", "selection/outcome is required"))
    return issues


def _fingerprint_complete(settlement: SettlementFingerprint) -> bool:
    if settlement.scope.value == "unknown" or settlement.period.value == "unknown":
        return False
    return not (settlement.extra_time_included is None or settlement.penalties_included is None)
