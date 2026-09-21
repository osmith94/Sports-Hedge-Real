"""Durable approved-market catalogue identity (Issue #341 Phase 2 / #437 Phase 1A).

UNIVERSE persists exact Matchbook / Kalshi / Polymarket native IDs and Kalshi
fee metadata for registered FIXTURE_MATCH families. Polymarket identity is the
Gamma market id plus real CLOB token IDs — never invented ``condition_id:0/1``
placeholders. COMPETITION_SEASON observation listings may persist exact
provider IDs with a sibling MarketScope; they stay out of the fixture
derived-price working set unless season logic processes them.
This module is identity / lifecycle truth, not a second matcher and not a
price engine.

Admission (`paper_admission`, `settlement_assumption`, live-execution
eligibility) is derived at use time from the live Approved Match Register and
PAPER mode. Those policy fields must never be stored on catalogue rows.
Quotes, solver output, mapping confidence, and work-queue state are also
forbidden.

COMPETITION_SEASON rows are observation-only in Phase 1A: missing native IDs
fail closed, and they are not register-admitted.

Data class: durable identity/fee-metadata. Not live quotes.
PAPER / read-only.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sports_hedge.domain.football import CanonicalOutcome, FootballPeriod, MarketFamily
from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import resolve_kalshi_fee_metadata
from sports_hedge.matching.approved_register import REGISTER_VERSION

CATALOGUE_SCHEMA_VERSION = 1
CATALOGUE_ISSUE = 341
FEE_SNAPSHOT_SCHEMA_VERSION = 1

FEE_STATUS_KNOWN = "known"
FEE_STATUS_UNKNOWN = "unknown"
FEE_STATUS_PARTIAL = "partial_event_fee_override"
FEE_STATUS_UNSUPPORTED = "unsupported"

FEE_SOURCE_NESTED_LIST_EVENTS = "nested_list_events"
FEE_SOURCE_GET_SERIES = "get_series"
FEE_SOURCE_EVENT_PAYLOAD = "event_payload"

CATALOGUE_FORBIDDEN_COLUMNS = frozenset(
    {
        "paper_admission",
        "settlement_assumption",
        "live_execution_eligible",
        "mapping_confidence",
        "min_mapping_confidence",
        "confidence",
        "quotes",
        "quote",
        "best_back",
        "best_lay",
        "solver_output",
        "paper_eligible",
        "work_queue_state",
        "next_retry_at",
        "in_flight",
        "priority",
    }
)
FEE_SNAPSHOT_FORBIDDEN_COLUMNS = frozenset(
    {
        "quote",
        "quotes",
        "best_bid",
        "best_ask",
        "yes_bid",
        "yes_ask",
        "order_book",
        "odds",
        "decimal_odds",
        "size_at_touch",
    }
)
HISTORY_FORBIDDEN_COLUMNS = CATALOGUE_FORBIDDEN_COLUMNS | FEE_SNAPSHOT_FORBIDDEN_COLUMNS

SUPPORTED_KALSHI_FEE_TYPES = frozenset({"quadratic", "quadratic_with_maker_fees"})


class CatalogueRowState(StrEnum):
    ACTIVE = "ACTIVE"
    NOT_LISTED = "NOT_LISTED"
    TERMINAL = "TERMINAL"
    DISAPPEARED = "DISAPPEARED"
    INVALIDATED = "INVALIDATED"
    VENUE_UNAVAILABLE = "VENUE_UNAVAILABLE"


class OutcomeNativeId(BaseModel):
    """Exact native identifier for one required register outcome."""

    outcome: str
    native_id: str


class KalshiFeeSnapshotRecord(BaseModel):
    """Compact Kalshi fee metadata. Never quotes."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    series_ticker: str
    event_ticker: str | None = None
    market_ticker: str | None = None
    fee_type: str | None = None
    fee_multiplier: str | None = None
    fee_type_override: Any = None
    fee_multiplier_override: Any = None
    series_fee_type: Any = None
    series_fee_multiplier: Any = None
    fee_provenance: str | None = None
    fee_resolution_status: str
    fee_resolution_error: str | None = None
    captured_at: datetime
    confirmed_at: datetime | None = None
    source: str | None = None

    def is_known(self) -> bool:
        return self.fee_resolution_status == FEE_STATUS_KNOWN and self.fee_type is not None

    def semantic_identity_parts(self) -> tuple[str, ...]:
        """Inputs that uniquely identify an immutable fee snapshot.

        Series fee type/multiplier, event override type/multiplier, resolution
        status/error, provenance, and series/event/market keys. Timestamps and
        quotes are excluded so the same semantics reuse the same snapshot ID.
        """

        return (
            (self.series_ticker or "").strip(),
            (self.event_ticker or "").strip(),
            (self.market_ticker or "").strip(),
            _canonical_fee_token(self.series_fee_type),
            _canonical_fee_token(self.series_fee_multiplier),
            _canonical_fee_token(self.fee_type_override),
            _canonical_fee_token(self.fee_multiplier_override),
            (self.fee_resolution_status or "").strip(),
            (self.fee_resolution_error or "").strip(),
            (self.fee_provenance or "").strip(),
        )


class CatalogueHistoryRecord(BaseModel):
    """Append-only lifecycle/version history. Not scheduler truth."""

    model_config = ConfigDict(extra="forbid")

    history_id: int | None = None
    catalogue_row_id: str
    prior_row_state: str | None = None
    new_row_state: str
    prior_content_version: int | None = None
    new_content_version: int
    prior_native_identity_json: str | None = None
    new_native_identity_json: str
    prior_kalshi_fee_snapshot_id: str | None = None
    new_kalshi_fee_snapshot_id: str | None = None
    invalidation_reason: str | None = None
    generation_id: str | None = None
    recorded_at: datetime


class ApprovedMarketCatalogueRow(BaseModel):
    """One durable approved-family identity row."""

    model_config = ConfigDict(extra="forbid")

    catalogue_row_id: str
    schema_version: int = CATALOGUE_SCHEMA_VERSION
    register_version: str = REGISTER_VERSION
    register_canonical_key: str
    canonical_event_id: str
    competition: str | None = None
    home_canonical: str | None = None
    away_canonical: str | None = None
    kickoff_utc: datetime | None = None
    matchbook_event_id: str | None = None
    matchbook_market_id: str | None = None
    matchbook_runner_ids: list[OutcomeNativeId] = Field(default_factory=list)
    kalshi_event_ticker: str | None = None
    kalshi_market_tickers: list[str] = Field(default_factory=list)
    kalshi_outcome_ids: list[OutcomeNativeId] = Field(default_factory=list)
    kalshi_series_ticker: str | None = None
    polymarket_event_id: str | None = None
    polymarket_market_id: str | None = None
    polymarket_condition_id: str | None = None
    polymarket_token_ids: list[OutcomeNativeId] = Field(default_factory=list)
    family: str | None = None
    period: str | None = None
    line: str | None = None
    required_outcomes: list[str] = Field(default_factory=list)
    kalshi_fee_snapshot_id: str | None = None
    row_state: CatalogueRowState = CatalogueRowState.ACTIVE
    invalidation_reason: str | None = None
    first_catalogued_at: datetime
    last_confirmed_at: datetime | None = None
    last_seen_generation_id: str | None = None
    content_version: int = 1
    market_scope: MarketScope = MarketScope.FIXTURE_MATCH
    source_venue: VenueName | None = None
    season_id: str | None = None
    competition_code: str | None = None
    participant_type: str | None = None
    participant_canonical_id: str | None = None
    settlement_fingerprint_version: str | None = None
    expected_settlement_horizon: str | None = None
    polymarket_clob_token_ids: list[str] = Field(default_factory=list)
    polymarket_event_slug: str | None = None
    polymarket_event_ticker: str | None = None

    @model_validator(mode="after")
    def validate_market_scope_identity(self) -> ApprovedMarketCatalogueRow:
        if self.market_scope is MarketScope.FIXTURE_MATCH:
            return self
        if self.market_scope is not MarketScope.COMPETITION_SEASON:
            raise ValueError("unsupported_market_scope")
        if self.home_canonical or self.away_canonical or self.kickoff_utc is not None:
            raise ValueError("fixture_fields_forbidden_on_competition_season")
        required = (
            self.season_id,
            self.competition_code,
            self.participant_type,
            self.participant_canonical_id,
            self.settlement_fingerprint_version,
        )
        if any(not str(value or "").strip() for value in required):
            raise ValueError("competition_season_identity_incomplete")
        if self.source_venue is None:
            raise ValueError("competition_season_source_venue_required")
        if self.source_venue is VenueName.POLYMARKET:
            tokens = [str(token).strip() for token in self.polymarket_clob_token_ids]
            if len(tokens) != 2 or any(not token for token in tokens):
                raise ValueError("missing_polymarket_clob_token_ids")
            if not self.polymarket_event_id or not self.polymarket_market_id:
                raise ValueError("missing_polymarket_native_ids")
        return self

    def native_identity_tuple(self) -> tuple[Any, ...]:
        return (
            self.matchbook_event_id,
            self.matchbook_market_id,
            tuple((item.outcome, item.native_id) for item in self.matchbook_runner_ids),
            self.kalshi_event_ticker,
            tuple(self.kalshi_market_tickers),
            tuple((item.outcome, item.native_id) for item in self.kalshi_outcome_ids),
            self.kalshi_series_ticker,
            None if self.source_venue is None else self.source_venue.value,
            self.market_scope.value,
            self.season_id,
            self.competition_code,
            self.participant_canonical_id,
            self.polymarket_event_id,
            self.polymarket_market_id,
            self.polymarket_condition_id,
            tuple((item.outcome, item.native_id) for item in self.polymarket_token_ids),
            tuple(self.polymarket_clob_token_ids),
            self.polymarket_event_slug,
            self.polymarket_event_ticker,
        )

    def native_identity_payload(self) -> dict[str, Any]:
        return {
            "matchbook_event_id": self.matchbook_event_id,
            "matchbook_market_id": self.matchbook_market_id,
            "matchbook_runner_ids": [item.model_dump() for item in self.matchbook_runner_ids],
            "kalshi_event_ticker": self.kalshi_event_ticker,
            "kalshi_market_tickers": list(self.kalshi_market_tickers),
            "kalshi_outcome_ids": [item.model_dump() for item in self.kalshi_outcome_ids],
            "kalshi_series_ticker": self.kalshi_series_ticker,
            "source_venue": None if self.source_venue is None else self.source_venue.value,
            "market_scope": self.market_scope.value,
            "season_id": self.season_id,
            "competition_code": self.competition_code,
            "participant_canonical_id": self.participant_canonical_id,
            "polymarket_event_id": self.polymarket_event_id,
            "polymarket_market_id": self.polymarket_market_id,
            "polymarket_condition_id": self.polymarket_condition_id,
            "polymarket_token_ids": [item.model_dump() for item in self.polymarket_token_ids],
            "polymarket_clob_token_ids": list(self.polymarket_clob_token_ids),
            "polymarket_event_slug": self.polymarket_event_slug,
            "polymarket_event_ticker": self.polymarket_event_ticker,
        }

    def native_identity_json(self) -> str:
        return json.dumps(self.native_identity_payload(), separators=(",", ":"), sort_keys=True)

    def content_identity_changed(self, other: ApprovedMarketCatalogueRow) -> bool:
        return (
            self.native_identity_tuple() != other.native_identity_tuple()
            or self.kalshi_fee_snapshot_id != other.kalshi_fee_snapshot_id
        )


class DerivedPriceEngineItem(BaseModel):
    """In-memory price-engine claim copied from an ACTIVE catalogue row.

    Not persisted. Phase 3 schedules these; Phase 2 only reconstructs identity.
    """

    catalogue_row_id: str
    content_version: int
    canonical_event_id: str
    register_canonical_key: str
    matchbook_event_id: str | None = None
    matchbook_market_id: str | None = None
    matchbook_runner_ids: list[OutcomeNativeId] = Field(default_factory=list)
    kalshi_event_ticker: str | None = None
    kalshi_market_tickers: list[str] = Field(default_factory=list)
    kalshi_outcome_ids: list[OutcomeNativeId] = Field(default_factory=list)
    kalshi_fee_snapshot_id: str | None = None
    polymarket_event_id: str | None = None
    polymarket_market_id: str | None = None
    polymarket_condition_id: str | None = None
    polymarket_token_ids: list[OutcomeNativeId] = Field(default_factory=list)
    family: str | None = None
    period: str | None = None
    line: str | None = None
    required_outcomes: list[str] = Field(default_factory=list)
    competition: str | None = None
    home_canonical: str | None = None
    away_canonical: str | None = None
    kickoff_utc: datetime | None = None


def classify_kalshi_fee_resolution(metadata: dict[str, Any]) -> tuple[str, str | None]:
    """Map `resolve_kalshi_fee_metadata` output onto snapshot status.

    Preserves fail-closed semantics: partial override, missing, flat, and
    unknown types never become known 0%.
    """

    error = str(metadata.get("fee_resolution_error") or "").strip()
    if error == "partial_event_fee_override":
        return FEE_STATUS_PARTIAL, error
    if error:
        return FEE_STATUS_UNKNOWN, error
    fee_type = str(metadata.get("fee_type") or "").strip().casefold()
    if not fee_type:
        return FEE_STATUS_UNKNOWN, "missing_fee_type"
    if fee_type == "flat":
        return FEE_STATUS_UNSUPPORTED, "flat_fee_type_unmodelled"
    if fee_type not in SUPPORTED_KALSHI_FEE_TYPES:
        return FEE_STATUS_UNSUPPORTED, f"unsupported_fee_type:{fee_type}"
    multiplier = metadata.get("fee_multiplier")
    if multiplier is None or multiplier == "":
        return FEE_STATUS_UNKNOWN, "missing_fee_multiplier"
    try:
        Decimal(str(multiplier))
    except (InvalidOperation, ValueError, TypeError):
        return FEE_STATUS_UNKNOWN, "invalid_fee_multiplier"
    return FEE_STATUS_KNOWN, None


def semantic_kalshi_fee_snapshot_id(record: KalshiFeeSnapshotRecord) -> str:
    payload = "|".join(record.semantic_identity_parts())
    digest = sha256(payload.encode()).hexdigest()[:24]
    return f"kfee:{digest}"


def kalshi_fee_snapshot_from_payloads(
    *,
    series: dict[str, Any] | None,
    event: dict[str, Any] | None,
    market_ticker: str | None,
    captured_at: datetime,
    source: str,
    snapshot_id: str | None = None,
    confirmed_at: datetime | None = None,
) -> KalshiFeeSnapshotRecord:
    metadata = resolve_kalshi_fee_metadata(event=event, series=series)
    status, error = classify_kalshi_fee_resolution(metadata)
    fee_type_raw = metadata.get("fee_type")
    fee_multiplier_raw = metadata.get("fee_multiplier")
    stored_type: str | None = None
    stored_multiplier: str | None = None
    if status == FEE_STATUS_KNOWN:
        stored_type = str(fee_type_raw)
        stored_multiplier = str(fee_multiplier_raw)
    elif status == FEE_STATUS_UNSUPPORTED and fee_type_raw not in (None, ""):
        # Audit the unsupported type; never invent a 0% multiplier.
        stored_type = str(fee_type_raw)
    event_payload = event or {}
    series_payload = series or {}
    record = KalshiFeeSnapshotRecord(
        snapshot_id=snapshot_id or "provisional",
        series_ticker=str(
            series_payload.get("ticker")
            or event_payload.get("series_ticker")
            or ""
        ).strip(),
        event_ticker=_optional_text(event_payload.get("event_ticker")),
        market_ticker=_optional_text(market_ticker),
        fee_type=stored_type,
        fee_multiplier=stored_multiplier,
        fee_type_override=metadata.get("fee_type_override"),
        fee_multiplier_override=metadata.get("fee_multiplier_override"),
        series_fee_type=metadata.get("series_fee_type"),
        series_fee_multiplier=metadata.get("series_fee_multiplier"),
        fee_provenance=None if metadata.get("fee_provenance") in (None, "") else str(
            metadata.get("fee_provenance")
        ),
        fee_resolution_status=status,
        fee_resolution_error=error,
        captured_at=captured_at,
        confirmed_at=confirmed_at,
        source=source,
    )
    if not snapshot_id:
        record = record.model_copy(update={"snapshot_id": semantic_kalshi_fee_snapshot_id(record)})
    return record


def catalogue_row_supports_paper_eligibility(
    row: ApprovedMarketCatalogueRow,
    snapshot: KalshiFeeSnapshotRecord | None,
) -> bool:
    """Later economics may mark paper-eligible only with a known fee snapshot.

    Unknown / partial / unsupported / missing never become 0%. This is not a
    solver decision; it is the catalogue-side fail-closed gate.
    """

    if row.row_state is not CatalogueRowState.ACTIVE:
        return False
    if row.market_scope is MarketScope.COMPETITION_SEASON:
        return False
    if str(row.register_canonical_key or "").startswith("OBSERVATION:"):
        return False
    if not row.kalshi_fee_snapshot_id:
        return False
    if snapshot is None:
        return False
    if snapshot.snapshot_id != row.kalshi_fee_snapshot_id:
        return False
    if snapshot.fee_resolution_status != FEE_STATUS_KNOWN:
        return False
    if not snapshot.fee_type:
        return False
    if snapshot.fee_multiplier is None or snapshot.fee_multiplier == "":
        return False
    try:
        Decimal(str(snapshot.fee_multiplier))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return True


def derived_price_engine_working_set(
    rows: list[ApprovedMarketCatalogueRow],
) -> list[DerivedPriceEngineItem]:
    """Rebuild in-memory price-engine identity from ACTIVE rows. No list_markets."""

    items: list[DerivedPriceEngineItem] = []
    for row in rows:
        if row.row_state is not CatalogueRowState.ACTIVE:
            continue
        if row.market_scope is MarketScope.COMPETITION_SEASON:
            # Phase 1A persists exact IDs; BACKGROUND/HOT exact-ID pricing is later.
            continue
        if str(row.register_canonical_key or "").startswith("OBSERVATION:"):
            continue
        items.append(
            DerivedPriceEngineItem(
                catalogue_row_id=row.catalogue_row_id,
                content_version=row.content_version,
                canonical_event_id=row.canonical_event_id,
                register_canonical_key=row.register_canonical_key,
                matchbook_event_id=row.matchbook_event_id,
                matchbook_market_id=row.matchbook_market_id,
                matchbook_runner_ids=list(row.matchbook_runner_ids),
                kalshi_event_ticker=row.kalshi_event_ticker,
                kalshi_market_tickers=list(row.kalshi_market_tickers),
                kalshi_outcome_ids=list(row.kalshi_outcome_ids),
                kalshi_fee_snapshot_id=row.kalshi_fee_snapshot_id,
                polymarket_event_id=row.polymarket_event_id,
                polymarket_market_id=row.polymarket_market_id,
                polymarket_condition_id=row.polymarket_condition_id,
                polymarket_token_ids=list(row.polymarket_token_ids),
                family=row.family,
                period=row.period,
                line=row.line,
                required_outcomes=list(row.required_outcomes),
                competition=row.competition,
                home_canonical=row.home_canonical,
                away_canonical=row.away_canonical,
                kickoff_utc=row.kickoff_utc,
            )
        )
    return items


def family_period_line_from_key(register_canonical_key: str) -> tuple[str | None, str, str | None]:
    if register_canonical_key == "MATCH_RESULT_FT":
        return MarketFamily.MATCH_RESULT.value, FootballPeriod.FULL_TIME.value, None
    if register_canonical_key == "BTTS_FT":
        return MarketFamily.BOTH_TEAMS_TO_SCORE.value, FootballPeriod.FULL_TIME.value, None
    if register_canonical_key == "FTTS_FT":
        return MarketFamily.FIRST_TEAM_TO_SCORE.value, FootballPeriod.FULL_TIME.value, None
    if register_canonical_key.startswith("TOTAL_GOALS_FT:"):
        line = register_canonical_key.split(":", 1)[1]
        return MarketFamily.TOTAL_GOALS.value, FootballPeriod.FULL_TIME.value, line
    if register_canonical_key == "NFL_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER.value, FootballPeriod.FULL_TIME.value, None
    if register_canonical_key.startswith("NFL_POINT_SPREAD_FT:"):
        line = register_canonical_key.split(":", 1)[1]
        return MarketFamily.POINT_SPREAD.value, FootballPeriod.FULL_TIME.value, line
    if register_canonical_key.startswith("NFL_TOTAL_POINTS_FT:"):
        line = register_canonical_key.split(":", 1)[1]
        return MarketFamily.TOTAL_POINTS.value, FootballPeriod.FULL_TIME.value, line
    return None, FootballPeriod.FULL_TIME.value, None


def required_outcomes_for_key(register_canonical_key: str) -> list[str]:
    if register_canonical_key == "MATCH_RESULT_FT":
        return [
            CanonicalOutcome.HOME.value,
            CanonicalOutcome.DRAW.value,
            CanonicalOutcome.AWAY.value,
        ]
    if register_canonical_key == "BTTS_FT":
        return [CanonicalOutcome.YES.value, CanonicalOutcome.NO.value]
    if register_canonical_key == "FTTS_FT":
        return [
            CanonicalOutcome.HOME.value,
            CanonicalOutcome.AWAY.value,
            CanonicalOutcome.NO_GOAL.value,
        ]
    if register_canonical_key.startswith("TOTAL_GOALS_FT:"):
        return [CanonicalOutcome.OVER.value, CanonicalOutcome.UNDER.value]
    if register_canonical_key == "NFL_GAME_WINNER_FT":
        return [CanonicalOutcome.HOME.value, CanonicalOutcome.AWAY.value]
    if register_canonical_key.startswith("NFL_POINT_SPREAD_FT:"):
        return [CanonicalOutcome.HOME.value, CanonicalOutcome.AWAY.value]
    if register_canonical_key.startswith("NFL_TOTAL_POINTS_FT:"):
        return [CanonicalOutcome.OVER.value, CanonicalOutcome.UNDER.value]
    return []


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_fee_token(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(value).strip()


def executable_polymarket_token_ids(
    token_ids: list[OutcomeNativeId],
    *,
    event_id: str | None,
    market_id: str | None,
    condition_id: str | None = None,
    required_outcomes: list[str] | None = None,
) -> list[OutcomeNativeId]:
    """Return real CLOB token IDs, or empty when identity is not executable."""

    from sports_hedge.nfl.normalize import is_fabricated_polymarket_clob_token

    event = str(event_id or "").strip()
    market = str(market_id or "").strip()
    condition = str(condition_id or "").strip()
    if not event or not market:
        return []
    cleaned: list[OutcomeNativeId] = []
    for item in token_ids:
        native = str(item.native_id or "").strip()
        if not native or is_fabricated_polymarket_clob_token(
            native, condition_id=condition, market_id=market
        ):
            return []
        cleaned.append(OutcomeNativeId(outcome=item.outcome, native_id=native))
    if not cleaned:
        return []
    required = [str(item).strip() for item in (required_outcomes or []) if str(item).strip()]
    if required and {item.outcome for item in cleaned} < set(required):
        return []
    return cleaned
