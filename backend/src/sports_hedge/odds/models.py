from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint
from sports_hedge.domain.models import MarketSide
from sports_hedge.facts.catalog import CompetitionCode
from sports_hedge.facts.identity import KickoffPrecision


class QualityTier(StrEnum):
    """Explicit source-quality labels. Higher letters are never inferred."""

    A = "A"  # timestamped exchange odds + liquidity
    B = "B"  # timestamped bookmaker odds
    C = "C"  # opening/closing odds only
    D = "D"  # non-odds match facts only / unavailable odds


class QuoteType(StrEnum):
    TIMESTAMPED = "timestamped"
    OPENING = "opening"
    CLOSING = "closing"
    UNKNOWN = "unknown"


class VenueKind(StrEnum):
    EXCHANGE = "exchange"
    BOOKMAKER = "bookmaker"
    AGGREGATE = "aggregate"
    UNKNOWN = "unknown"


class OddsObservation(BaseModel):
    """Normalized historical odds row. Database is the source of truth."""

    observation_id: str
    canonical_match_id: str
    source: str
    source_market_id: str | None = None
    source_reference: str | None = None
    venue: str | None = None
    bookmaker: str | None = None
    venue_kind: VenueKind = VenueKind.UNKNOWN
    market_family: MarketFamily
    period: FootballPeriod
    line: Decimal | None = None
    selection: str
    side: MarketSide | None = None
    decimal_odds: Decimal | None = Field(default=None, gt=Decimal(1))
    observed_at: datetime | None = None
    quote_type: QuoteType = QuoteType.UNKNOWN
    spread: Decimal | None = Field(default=None, ge=0)
    liquidity: Decimal | None = Field(default=None, ge=0)
    commission_known: bool = False
    source_url: str | None = None
    retrieved_at: datetime
    raw_payload_hash: str | None = None
    quality_tier: QualityTier
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    semantics_complete: bool = False
    settlement_key: str | None = None
    competition_code: CompetitionCode
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    kickoff_precision: KickoffPrecision = KickoffPrecision.MINUTE
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_timezones(self) -> OddsObservation:
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            self.observed_at = self.observed_at.replace(tzinfo=UTC)
        if self.retrieved_at.tzinfo is None:
            self.retrieved_at = self.retrieved_at.replace(tzinfo=UTC)
        if self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        return self

    def market_equivalence_key(self) -> tuple[str, ...] | None:
        """Return a grouping key only when settlement semantics are complete.

        Incomplete fingerprints must not masquerade as equivalent markets.
        """

        if not self.semantics_complete or not self.settlement_key:
            return None
        line = "" if self.line is None else format(self.line, "f")
        return (
            self.canonical_match_id,
            self.market_family.value,
            self.period.value,
            line,
            self.settlement_key,
        )


class CanonicalMatchFact(BaseModel):
    """Match-level fact row used as the coverage denominator."""

    canonical_match_id: str
    competition_code: CompetitionCode
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    kickoff_precision: KickoffPrecision = KickoffPrecision.MINUTE
    source: str
    source_match_id: str | None = None
    home_goals: int | None = None
    away_goals: int | None = None
    retrieved_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_timezones(self) -> CanonicalMatchFact:
        if self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        if self.retrieved_at.tzinfo is None:
            self.retrieved_at = self.retrieved_at.replace(tzinfo=UTC)
        return self


class MappingException(BaseModel):
    exception_id: str
    source: str
    source_reference: str
    reason: str
    detail: str
    field: str
    retrieved_at: datetime
    raw_payload_hash: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> MappingException:
        if self.retrieved_at.tzinfo is None:
            self.retrieved_at = self.retrieved_at.replace(tzinfo=UTC)
        return self


class RawOddsRecord(BaseModel):
    """Provider-neutral ingest envelope. Adapters emit this; mapping is separate."""

    source: str = Field(min_length=1)
    source_market_id: str | None = None
    source_match_id: str | None = None
    source_reference: str | None = None
    source_url: str | None = None
    venue: str | None = None
    bookmaker: str | None = None
    venue_kind: VenueKind = VenueKind.UNKNOWN
    competition: str | None = None
    season: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    kickoff_utc: datetime | None = None
    kickoff_precision: KickoffPrecision = KickoffPrecision.UNKNOWN
    market_family: MarketFamily | None = None
    period: FootballPeriod | None = None
    line: Decimal | None = None
    selection: str | None = None
    side: MarketSide | None = None
    decimal_odds: Decimal | None = None
    observed_at: datetime | None = None
    quote_type: QuoteType = QuoteType.UNKNOWN
    spread: Decimal | None = None
    liquidity: Decimal | None = None
    commission_known: bool = False
    retrieved_at: datetime
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    home_goals: int | None = None
    away_goals: int | None = None
    settlement: SettlementFingerprint | None = None
    semantics_complete: bool | None = None
    mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def ensure_timezones(self) -> RawOddsRecord:
        if self.kickoff_utc is not None and self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            self.observed_at = self.observed_at.replace(tzinfo=UTC)
        if self.retrieved_at.tzinfo is None:
            self.retrieved_at = self.retrieved_at.replace(tzinfo=UTC)
        return self


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = repr(sorted(payload.items())).encode("utf-8")
    return sha256(encoded).hexdigest()[:32]


def observation_id_for(
    *,
    source: str,
    source_market_id: str | None,
    selection: str,
    side: MarketSide | None,
    quote_type: QuoteType,
    observed_at: datetime | None,
    line: Decimal | None,
) -> str:
    payload = "|".join(
        [
            source.strip().casefold(),
            source_market_id or "",
            selection.strip().casefold(),
            "" if side is None else side.value,
            quote_type.value,
            "na" if observed_at is None else observed_at.isoformat(),
            "" if line is None else format(line, "f"),
        ]
    )
    return f"obs:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def assign_quality_tier(
    *,
    has_odds: bool,
    quote_type: QuoteType,
    source_provided_timestamp: bool,
    liquidity: Decimal | None,
    venue_kind: VenueKind,
) -> QualityTier:
    """Assign quality from what the source actually provided. Never upgrade."""

    if not has_odds:
        return QualityTier.D
    if quote_type in {QuoteType.OPENING, QuoteType.CLOSING}:
        return QualityTier.C
    if quote_type == QuoteType.TIMESTAMPED and source_provided_timestamp:
        if venue_kind == VenueKind.EXCHANGE and liquidity is not None:
            return QualityTier.A
        return QualityTier.B
    return QualityTier.C
