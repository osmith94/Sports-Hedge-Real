"""Durable COMPETITION_SEASON catalogue observation rows (Phase 1A).

Extends the existing Approved Market Catalogue store generically via
``MarketScope``. Season rows are per-venue observation listings with exact
native IDs. They are never register-admitted and are excluded from the
price-engine working set until a later exact-ID pricing slice.

PAPER / read-only. No venue writes.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    catalogue_row_supports_paper_eligibility,
)
from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.models import VenueName
from sports_hedge.domain.outrights import (
    CanonicalSeasonMarketIdentity,
    OutrightNativeListing,
)
from sports_hedge.matching.approved_register import REGISTER_VERSION
from sports_hedge.outrights.identity import canonical_season_market_id
from sports_hedge.outrights.native_ids import parse_clob_token_ids
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

OBSERVATION_KEY_PREFIX = "OBSERVATION"
SEASON_CATALOGUE_ISSUE = 437


class SeasonCatalogueError(ValueError):
    """Fail-closed season catalogue persistence."""


def season_observation_key(*, market_family: str, venue: VenueName | str) -> str:
    venue_token = venue.value if isinstance(venue, VenueName) else str(venue).strip().casefold()
    family = str(market_family or "").strip()
    if not family or not venue_token:
        raise SeasonCatalogueError("missing_observation_key_parts")
    return f"{OBSERVATION_KEY_PREFIX}:{family}:{venue_token}"


def is_season_observation_key(register_canonical_key: str) -> bool:
    return str(register_canonical_key or "").startswith(f"{OBSERVATION_KEY_PREFIX}:")


def _require_no_fixture_fields(identity: CanonicalSeasonMarketIdentity) -> None:
    subject = identity.subject
    for forbidden in ("home_team", "away_team", "kickoff_utc"):
        if forbidden in subject.model_fields_set and getattr(subject, forbidden, None) is not None:
            raise SeasonCatalogueError(f"fixture_fields_forbidden_on_competition_season:{forbidden}")


def _validate_listing_for_venue(listing: OutrightNativeListing) -> VenueName:
    try:
        venue = VenueName(listing.venue)
    except ValueError as exc:
        raise SeasonCatalogueError("unsupported_outright_venue") from exc
    if not listing.native_event_id.strip() or not listing.native_market_id.strip():
        raise SeasonCatalogueError("missing_native_event_or_market_id")
    if not listing.native_runner_or_contract_id.strip():
        raise SeasonCatalogueError("missing_native_runner_or_contract_id")
    if venue is VenueName.POLYMARKET:
        parse_clob_token_ids(list(listing.polymarket_clob_token_ids))
    return venue


def build_season_observation_row(
    *,
    identity: CanonicalSeasonMarketIdentity,
    listing: OutrightNativeListing,
    now: datetime,
    generation_id: str | None = None,
    catalogue_row_id: str | None = None,
) -> ApprovedMarketCatalogueRow:
    """Build one per-venue observation row. Does not admit equivalence."""

    _require_no_fixture_fields(identity)
    venue = _validate_listing_for_venue(listing)
    canonical_id = canonical_season_market_id(identity)
    key = season_observation_key(market_family=identity.market_family.value, venue=venue)
    row_id = catalogue_row_id or _season_row_id(canonical_id, key)
    matchbook_event_id = listing.native_event_id if venue is VenueName.MATCHBOOK else None
    matchbook_market_id = listing.native_market_id if venue is VenueName.MATCHBOOK else None
    matchbook_runners: list[OutcomeNativeId] = []
    if venue is VenueName.MATCHBOOK:
        matchbook_runners = [
            OutcomeNativeId(
                outcome=identity.participant_canonical_id,
                native_id=listing.native_runner_or_contract_id,
            )
        ]
    kalshi_event = listing.native_event_id if venue is VenueName.KALSHI else None
    kalshi_tickers = [listing.native_market_id] if venue is VenueName.KALSHI else []
    kalshi_outcomes: list[OutcomeNativeId] = []
    if venue is VenueName.KALSHI:
        kalshi_outcomes = [
            OutcomeNativeId(
                outcome=identity.participant_canonical_id,
                native_id=listing.native_runner_or_contract_id,
            )
        ]
    return ApprovedMarketCatalogueRow(
        catalogue_row_id=row_id,
        register_version=REGISTER_VERSION,
        register_canonical_key=key,
        canonical_event_id=canonical_id,
        competition=identity.subject.competition_code,
        home_canonical=None,
        away_canonical=None,
        kickoff_utc=None,
        matchbook_event_id=matchbook_event_id,
        matchbook_market_id=matchbook_market_id,
        matchbook_runner_ids=matchbook_runners,
        kalshi_event_ticker=kalshi_event,
        kalshi_market_tickers=kalshi_tickers,
        kalshi_outcome_ids=kalshi_outcomes,
        kalshi_series_ticker=listing.kalshi_series_ticker if venue is VenueName.KALSHI else None,
        family=identity.market_family.value,
        period=None,
        line=None,
        required_outcomes=[identity.participant_canonical_id],
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=now,
        last_confirmed_at=now,
        last_seen_generation_id=generation_id,
        content_version=1,
        market_scope=MarketScope.COMPETITION_SEASON,
        source_venue=venue,
        season_id=identity.subject.season_id,
        competition_code=identity.subject.competition_code,
        participant_type=identity.participant_type.value,
        participant_canonical_id=identity.participant_canonical_id,
        settlement_fingerprint_version=identity.settlement_fingerprint_version,
        expected_settlement_horizon=identity.expected_settlement_horizon,
        polymarket_event_id=listing.native_event_id if venue is VenueName.POLYMARKET else None,
        polymarket_market_id=listing.native_market_id if venue is VenueName.POLYMARKET else None,
        polymarket_condition_id=listing.polymarket_condition_id if venue is VenueName.POLYMARKET else None,
        polymarket_clob_token_ids=(
            list(listing.polymarket_clob_token_ids) if venue is VenueName.POLYMARKET else []
        ),
        polymarket_event_slug=listing.polymarket_event_slug if venue is VenueName.POLYMARKET else None,
        polymarket_event_ticker=listing.polymarket_event_ticker if venue is VenueName.POLYMARKET else None,
    )


def persist_season_observation(
    store: SqliteApprovedMarketCatalogueStore,
    *,
    identity: CanonicalSeasonMarketIdentity,
    listing: OutrightNativeListing,
    now: datetime,
    generation_id: str | None = None,
) -> ApprovedMarketCatalogueRow:
    row = build_season_observation_row(
        identity=identity,
        listing=listing,
        now=now,
        generation_id=generation_id,
    )
    if row.market_scope is not MarketScope.COMPETITION_SEASON:
        raise SeasonCatalogueError("market_scope_must_be_competition_season")
    if row.home_canonical or row.away_canonical or row.kickoff_utc is not None:
        raise SeasonCatalogueError("fixture_fields_forbidden_on_competition_season")
    if row.source_venue is VenueName.POLYMARKET:
        parse_clob_token_ids(row.polymarket_clob_token_ids)
    persisted = store.upsert_catalogue_row(row)
    if catalogue_row_supports_paper_eligibility(persisted, None):
        raise SeasonCatalogueError("season_observation_must_not_be_paper_eligible")
    return persisted


def season_rows_are_price_engine_excluded(row: ApprovedMarketCatalogueRow) -> bool:
    return row.market_scope is MarketScope.COMPETITION_SEASON or is_season_observation_key(
        row.register_canonical_key
    )


def _season_row_id(canonical_id: str, key: str) -> str:
    digest = sha256(f"{canonical_id}|{key}".encode()).hexdigest()[:24]
    return f"amc-season:{digest}"


def observation_native_payload(row: ApprovedMarketCatalogueRow) -> dict[str, Any]:
    return {
        "market_scope": row.market_scope.value if row.market_scope is not None else None,
        "source_venue": None if row.source_venue is None else row.source_venue.value,
        "season_id": row.season_id,
        "competition_code": row.competition_code,
        "participant_type": row.participant_type,
        "participant_canonical_id": row.participant_canonical_id,
        "settlement_fingerprint_version": row.settlement_fingerprint_version,
        "expected_settlement_horizon": row.expected_settlement_horizon,
        "matchbook_event_id": row.matchbook_event_id,
        "matchbook_market_id": row.matchbook_market_id,
        "kalshi_event_ticker": row.kalshi_event_ticker,
        "kalshi_market_tickers": list(row.kalshi_market_tickers),
        "polymarket_event_id": row.polymarket_event_id,
        "polymarket_market_id": row.polymarket_market_id,
        "polymarket_clob_token_ids": list(row.polymarket_clob_token_ids),
    }
