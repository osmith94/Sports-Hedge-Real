"""UNIVERSE catalogue maintenance for Issue #341 Phase 2.

Uses the Approved Match Register as the only runtime equivalence function.
Persists exact native IDs and compact Kalshi / Polymarket fee snapshots. Does not
fetch order books, run the solver, or create a durable price-engine queue.

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
    FEE_SOURCE_GAMMA_MARKET,
    FEE_SOURCE_GET_SERIES,
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    executable_polymarket_token_ids,
    family_period_line_from_key,
    kalshi_fee_snapshot_from_payloads,
    polymarket_fee_snapshot_from_payload,
    required_outcomes_for_key,
    semantic_kalshi_fee_snapshot_id,
)
from sports_hedge.application.target_competitions import (
    resolve_catalogue_competition_code,
    resolve_target_competition_from_kalshi_ticker,
)
from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.domain.models import VenueName
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
    from sports_hedge.nba.constants import CANONICAL_NBA_POINT_SPREAD, CANONICAL_NBA_TOTAL_POINTS
    from sports_hedge.ncaab.constants import (
        CANONICAL_NCAAB_POINT_SPREAD,
        CANONICAL_NCAAB_TOTAL_POINTS,
    )
    from sports_hedge.nfl.constants import CANONICAL_NFL_POINT_SPREAD, CANONICAL_NFL_TOTAL_POINTS

    if key.startswith(f"{CANONICAL_NFL_POINT_SPREAD}:"):
        return CANONICAL_NFL_POINT_SPREAD
    if key.startswith(f"{CANONICAL_NFL_TOTAL_POINTS}:"):
        return CANONICAL_NFL_TOTAL_POINTS
    if key.startswith(f"{CANONICAL_NBA_POINT_SPREAD}:"):
        return CANONICAL_NBA_POINT_SPREAD
    if key.startswith(f"{CANONICAL_NBA_TOTAL_POINTS}:"):
        return CANONICAL_NBA_TOTAL_POINTS
    if key.startswith(f"{CANONICAL_NCAAB_POINT_SPREAD}:"):
        return CANONICAL_NCAAB_POINT_SPREAD
    if key.startswith(f"{CANONICAL_NCAAB_TOTAL_POINTS}:"):
        return CANONICAL_NCAAB_TOTAL_POINTS
    return key


def family_key_from_kalshi_series(series_ticker: str | None) -> str | None:
    """Map a Kalshi series or event ticker onto a register family key."""

    ticker = str(series_ticker or "").strip().upper()
    if not ticker:
        return None
    from sports_hedge.tennis.constants import CANONICAL_TENNIS_MATCH_WINNER
    from sports_hedge.tennis.detect import approved_kalshi_tennis_series, is_tennis_kalshi_ticker

    if approved_kalshi_tennis_series(ticker) is not None:
        return CANONICAL_TENNIS_MATCH_WINNER
    if is_tennis_kalshi_ticker(ticker):
        return None
    from sports_hedge.nba.constants import (
        CANONICAL_NBA_GAME_WINNER,
        CANONICAL_NBA_POINT_SPREAD,
        CANONICAL_NBA_TOTAL_POINTS,
        NBA_KALSHI_GAME_SERIES,
        NBA_KALSHI_SPREAD_SERIES,
        NBA_KALSHI_TOTAL_SERIES,
    )
    from sports_hedge.nba.detect import approved_kalshi_nba_series
    from sports_hedge.ncaab.constants import (
        CANONICAL_NCAAB_GAME_WINNER,
        CANONICAL_NCAAB_POINT_SPREAD,
        CANONICAL_NCAAB_TOTAL_POINTS,
        NCAAB_KALSHI_GAME_SERIES,
        NCAAB_KALSHI_SPREAD_SERIES,
        NCAAB_KALSHI_TOTAL_SERIES,
    )
    from sports_hedge.ncaab.detect import (
        approved_kalshi_ncaab_series,
        rejected_kalshi_ncaab_series,
    )
    from sports_hedge.nfl.constants import (
        CANONICAL_NFL_GAME_WINNER,
        CANONICAL_NFL_POINT_SPREAD,
        CANONICAL_NFL_TOTAL_POINTS,
        NFL_KALSHI_GAME_SERIES,
        NFL_KALSHI_SPREAD_SERIES,
        NFL_KALSHI_TOTAL_SERIES,
    )
    from sports_hedge.nfl.detect import approved_kalshi_nfl_series

    ncaab_series = approved_kalshi_ncaab_series(ticker)
    if ncaab_series == NCAAB_KALSHI_GAME_SERIES:
        return CANONICAL_NCAAB_GAME_WINNER
    if ncaab_series == NCAAB_KALSHI_SPREAD_SERIES:
        return CANONICAL_NCAAB_POINT_SPREAD
    if ncaab_series == NCAAB_KALSHI_TOTAL_SERIES:
        return CANONICAL_NCAAB_TOTAL_POINTS
    if rejected_kalshi_ncaab_series(ticker):
        return None
    nfl_series = approved_kalshi_nfl_series(ticker)
    if nfl_series == NFL_KALSHI_GAME_SERIES:
        return CANONICAL_NFL_GAME_WINNER
    if nfl_series == NFL_KALSHI_SPREAD_SERIES:
        return CANONICAL_NFL_POINT_SPREAD
    if nfl_series == NFL_KALSHI_TOTAL_SERIES:
        return CANONICAL_NFL_TOTAL_POINTS
    nba_series = approved_kalshi_nba_series(ticker)
    if nba_series == NBA_KALSHI_GAME_SERIES:
        return CANONICAL_NBA_GAME_WINNER
    if nba_series == NBA_KALSHI_SPREAD_SERIES:
        return CANONICAL_NBA_POINT_SPREAD
    if nba_series == NBA_KALSHI_TOTAL_SERIES:
        return CANONICAL_NBA_TOTAL_POINTS
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
    """Exact registered venue identity for one canonical key.

    Soccer PAPER remains Matchbook↔Kalshi. NFL PAPER may attach Matchbook,
    Kalshi, and Polymarket exact IDs onto the same catalogue row. NBA PAPER
    attaches Kalshi and Polymarket exact IDs for GAME_WINNER only. Polymarket
    legs require real CLOB token IDs.
    """

    def __init__(
        self,
        *,
        register_canonical_key: str,
        matchbook: CanonicalMarket | None = None,
        kalshi: CanonicalMarket | None = None,
        polymarket: CanonicalMarket | None = None,
        kalshi_event_payload: dict[str, Any] | None = None,
        kalshi_series_payload: dict[str, Any] | None = None,
        polymarket_market_payload: dict[str, Any] | None = None,
        fee_source: str = FEE_SOURCE_GET_SERIES,
    ) -> None:
        present = [item for item in (matchbook, kalshi, polymarket) if item is not None]
        if len(present) < 2:
            raise ValueError("catalogue identity requires at least two venue markets")
        self.register_canonical_key = register_canonical_key
        self.matchbook = matchbook
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.kalshi_event_payload = kalshi_event_payload or {}
        self.kalshi_series_payload = kalshi_series_payload or {}
        self.polymarket_market_payload = polymarket_market_payload or {}
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
    generation_selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    allow_disappearance: bool = True,
) -> list[ApprovedMarketCatalogueRow]:
    """Upsert ACTIVE rows for registered pairs and invalidate missing families.

    A row may disappear only when that family's UNIVERSE discovery completed
    successfully and the exact register key was genuinely absent. Incomplete
    TOTAL/FTTS discovery leaves existing rows ACTIVE/unconfirmed/retryable.
    Catalogue completion does not require executable books or solver output.
    The complete read/modify/write runs in one SQLite transaction.
    An old-scope generation must not disappear rows outside its selected codes.
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
            generation_selected_codes=generation_selected_codes,
            allow_disappearance=allow_disappearance,
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
    generation_selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    allow_disappearance: bool = True,
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
        generation_selected_codes=generation_selected_codes,
        allow_disappearance=allow_disappearance,
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
    generation_selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    allow_disappearance: bool = True,
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
        key = str(pair.register_canonical_key or "").strip()
        if not key:
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
    selected = (
        None
        if generation_selected_codes is None
        else frozenset(str(code).strip() for code in generation_selected_codes if str(code).strip())
    )
    for existing in tx.list_rows_for_event(canonical_event_id):
        if not allow_disappearance:
            continue
        if existing.register_canonical_key in found_keys:
            continue
        if existing.row_state is not CatalogueRowState.ACTIVE:
            continue
        row_code = resolve_catalogue_competition_code(
            competition=existing.competition or competition,
            kalshi_series_ticker=existing.kalshi_series_ticker,
        )
        if selected is not None and (row_code is None or row_code not in selected):
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
    row_id = catalogue_row_id_for(canonical_event_id, register_canonical_key)
    existing = tx.get_row(row_id) or tx.get_row_for_identity(
        canonical_event_id, register_canonical_key
    )
    mb_event, mb_market, mb_runners = _venue_ids(pair.matchbook, register_canonical_key)
    kalshi_event, _kalshi_source, kalshi_outcomes = _venue_ids(pair.kalshi, register_canonical_key)
    tickers = kalshi_constituent_tickers(pair.kalshi) if pair.kalshi is not None else []
    series_ticker = str(
        pair.kalshi_series_payload.get("ticker")
        or pair.kalshi_event_payload.get("series_ticker")
        or ""
    ).strip()
    event_ticker = str(
        pair.kalshi_event_payload.get("event_ticker")
        or (kalshi_event or "")
        or ""
    ).strip() or None
    snapshot_id = None if existing is None else existing.kalshi_fee_snapshot_id
    if pair.kalshi is not None and (
        _kalshi_payloads_have_fee_evidence(pair.kalshi_event_payload, pair.kalshi_series_payload)
        or not snapshot_id
    ):
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
        snapshot_id = snapshot.snapshot_id
    pm_event, pm_market, pm_tokens = _polymarket_ids(pair.polymarket, register_canonical_key)
    pm_snapshot_id = None if existing is None else existing.polymarket_fee_snapshot_id
    if pair.polymarket is not None:
        from sports_hedge.fees.polymarket import market_payload_has_fee_evidence

        pm_payload = pair.polymarket_market_payload
        if market_payload_has_fee_evidence(pm_payload) or not pm_snapshot_id:
            pm_snapshot = polymarket_fee_snapshot_from_payload(
                pm_payload,
                captured_at=now,
                source=FEE_SOURCE_GAMMA_MARKET,
                confirmed_at=now,
                source_market_id=pm_market,
            )
            tx.insert_polymarket_fee_snapshot(pm_snapshot)
            pm_snapshot_id = pm_snapshot.snapshot_id
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
        matchbook_event_id=_prefer(mb_event, None if existing is None else existing.matchbook_event_id),
        matchbook_market_id=_prefer(mb_market, None if existing is None else existing.matchbook_market_id),
        matchbook_runner_ids=_prefer_list(
            mb_runners, [] if existing is None else existing.matchbook_runner_ids
        ),
        kalshi_event_ticker=_prefer(
            event_ticker or kalshi_event,
            None if existing is None else existing.kalshi_event_ticker,
        ),
        kalshi_market_tickers=_prefer_list(
            tickers, [] if existing is None else existing.kalshi_market_tickers
        ),
        kalshi_outcome_ids=_prefer_list(
            kalshi_outcomes, [] if existing is None else existing.kalshi_outcome_ids
        ),
        kalshi_series_ticker=_prefer(
            series_ticker or None,
            None if existing is None else existing.kalshi_series_ticker,
        ),
        polymarket_event_id=_prefer(
            pm_event, None if existing is None else existing.polymarket_event_id
        ),
        polymarket_market_id=_prefer(
            pm_market, None if existing is None else existing.polymarket_market_id
        ),
        polymarket_condition_id=_prefer(
            _polymarket_condition_id(pair.polymarket),
            None if existing is None else existing.polymarket_condition_id,
        ),
        polymarket_token_ids=_prefer_list(
            pm_tokens, [] if existing is None else existing.polymarket_token_ids
        ),
        family=family,
        period=period,
        line=line,
        required_outcomes=required_outcomes_for_key(register_canonical_key),
        kalshi_fee_snapshot_id=snapshot_id,
        polymarket_fee_snapshot_id=pm_snapshot_id,
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
    left: CanonicalMarket,
    right: CanonicalMarket,
    *,
    kalshi_event_payload: dict[str, Any] | None = None,
    kalshi_series_payload: dict[str, Any] | None = None,
    polymarket_market_payload: dict[str, Any] | None = None,
    fee_source: str = FEE_SOURCE_GET_SERIES,
) -> CataloguePairIdentity | None:
    key = registered_canonical_key(left, right)
    if key is None:
        return None
    by_venue = {left.source_venue: left, right.source_venue: right}
    polymarket = by_venue.get(VenueName.POLYMARKET)
    if polymarket is not None:
        tokens = executable_polymarket_token_ids(
            ordered_native_ids(polymarket, key),
            event_id=str(polymarket.event.source_event_id),
            market_id=str(polymarket.source_market_id),
            required_outcomes=required_outcomes_for_key(key),
        )
        if not tokens:
            polymarket = None
            by_venue.pop(VenueName.POLYMARKET, None)
            if len(by_venue) < 2:
                return None
    return CataloguePairIdentity(
        register_canonical_key=key,
        matchbook=by_venue.get(VenueName.MATCHBOOK),
        kalshi=by_venue.get(VenueName.KALSHI),
        polymarket=polymarket,
        kalshi_event_payload=kalshi_event_payload,
        kalshi_series_payload=kalshi_series_payload,
        polymarket_market_payload=polymarket_market_payload,
        fee_source=fee_source,
    )


def _venue_ids(
    market: CanonicalMarket | None, register_canonical_key: str
) -> tuple[str | None, str | None, list[OutcomeNativeId]]:
    if market is None:
        return None, None, []
    event_id = str(market.event.source_event_id or "").strip() or None
    market_id = str(market.source_market_id or "").strip() or None
    return event_id, market_id, ordered_native_ids(market, register_canonical_key)


def _polymarket_ids(
    market: CanonicalMarket | None, register_canonical_key: str
) -> tuple[str | None, str | None, list[OutcomeNativeId]]:
    event_id, market_id, runners = _venue_ids(market, register_canonical_key)
    tokens = executable_polymarket_token_ids(
        runners,
        event_id=event_id,
        market_id=market_id,
        required_outcomes=required_outcomes_for_key(register_canonical_key),
    )
    if not tokens:
        return None, None, []
    return event_id, market_id, tokens


def _polymarket_condition_id(market: CanonicalMarket | None) -> str | None:
    if market is None:
        return None
    extra = getattr(market, "source_condition_id", None)
    text = str(extra or "").strip()
    return text or None


def _prefer(new: str | None, existing: str | None) -> str | None:
    incoming = str(new or "").strip()
    if incoming:
        return incoming
    held = str(existing or "").strip()
    return held or None


def _prefer_list(new: list[Any], existing: list[Any]) -> list[Any]:
    if new:
        return list(new)
    return list(existing)


def _kalshi_payloads_have_fee_evidence(
    event: dict[str, Any] | None, series: dict[str, Any] | None
) -> bool:
    """True when event/series carry fee fields, not just ticker identity."""

    event_payload = event or {}
    series_payload = series or {}
    keys = (
        "fee_type",
        "fee_multiplier",
        "fee_type_override",
        "fee_multiplier_override",
    )
    return any(
        event_payload.get(key) not in (None, "") or series_payload.get(key) not in (None, "")
        for key in keys
    )
