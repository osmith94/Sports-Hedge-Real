from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint
from sports_hedge.domain.models import MarketSide
from sports_hedge.facts.identity import KickoffPrecision
from sports_hedge.odds.timestamps import require_aware


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
    source_observation_key: str
    quality_tier: QualityTier
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    semantics_complete: bool = False
    settlement_key: str | None = None
    competition_code: str
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    kickoff_precision: KickoffPrecision = KickoffPrecision.MINUTE
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_naive_timestamps(self) -> OddsObservation:
        require_aware(self.retrieved_at, "retrieved_at")
        require_aware(self.kickoff_utc, "kickoff_utc")
        if self.observed_at is not None:
            require_aware(self.observed_at, "observed_at")
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

    def implied_probability(self) -> Decimal | None:
        """Raw implied probability 1/odds. None when no odds were stored."""

        if self.decimal_odds is None:
            return None
        return Decimal(1) / self.decimal_odds

    def implied_logit(self) -> float | None:
        """Logit of implied probability for movement analysis. Not a prediction."""

        probability = self.implied_probability()
        if probability is None:
            return None
        value = float(probability)
        if value <= 0.0 or value >= 1.0:
            return None
        return math.log(value / (1.0 - value))


class CanonicalMatchFact(BaseModel):
    """Odds-side match index used as a coverage cache.

    This is not the historical football facts repository. Match scores and
    events belong to ``sports_hedge.historical``. This row only records that
    odds ingestion observed a canonical match ID.
    """

    canonical_match_id: str
    competition_code: str
    season: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    kickoff_precision: KickoffPrecision = KickoffPrecision.MINUTE
    source: str
    source_match_id: str | None = None
    retrieved_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_naive_timestamps(self) -> CanonicalMatchFact:
        require_aware(self.kickoff_utc, "kickoff_utc")
        require_aware(self.retrieved_at, "retrieved_at")
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
    def reject_naive_timestamps(self) -> MappingException:
        require_aware(self.retrieved_at, "retrieved_at")
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
    def reject_naive_timestamps(self) -> RawOddsRecord:
        require_aware(self.retrieved_at, "retrieved_at")
        if self.kickoff_utc is not None:
            require_aware(self.kickoff_utc, "kickoff_utc")
        if self.observed_at is not None:
            require_aware(self.observed_at, "observed_at")
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
    decimal_odds: Decimal | None,
    raw_payload_hash: str | None,
) -> str:
    """Identity of one stored observation including content.

    Exact replays keep this ID (idempotent). A corrected price or payload
    hash produces a new ID so revisions are append-only.
    """

    payload = "|".join(
        [
            source_observation_key(
                source=source,
                source_market_id=source_market_id,
                selection=selection,
                side=side,
                quote_type=quote_type,
                observed_at=observed_at,
                line=line,
            ),
            "" if decimal_odds is None else format(decimal_odds, "f"),
            raw_payload_hash or "",
        ]
    )
    return f"obs:{sha256(payload.encode()).hexdigest()[:24]}"


def source_observation_key(
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
    return f"srcobs:{sha256(payload.encode()).hexdigest()[:24]}"


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
