"""UNIVERSE catalogue maintenance for Issue #341 Phase 2.

Uses the Approved Match Register as the only runtime equivalence function.
Persists exact native IDs and compact Kalshi fee snapshots. Does not fetch
order books, run the solver, or create a durable price-engine queue.

Catalogue disappearance is family-scoped. Fixture-wide listed_ok is not an
authority: GAME/BTTS discovery success must not imply TOTAL/FTTS completeness.

Synchronous SQLite mutation is intended to run off the scanner event loop
via `persist_universe_catalogue_pass_offloop`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from sports_hedge.application.approved_market_catalogue import (
    CATALOGUE_SCHEMA_VERSION,
    FEE_SOURCE_EVENT_PAYLOAD,
    FEE_SOURCE_GET_SERIES,
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    family_period_line_from_key,
    kalshi_fee_snapshot_from_payloads,
    required_outcomes_for_key,
    semantic_kalshi_fee_snapshot_id,
)
from sports_hedge.application.target_competitions import (
    resolve_target_competition_from_kalshi_ticker,
)
from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    CANONICAL_TOTAL_GOALS_FT,
    REGISTER_VERSION,
    registered_canonical_key,
)
from sports_hedge.persistence.approved_market_catalogue import (
    ApprovedMarketCatalogueTransaction,
    SqliteApprovedMarketCatalogueStore,
)

DISAPPEARED_FAMILY_REASON = "family_not_listed_this_generation"
TERMINAL_FIXTURE_REASON = "fixture_terminal"
KALSHI_SERIES_STATUS_OK = "ok"

# Longest suffix first so BTTS/FTTS/TOTAL cannot be confused with GAME.
_KALSHI_SERIES_FAMILY_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("BTTS", CANONICAL_BTTS_FT),
    ("FTTS", CANONICAL_FTTS_FT),
    ("TOTAL", CANONICAL_TOTAL_GOALS_FT),
    ("GAME", CANONICAL_MATCH_RESULT_FT),
)


@dataclass(frozen=True)
class FamilyDiscoveryCompleteness:
    """Explicit UNIVERSE family-discovery truth for one fixture/generation.

    This is the only catalogue-completeness authority. A family may be marked
    disappeared only when every required Matchbook source-event listing for the
    fixture finished successfully and the required Kalshi series discovery for
    that family completed successfully. Timeout / deferred / not-queried /
    budget-truncated families stay ACTIVE/unconfirmed.
    """

    matchbook_listing_complete: bool = False
    kalshi_series_results: tuple[dict[str, Any], ...] = ()
    kalshi_incomplete_family_keys: frozenset[str] = frozenset()
    target_competition_code: str | None = None


def catalogue_family_key(register_canonical_key: str) -> str:
    """Family identity used for completeness. TOTAL lines share one family."""

    key = str(register_canonical_key or "").strip()
    if key.startswith(f"{CANONICAL_TOTAL_GOALS_FT}:"):
        return CANONICAL_TOTAL_GOALS_FT
    return key


def family_key_from_kalshi_series(series_ticker: str | None) -> str | None:
    """Map a Kalshi series or event ticker onto a register family key."""

    ticker = str(series_ticker or "").strip().upper()
    if not ticker:
        return None
    head = ticker.split("-", 1)[0]
    for suffix, key in _KALSHI_SERIES_FAMILY_SUFFIXES:
        if head.endswith(suffix) or ticker.endswith(suffix):
            return key
    return None


def complete_family_keys(evidence: FamilyDiscoveryCompleteness) -> frozenset[str]:
    """Families whose upstream discovery completed for this fixture/generation.

    GAME/BTTS success does not imply TOTAL/FTTS completeness. A missing series
    row is not-queried. Any status other than ok is timeout/deferred/failed.
    """

    if not evidence.matchbook_listing_complete:
        return frozenset()
    wanted_code = str(evidence.target_competition_code or "").strip()
    if not wanted_code:
        return frozenset()
    complete: set[str] = set()
    for row in evidence.kalshi_series_results:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip() != KALSHI_SERIES_STATUS_OK:
            continue
        series = str(row.get("series") or "").strip()
        family = family_key_from_kalshi_series(series)
        if family is None:
            continue
        competition = resolve_target_competition_from_kalshi_ticker(series)
        if competition is None or competition.code.value != wanted_code:
            continue
        complete.add(family)
    return frozenset(complete - set(evidence.kalshi_incomplete_family_keys))


class CataloguePairIdentity:
    """Exact MB↔Kalshi identity for one registered canonical key."""

    def __init__(
        self,
        *,
        register_canonical_key: str,
        matchbook: CanonicalMarket,
        kalshi: CanonicalMarket,
        kalshi_event_payload: dict[str, Any] | None,
        kalshi_series_payload: dict[str, Any] | None,
        fee_source: str,
    ) -> None:
        self.register_canonical_key = register_canonical_key
        self.matchbook = matchbook
        self.kalshi = kalshi
        self.kalshi_event_payload = kalshi_event_payload or {}
        self.kalshi_series_payload = kalshi_series_payload or {}
        self.fee_source = fee_source


def catalogue_row_id_for(canonical_event_id: str, register_canonical_key: str) -> str:
    digest = sha256(f"{canonical_event_id}|{register_canonical_key}".encode()).hexdigest()[:24]
    return f"amc:{digest}"


def ordered_native_ids(market: CanonicalMarket, register_canonical_key: str) -> list[OutcomeNativeId]:
    by_outcome = {runner.outcome.value: runner.source_runner_id for runner in market.runners}
    return [
        OutcomeNativeId(outcome=outcome, native_id=by_outcome[outcome])
        for outcome in required_outcomes_for_key(register_canonical_key)
        if outcome in by_outcome
    ]


def kalshi_constituent_tickers(market: CanonicalMarket) -> list[str]:
    tickers: list[str] = []
    for runner in market.runners:
        ticker = str(runner.source_runner_id).rsplit(":", 1)[0].strip()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    if not tickers:
        source = str(market.source_market_id or "").strip()
        if source:
            tickers.append(source)
    return tickers


def persist_universe_catalogue_pass(
    store: SqliteApprovedMarketCatalogueStore,
    *,
    canonical_event_id: str,
    competition: str | None,
    home_canonical: str | None,
    away_canonical: str | None,
    kickoff_utc: datetime | None,
    pairs: list[CataloguePairIdentity],
    now: datetime,
    generation_id: str | None,
    family_discovery: FamilyDiscoveryCompleteness | None,
    terminal: bool,
) -> list[ApprovedMarketCatalogueRow]:
    """Upsert ACTIVE rows for registered pairs and invalidate missing families.

    A row may disappear only when that family's UNIVERSE discovery completed
    successfully and the exact register key was genuinely absent. Incomplete
    TOTAL/FTTS discovery leaves existing rows ACTIVE/unconfirmed/retryable.
    Catalogue completion does not require executable books or solver output.
    The complete read/modify/write runs in one SQLite transaction.
    """

    evidence = family_discovery or FamilyDiscoveryCompleteness()
    return store.run_in_transaction(
        lambda tx: _persist_universe_catalogue_pass_tx(
            tx,
            canonical_event_id=canonical_event_id,
            competition=competition,
            home_canonical=home_canonical,
            away_canonical=away_canonical,
            kickoff_utc=kickoff_utc,
            pairs=pairs,
            now=now,
            generation_id=generation_id,
            family_discovery=evidence,
            terminal=terminal,
        )
    )


async def persist_universe_catalogue_pass_offloop(
    store: SqliteApprovedMarketCatalogueStore,
    *,
    canonical_event_id: str,
    competition: str | None,
    home_canonical: str | None,
    away_canonical: str | None,
    kickoff_utc: datetime | None,
    pairs: list[CataloguePairIdentity],
    now: datetime,
    generation_id: str | None,
    family_discovery: FamilyDiscoveryCompleteness | None,
    terminal: bool,
) -> list[ApprovedMarketCatalogueRow]:
    """Bounded off-loop wrapper so SQLite catalogue I/O cannot stall HOT."""

    return await asyncio.to_thread(
        persist_universe_catalogue_pass,
        store,
        canonical_event_id=canonical_event_id,
        competition=competition,
        home_canonical=home_canonical,
        away_canonical=away_canonical,
        kickoff_utc=kickoff_utc,
        pairs=pairs,
        now=now,
        generation_id=generation_id,
        family_discovery=family_discovery,
        terminal=terminal,
    )


def _persist_universe_catalogue_pass_tx(
    tx: ApprovedMarketCatalogueTransaction,
    *,
    canonical_event_id: str,
    competition: str | None,
    home_canonical: str | None,
    away_canonical: str | None,
    kickoff_utc: datetime | None,
    pairs: list[CataloguePairIdentity],
    now: datetime,
    generation_id: str | None,
    family_discovery: FamilyDiscoveryCompleteness,
    terminal: bool,
) -> list[ApprovedMarketCatalogueRow]:
    if terminal:
        return tx.mark_fixture_terminal(
            canonical_event_id,
            reason=TERMINAL_FIXTURE_REASON,
            now=now,
            generation_id=generation_id,
        )

    found_keys: set[str] = set()
    rows: list[ApprovedMarketCatalogueRow] = []
    for pair in pairs:
        key = pair.register_canonical_key or registered_canonical_key(pair.matchbook, pair.kalshi)
        if key is None:
            continue
        found_keys.add(key)
        rows.append(
            _upsert_active_pair(
                tx,
                canonical_event_id=canonical_event_id,
                competition=competition,
                home_canonical=home_canonical,
                away_canonical=away_canonical,
                kickoff_utc=kickoff_utc,
                pair=pair,
                register_canonical_key=key,
                now=now,
                generation_id=generation_id,
            )
        )

    complete = complete_family_keys(family_discovery)
    for existing in tx.list_rows_for_event(canonical_event_id):
        if existing.register_canonical_key in found_keys:
            continue
        if existing.row_state is not CatalogueRowState.ACTIVE:
            continue
        if catalogue_family_key(existing.register_canonical_key) not in complete:
            continue
        disappeared = tx.mark_disappeared(
            existing.catalogue_row_id,
            reason=DISAPPEARED_FAMILY_REASON,
            now=now,
            generation_id=generation_id,
        )
        rows.append(disappeared or existing)
    return rows


def _upsert_active_pair(
    tx: ApprovedMarketCatalogueTransaction,
    *,
    canonical_event_id: str,
    competition: str | None,
    home_canonical: str | None,
    away_canonical: str | None,
    kickoff_utc: datetime | None,
    pair: CataloguePairIdentity,
    register_canonical_key: str,
    now: datetime,
    generation_id: str | None,
) -> ApprovedMarketCatalogueRow:
    family, period, line = family_period_line_from_key(register_canonical_key)
    mb_runners = ordered_native_ids(pair.matchbook, register_canonical_key)
    kalshi_outcomes = ordered_native_ids(pair.kalshi, register_canonical_key)
    tickers = kalshi_constituent_tickers(pair.kalshi)
    series_ticker = str(
        pair.kalshi_series_payload.get("ticker")
        or pair.kalshi_event_payload.get("series_ticker")
        or ""
    ).strip()
    event_ticker = str(
        pair.kalshi_event_payload.get("event_ticker") or pair.kalshi.event.source_event_id or ""
    ).strip() or None
    market_ticker = tickers[0] if tickers else None
    snapshot = kalshi_fee_snapshot_from_payloads(
        series=pair.kalshi_series_payload,
        event=pair.kalshi_event_payload,
        market_ticker=market_ticker,
        captured_at=now,
        source=pair.fee_source or FEE_SOURCE_EVENT_PAYLOAD,
        confirmed_at=now,
    )
    if series_ticker and snapshot.series_ticker != series_ticker:
        snapshot = snapshot.model_copy(update={"series_ticker": series_ticker})
        snapshot = snapshot.model_copy(
            update={"snapshot_id": semantic_kalshi_fee_snapshot_id(snapshot)}
        )
    tx.insert_fee_snapshot(snapshot)
    row_id = catalogue_row_id_for(canonical_event_id, register_canonical_key)
    existing = tx.get_row(row_id) or tx.get_row_for_identity(
        canonical_event_id, register_canonical_key
    )
    incoming = ApprovedMarketCatalogueRow(
        catalogue_row_id=row_id if existing is None else existing.catalogue_row_id,
        schema_version=CATALOGUE_SCHEMA_VERSION,
        register_version=REGISTER_VERSION,
        register_canonical_key=register_canonical_key,
        canonical_event_id=canonical_event_id,
        competition=competition,
        home_canonical=home_canonical,
        away_canonical=away_canonical,
        kickoff_utc=kickoff_utc,
        matchbook_event_id=str(pair.matchbook.event.source_event_id),
        matchbook_market_id=str(pair.matchbook.source_market_id),
        matchbook_runner_ids=mb_runners,
        kalshi_event_ticker=event_ticker or str(pair.kalshi.event.source_event_id),
        kalshi_market_tickers=tickers,
        kalshi_outcome_ids=kalshi_outcomes,
        kalshi_series_ticker=series_ticker or None,
        family=family,
        period=period,
        line=line,
        required_outcomes=required_outcomes_for_key(register_canonical_key),
        kalshi_fee_snapshot_id=snapshot.snapshot_id,
        row_state=CatalogueRowState.ACTIVE,
        invalidation_reason=None,
        first_catalogued_at=now if existing is None else existing.first_catalogued_at,
        last_confirmed_at=now,
        last_seen_generation_id=generation_id,
        content_version=1 if existing is None else existing.content_version,
    )
    if existing is not None and existing.content_identity_changed(incoming):
        incoming = incoming.model_copy(update={"content_version": existing.content_version + 1})
    return tx.upsert_catalogue_row(incoming)


def pair_identity_from_markets(
    matchbook: CanonicalMarket,
    kalshi: CanonicalMarket,
    *,
    kalshi_event_payload: dict[str, Any] | None = None,
    kalshi_series_payload: dict[str, Any] | None = None,
    fee_source: str = FEE_SOURCE_GET_SERIES,
) -> CataloguePairIdentity | None:
    key = registered_canonical_key(matchbook, kalshi)
    if key is None:
        return None
    return CataloguePairIdentity(
        register_canonical_key=key,
        matchbook=matchbook,
        kalshi=kalshi,
        kalshi_event_payload=kalshi_event_payload,
        kalshi_series_payload=kalshi_series_payload,
        fee_source=fee_source,
    )
