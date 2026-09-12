from __future__ import annotations

from hashlib import sha256

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint
from sports_hedge.facts.aliases import football_alias_registry
from sports_hedge.facts.catalog import competition_from_label
from sports_hedge.facts.identity import build_match_ref
from sports_hedge.historical.catalog import HistoricalCatalog
from sports_hedge.historical.errors import HistoricalMappingError
from sports_hedge.normalization.text import normalize_text
from sports_hedge.odds.models import (
    MappingException,
    OddsObservation,
    RawOddsRecord,
    assign_quality_tier,
    observation_id_for,
    payload_hash,
    source_observation_key,
)
from sports_hedge.odds.timestamps import require_aware


class OddsMappingError(ValueError):
    """Raised when a record cannot be mapped without guessing."""


_HISTORICAL_CATALOG = HistoricalCatalog()


def map_raw_record(record: RawOddsRecord) -> OddsObservation:
    """Map onto the shared ``sports_hedge.facts`` match identity from main."""

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
    require_aware(record.retrieved_at, "retrieved_at")
    require_aware(record.kickoff_utc, "kickoff_utc")

    try:
        home_team = _resolve_bounded_team(record.home_team)
        away_team = _resolve_bounded_team(record.away_team)
        match = build_match_ref(
            competition=record.competition,
            home_team=home_team,
            away_team=away_team,
            kickoff_utc=record.kickoff_utc,
            season=record.season,
            kickoff_precision=record.kickoff_precision,
        )
    except (ValueError, HistoricalMappingError) as error:
        raise OddsMappingError(str(error)) from error

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
    raw_hash = payload_hash(record.raw_payload) if record.raw_payload else None
    key = source_observation_key(
        source=record.source,
        source_market_id=record.source_market_id,
        selection=record.selection,
        side=record.side,
        quote_type=record.quote_type,
        observed_at=record.observed_at,
        line=record.line,
    )
    observation_id = observation_id_for(
        source=record.source,
        source_market_id=record.source_market_id,
        selection=record.selection,
        side=record.side,
        quote_type=record.quote_type,
        observed_at=record.observed_at,
        line=record.line,
        decimal_odds=record.decimal_odds,
        raw_payload_hash=raw_hash,
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
        raw_payload_hash=raw_hash,
        source_observation_key=key,
        quality_tier=quality,
        confidence=record.mapping_confidence if record.mapping_confidence is not None else 1.0,
        semantics_complete=semantics_complete,
        settlement_key=settlement_key,
        competition_code=match.competition_code.value,
        season=match.season,
        home_team=match.home_team,
        away_team=match.away_team,
        kickoff_utc=match.kickoff_utc,
        kickoff_precision=match.kickoff_precision,
        metadata={
            "source_match_id": record.source_match_id,
            "home_team_id": match.home_team_id,
            "away_team_id": match.away_team_id,
            "research_only": bool(record.raw_payload.get("research_only")),
            "source_url": record.source_url,
        },
    )


def mapping_exception_for(record: RawOddsRecord, error: OddsMappingError) -> MappingException:
    issues = _identity_issues(record)
    field, reason, detail = issues[0] if issues else ("record", "mapping_failed", str(error))
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
    elif competition_from_label(record.competition, record.season or "2025/26") is None and (
        competition_from_label(record.competition) is None
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
    else:
        for field, label in (("home_team", record.home_team), ("away_team", record.away_team)):
            try:
                _resolve_bounded_team(label)
            except OddsMappingError as error:
                issues.append((field, "unknown_team", str(error)))
    if record.kickoff_utc is None:
        issues.append(("kickoff_utc", "missing_kickoff", "kickoff is required for canonical match identity"))
    if record.market_family is None or record.market_family == MarketFamily.UNKNOWN:
        issues.append(("market_family", "unknown_market_family", "market family is unknown"))
    if record.period is None or record.period == FootballPeriod.UNKNOWN:
        issues.append(("period", "unknown_period", "period is unknown"))
    if not record.selection:
        issues.append(("selection", "missing_selection", "selection/outcome is required"))
    return issues


def _resolve_bounded_team(name: str) -> str:
    """Fail closed unless the name is in the historical catalog or an explicit alias of one."""

    try:
        return _HISTORICAL_CATALOG.resolve_team(name).canonical_name
    except HistoricalMappingError:
        key = normalize_text(name)
        if key not in football_alias_registry.aliases:
            raise OddsMappingError(f"Unknown team '{name}'")
        aliased = football_alias_registry.aliases[key]
        try:
            return _HISTORICAL_CATALOG.resolve_team(aliased).canonical_name
        except HistoricalMappingError as error:
            raise OddsMappingError(f"Unknown team '{name}'") from error


def _fingerprint_complete(settlement: SettlementFingerprint) -> bool:
    if settlement.scope.value == "unknown" or settlement.period.value == "unknown":
        return False
    return not (settlement.extra_time_included is None or settlement.penalties_included is None)
