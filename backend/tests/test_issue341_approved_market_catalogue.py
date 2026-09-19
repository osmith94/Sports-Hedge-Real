"""Issue #341 Phase 2: durable approved-market catalogue + Kalshi fee snapshots.

Deterministic fixture/demo providers. Not owner-live quotes. Not modelled
probabilities. PAPER / read-only. No Phase 3 price-engine queue.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.approved_market_catalogue import (
    CATALOGUE_FORBIDDEN_COLUMNS,
    CATALOGUE_SCHEMA_VERSION,
    FEE_SNAPSHOT_FORBIDDEN_COLUMNS,
    FEE_STATUS_KNOWN,
    FEE_STATUS_PARTIAL,
    FEE_STATUS_UNKNOWN,
    HISTORY_FORBIDDEN_COLUMNS,
    CatalogueRowState,
    KalshiFeeSnapshotRecord,
    catalogue_row_supports_paper_eligibility,
    derived_price_engine_working_set,
    kalshi_fee_snapshot_from_payloads,
    semantic_kalshi_fee_snapshot_id,
)
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.catalogue_maintenance import (
    persist_universe_catalogue_pass,
    persist_universe_catalogue_pass_offloop,
)
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.universe_checkpoint import (
    FAT_CHECKPOINT_PAYLOAD_KEYS,
    UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES,
    UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
    UniverseCheckpointTooLarge,
    encode_durable_universe_checkpoint,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import resolve_kalshi_fee_metadata
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    REGISTER_VERSION,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import (
    MB_EVENT_ID,
    _DisabledPolymarket,
    _all_books,
    _costs,
    _fx,
    _kalshi_btts_event,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _kalshi_total_event,
    _mb_btts,
    _mb_dnb,
    _mb_event,
    _mb_ftts,
    _mb_match_odds,
    _mb_totals,
    _runner,
    _series,
)
from sports_hedge.catalogue.corpus import ET_RULES, GAMEWIN_TEMPLATE, REGULATION

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
FOUR_KEYS = {
    CANONICAL_MATCH_RESULT_FT,
    CANONICAL_BTTS_FT,
    "TOTAL_GOALS_FT:2.5",
    CANONICAL_FTTS_FT,
}


def _series_map() -> dict[str, dict[str, Any]]:
    return {
        "KXEPLGAME": _series("KXEPLGAME"),
        "KXEPLBTTS": _series("KXEPLBTTS"),
        "KXEPLTOTAL": _series("KXEPLTOTAL"),
        "KXEPLFTTS": _series("KXEPLFTTS"),
    }


def _kalshi_events() -> list[dict[str, Any]]:
    return [
        _kalshi_game_event(rules=REGULATION),
        _kalshi_btts_event(),
        _kalshi_total_event("2.5"),
        _kalshi_ftts_event(),
    ]


def _mb_markets() -> list[dict[str, Any]]:
    return [_mb_match_odds(), _mb_btts(), _mb_totals("2.5"), _mb_ftts()]


def _mb_handicap() -> dict[str, Any]:
    return {
        "id": 316061,
        "name": "Asian Handicap",
        "handicap": "-0.5",
        "runners": [_runner(51, "Brentford -0.5", "1.80"), _runner(52, "Chelsea +0.5", "2.00")],
    }


def _mb_double_chance() -> dict[str, Any]:
    return {
        "id": 316062,
        "name": "Double Chance",
        "runners": [
            _runner(61, "Brentford or Draw", "1.30"),
            _runner(62, "Draw or Chelsea", "1.45"),
            _runner(63, "Brentford or Chelsea", "1.20"),
        ],
    }


def _mb_team_total() -> dict[str, Any]:
    return {
        "id": 316063,
        "name": "Brentford Total Goals 1.5",
        "line": "1.5",
        "runners": [_runner(71, "Over 1.5", "1.90"), _runner(72, "Under 1.5", "1.90")],
    }


def _mb_player_prop() -> dict[str, Any]:
    return {
        "id": 316064,
        "name": "Player to Score",
        "runners": [_runner(81, "Yes", "2.50"), _runner(82, "No", "1.50")],
    }


class _NoBookKalshi(OverlapKalshi):
    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        self.book_calls.append(str(market_id))
        raise RuntimeError("order_book_must_not_gate_catalogue")


async def _collect_catalogue(
    matchbook: OverlapMatchbook,
    kalshi: OverlapKalshi,
    store: SqliteApprovedMarketCatalogueStore,
    **kwargs: Any,
):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        catalogue_store=store,
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            unbounded_cycle=True,
            **kwargs,
        )
    finally:
        repository.close()


def test_paper_boundary_and_timeouts_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert Settings.model_fields["paper_scan_venue_timeout_seconds"].default == 15
    assert Settings.model_fields["paper_scan_matchbook_concurrency"].default == 4
    assert Settings.model_fields["paper_scan_kalshi_concurrency"].default == 4
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)
    source = inspect.getsource(ReadOnlyCrossVenueCollector)
    for banned in ("place_order", "cancel_order", "sign_order"):
        assert banned not in source


def test_catalogue_and_fee_schema_forbid_policy_and_quote_columns() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        catalogue_cols = {name.casefold() for name in store.table_columns("approved_market_catalogue")}
        fee_cols = {name.casefold() for name in store.table_columns("kalshi_fee_snapshot")}
        history_cols = {
            name.casefold() for name in store.table_columns("approved_market_catalogue_history")
        }
        assert not (catalogue_cols & {item.casefold() for item in CATALOGUE_FORBIDDEN_COLUMNS})
        assert not (fee_cols & {item.casefold() for item in FEE_SNAPSHOT_FORBIDDEN_COLUMNS})
        assert not (history_cols & {item.casefold() for item in HISTORY_FORBIDDEN_COLUMNS})
        assert "paper_admission" not in catalogue_cols
        assert "settlement_assumption" not in catalogue_cols
        assert "live_execution_eligible" not in catalogue_cols
        assert "prior_row_state" in history_cols
        assert "new_row_state" in history_cols
        assert "prior_content_version" in history_cols
        assert "new_content_version" in history_cols
        assert "generation_id" in history_cols
        assert "recorded_at" in history_cols
        assert CATALOGUE_SCHEMA_VERSION == 1
    finally:
        store.close()


def test_compact_checkpoint_contract_unchanged() -> None:
    assert UNIVERSE_CHECKPOINT_SEMANTICS_VERSION == 2
    assert UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES == 256 * 1024
    payload = {
        "generation_id": 1,
        "generation_started_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "semantics_version": 2,
        "work_units": {},
        "series_work": {},
        "evaluated_ids": ["fx-1"],
    }
    encoded = encode_durable_universe_checkpoint(payload)
    assert len(encoded.encode("utf-8")) <= UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES
    from sports_hedge.application.universe_checkpoint import (
        checkpoint_from_payload,
        durable_checkpoint_has_fat_payload,
    )

    for key in FAT_CHECKPOINT_PAYLOAD_KEYS:
        fat = dict(payload)
        fat[key] = {"blob": "x"}
        assert durable_checkpoint_has_fat_payload(fat) is True
        assert checkpoint_from_payload(fat) is None
    huge = dict(payload)
    huge["evaluated_ids"] = [f"id-{index}" * 50 for index in range(5000)]
    with pytest.raises(UniverseCheckpointTooLarge):
        encode_durable_universe_checkpoint(huge)
    assert "approved_market_catalogue" not in payload
    assert "work_queue" not in payload


def test_known_and_partial_fee_snapshots_fail_closed() -> None:
    known = kalshi_fee_snapshot_from_payloads(
        series=_series("KXEPLGAME"),
        event={"event_ticker": "KXEPLGAME-26SEP20BRECHE", "series_ticker": "KXEPLGAME"},
        market_ticker="KXEPLGAME-26SEP20BRECHE-BRE",
        captured_at=NOW,
        source="get_series",
        snapshot_id="kfee:known",
        confirmed_at=NOW,
    )
    assert known.fee_resolution_status == FEE_STATUS_KNOWN
    assert known.fee_type == "quadratic"
    assert known.fee_multiplier == "1"
    assert known.fee_provenance == "series"
    assert known.fee_resolution_error is None

    partial_event = {
        "event_ticker": "KXEPLGAME-26SEP20BRECHE",
        "series_ticker": "KXEPLGAME",
        "fee_type_override": "quadratic",
    }
    resolved = resolve_kalshi_fee_metadata(event=partial_event, series=_series("KXEPLGAME"))
    assert resolved["fee_resolution_error"] == "partial_event_fee_override"
    assert resolved["fee_type"] is None
    assert resolved["fee_multiplier"] is None
    partial = kalshi_fee_snapshot_from_payloads(
        series=_series("KXEPLGAME"),
        event=partial_event,
        market_ticker="KXEPLGAME-26SEP20BRECHE-BRE",
        captured_at=NOW,
        source="event_payload",
        snapshot_id="kfee:partial",
        confirmed_at=NOW,
    )
    assert partial.fee_resolution_status == FEE_STATUS_PARTIAL
    assert partial.fee_type is None
    assert partial.fee_multiplier is None

    unknown = kalshi_fee_snapshot_from_payloads(
        series={"ticker": "KXEPLGAME"},
        event={"event_ticker": "KXEPLGAME-26SEP20BRECHE", "series_ticker": "KXEPLGAME"},
        market_ticker=None,
        captured_at=NOW,
        source="get_series",
        snapshot_id="kfee:unknown",
    )
    assert unknown.fee_resolution_status == FEE_STATUS_UNKNOWN
    assert unknown.fee_multiplier is None


def test_unknown_fee_snapshot_cannot_support_paper_eligibility() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        snapshot = KalshiFeeSnapshotRecord(
            snapshot_id="kfee:unknown-ref",
            series_ticker="KXEPLGAME",
            fee_resolution_status=FEE_STATUS_UNKNOWN,
            fee_resolution_error="missing_fee_type",
            captured_at=NOW,
            source="get_series",
        )
        store.upsert_fee_snapshot(snapshot)
        from sports_hedge.application.approved_market_catalogue import (
            ApprovedMarketCatalogueRow,
            OutcomeNativeId,
        )

        row = ApprovedMarketCatalogueRow(
            catalogue_row_id="amc:test",
            register_canonical_key=CANONICAL_MATCH_RESULT_FT,
            canonical_event_id="evt-1",
            matchbook_event_id="1",
            matchbook_market_id="10",
            matchbook_runner_ids=[
                OutcomeNativeId(outcome="home", native_id="1"),
                OutcomeNativeId(outcome="draw", native_id="2"),
                OutcomeNativeId(outcome="away", native_id="3"),
            ],
            kalshi_event_ticker="KXEPLGAME-1",
            kalshi_market_tickers=["KXEPLGAME-1-H"],
            kalshi_fee_snapshot_id=snapshot.snapshot_id,
            row_state=CatalogueRowState.ACTIVE,
            first_catalogued_at=NOW,
            last_confirmed_at=NOW,
            content_version=1,
        )
        store.upsert_catalogue_row(row)
        loaded = store.get_fee_snapshot(snapshot.snapshot_id)
        assert loaded is not None
        assert catalogue_row_supports_paper_eligibility(row, loaded) is False
        known = snapshot.model_copy(
            update={
                "snapshot_id": "kfee:known-ref",
                "fee_resolution_status": FEE_STATUS_KNOWN,
                "fee_type": "quadratic",
                "fee_multiplier": "1",
                "fee_resolution_error": None,
                "fee_provenance": "series",
            }
        )
        assert catalogue_row_supports_paper_eligibility(
            row.model_copy(update={"kalshi_fee_snapshot_id": known.snapshot_id}),
            known,
        )
        assert catalogue_row_supports_paper_eligibility(
            row.model_copy(update={"kalshi_fee_snapshot_id": None}),
            known,
        ) is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_universe_persists_four_canonical_keys_without_books() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = _NoBookKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        report = await _collect_catalogue(matchbook, kalshi, store)
        fixture = next(item for item in report.discovered_fixtures if item.matchbook_matched)
        rows = store.list_active()
        keys = {row.register_canonical_key for row in rows}
        assert keys == FOUR_KEYS
        assert all(row.register_version == REGISTER_VERSION for row in rows)
        assert all(row.schema_version == CATALOGUE_SCHEMA_VERSION for row in rows)
        assert all(row.row_state is CatalogueRowState.ACTIVE for row in rows)
        assert all(row.canonical_event_id == fixture.canonical_event_id for row in rows)
        by_key = {row.register_canonical_key: row for row in rows}
        assert by_key[CANONICAL_MATCH_RESULT_FT].matchbook_market_id == "316010"
        assert by_key[CANONICAL_BTTS_FT].matchbook_market_id == "316020"
        assert by_key["TOTAL_GOALS_FT:2.5"].line == "2.5"
        assert by_key[CANONICAL_FTTS_FT].matchbook_market_id == "316040"
        assert by_key[CANONICAL_MATCH_RESULT_FT].kalshi_market_tickers
        for row in rows:
            snapshot = store.get_fee_snapshot(row.kalshi_fee_snapshot_id or "")
            assert snapshot is not None
            assert snapshot.fee_resolution_status == FEE_STATUS_KNOWN
            assert snapshot.fee_type == "quadratic"
            assert snapshot.fee_multiplier == "1"
            assert snapshot.fee_provenance == "series"
            assert catalogue_row_supports_paper_eligibility(row, snapshot) is True
        working = store.active_price_engine_working_set()
        assert {item.register_canonical_key for item in working} == FOUR_KEYS
        # Catalogue completed even though every Kalshi book call failed.
        assert kalshi.book_calls
    finally:
        store.close()


@pytest.mark.asyncio
async def test_total_line_separation_and_unsafe_lines_rejected() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    two_five = _mb_totals("2.5")
    three_five = _mb_totals("3.5")
    three_five["id"] = 316031
    integer = _mb_totals("2.0")
    integer["id"] = 316032
    quarter = _mb_totals("2.25")
    quarter["id"] = 316033
    mb_markets = [_mb_match_odds(), two_five, three_five, integer, quarter]
    total_event = _kalshi_total_event("2.5")
    extra = _kalshi_total_event("3.5")
    total_event["markets"] = list(total_event["markets"]) + list(extra["markets"])
    integer = _kalshi_total_event("2.0")
    quarter = _kalshi_total_event("2.25")
    integer["event_ticker"] = "KXEPLTOTAL-26SEP20BRECHE-INT"
    quarter["event_ticker"] = "KXEPLTOTAL-26SEP20BRECHE-QTR"
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): mb_markets})
    kalshi = OverlapKalshi(
        [_kalshi_game_event(rules=REGULATION), total_event, integer, quarter],
        series_by_ticker=_series_map(),
        books=_all_books(),
    )
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        keys = {row.register_canonical_key for row in store.list_active()}
        assert "TOTAL_GOALS_FT:2.5" in keys
        assert "TOTAL_GOALS_FT:3.5" in keys
        assert "TOTAL_GOALS_FT:2.0" not in keys
        assert "TOTAL_GOALS_FT:2" not in keys
        assert "TOTAL_GOALS_FT:2.25" not in keys
        assert CANONICAL_MATCH_RESULT_FT in keys
        two = next(row for row in store.list_active() if row.register_canonical_key == "TOTAL_GOALS_FT:2.5")
        three = next(row for row in store.list_active() if row.register_canonical_key == "TOTAL_GOALS_FT:3.5")
        assert two.catalogue_row_id != three.catalogue_row_id
        assert two.line == "2.5"
        assert three.line == "3.5"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_extra_time_incomplete_and_missing_no_goal_never_active() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    missing_ng = _kalshi_ftts_event()
    missing_ng["markets"] = missing_ng["markets"][:2]
    matchbook = OverlapMatchbook(
        [_mb_event()],
        {str(MB_EVENT_ID): [_mb_match_odds(), _mb_ftts()]},
    )
    try:
        kalshi_et = OverlapKalshi(
            [_kalshi_game_event(rules=ET_RULES), missing_ng],
            series_by_ticker=_series_map(),
            books=_all_books(),
        )
        await _collect_catalogue(matchbook, kalshi_et, store)
        keys = {row.register_canonical_key for row in store.list_active()}
        assert CANONICAL_MATCH_RESULT_FT not in keys
        assert CANONICAL_FTTS_FT not in keys

        incomplete_store = SqliteApprovedMarketCatalogueStore(":memory:")
        kalshi_incomplete = OverlapKalshi(
            [_kalshi_game_event(rules=GAMEWIN_TEMPLATE, drop_draw=True)],
            series_by_ticker=_series_map(),
            books=_all_books(),
        )
        await _collect_catalogue(matchbook, kalshi_incomplete, incomplete_store)
        incomplete_keys = {row.register_canonical_key for row in incomplete_store.list_active()}
        assert CANONICAL_MATCH_RESULT_FT not in incomplete_keys
        incomplete_store.close()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_non_target_families_produce_no_row_and_no_kalshi_depth() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook(
        [_mb_event()],
        {
            str(MB_EVENT_ID): [
                *_mb_markets(),
                _mb_dnb(),
                _mb_handicap(),
                _mb_double_chance(),
                _mb_team_total(),
                _mb_player_prop(),
            ]
        },
    )
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        rows = store.list_rows_for_event(store.list_active()[0].canonical_event_id)
        keys = {row.register_canonical_key for row in rows}
        assert keys == FOUR_KEYS
        assert not any("DNB" in (row.family or "").upper() for row in rows)
        assert not any(row.family in {"draw_no_bet", "asian_handicap", "double_chance", "team_total", "player_props"} for row in rows)
        approved_tickers = {
            ticker
            for row in store.list_active()
            for ticker in row.kalshi_market_tickers
        }
        assert kalshi.book_calls
        assert set(kalshi.book_calls) <= approved_tickers
        assert "316050" not in {row.matchbook_market_id for row in rows}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_one_family_disappearance_does_not_invalidate_siblings() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        assert {row.register_canonical_key for row in store.list_active()} == FOUR_KEYS
        kalshi.events = [
            _kalshi_game_event(rules=REGULATION),
            _kalshi_total_event("2.5"),
            _kalshi_ftts_event(),
        ]
        await _collect_catalogue(matchbook, kalshi, store)
        rows = {row.register_canonical_key: row for row in store.list_rows_for_event(fixture_id)}
        assert rows[CANONICAL_BTTS_FT].row_state is CatalogueRowState.DISAPPEARED
        assert rows[CANONICAL_BTTS_FT].invalidation_reason
        assert rows[CANONICAL_MATCH_RESULT_FT].row_state is CatalogueRowState.ACTIVE
        assert rows["TOTAL_GOALS_FT:2.5"].row_state is CatalogueRowState.ACTIVE
        assert rows[CANONICAL_FTTS_FT].row_state is CatalogueRowState.ACTIVE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_terminal_fixture_marks_catalogue_rows_terminal() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        closed = _mb_event()
        closed["status"] = "closed"
        matchbook.events = [closed]
        await _collect_catalogue(matchbook, kalshi, store)
        rows = store.list_rows_for_event(fixture_id)
        assert rows
        assert all(row.row_state is CatalogueRowState.TERMINAL for row in rows)
        assert store.list_active() == []
        assert derived_price_engine_working_set(rows) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_reads_active_ids_without_list_markets(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    store = SqliteApprovedMarketCatalogueStore(database)
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        expected = {
            row.register_canonical_key: (
                row.matchbook_market_id,
                tuple(row.kalshi_market_tickers),
                row.content_version,
            )
            for row in store.list_active()
        }
        assert set(expected) == FOUR_KEYS
        store.close()
        restarted = SqliteApprovedMarketCatalogueStore(database)
        loaded = restarted.list_active()
        assert {
            row.register_canonical_key: (
                row.matchbook_market_id,
                tuple(row.kalshi_market_tickers),
                row.content_version,
            )
            for row in loaded
        } == expected
        working = restarted.active_price_engine_working_set()
        assert {item.register_canonical_key for item in working} == FOUR_KEYS
        assert all(item.matchbook_market_id for item in working)
        assert all(item.kalshi_market_tickers for item in working)
        matchbook.list_markets_calls.clear()
        kalshi.list_markets_calls.clear()
        # Reader path must not rediscover.
        assert matchbook.list_markets_calls == []
        assert kalshi.list_markets_calls == []
        restarted.close()
    finally:
        if store._shared_connection is not None:
            store.close()


@pytest.mark.asyncio
async def test_native_id_change_increments_content_version() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        original = next(
            row for row in store.list_active() if row.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        )
        assert original.content_version == 1
        changed = _mb_match_odds()
        changed["id"] = 316099
        matchbook.markets_by_id = {
            str(MB_EVENT_ID): [changed, _mb_btts(), _mb_totals("2.5"), _mb_ftts()]
        }
        await _collect_catalogue(matchbook, kalshi, store)
        updated = next(
            row for row in store.list_active() if row.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        )
        assert updated.catalogue_row_id == original.catalogue_row_id
        assert updated.matchbook_market_id == "316099"
        assert updated.content_version == original.content_version + 1
        siblings = [
            row
            for row in store.list_active()
            if row.register_canonical_key != CANONICAL_MATCH_RESULT_FT
        ]
        assert all(row.content_version == 1 for row in siblings)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_partial_event_override_is_not_known_on_catalogue_row() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    game = _kalshi_game_event(rules=REGULATION)
    game["fee_type_override"] = "quadratic"
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): [_mb_match_odds()]})
    kalshi = OverlapKalshi(
        [game],
        series_by_ticker={"KXEPLGAME": _series("KXEPLGAME")},
        books=_all_books(),
    )
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        rows = store.list_active()
        assert len(rows) == 1
        row = rows[0]
        assert row.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        snapshot = store.get_fee_snapshot(row.kalshi_fee_snapshot_id or "")
        assert snapshot is not None
        assert snapshot.fee_resolution_status == FEE_STATUS_PARTIAL
        assert snapshot.fee_multiplier is None
        assert catalogue_row_supports_paper_eligibility(row, snapshot) is False
    finally:
        store.close()


def test_no_phase3_durable_price_engine_queue() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        with store._connect() as connection:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "approved_market_catalogue" in names
        assert "kalshi_fee_snapshot" in names
        assert "approved_market_catalogue_history" in names
        assert "hot_work_item_v1" not in names
        assert "price_engine_queue" not in names
        persist_src = inspect.getsource(persist_universe_catalogue_pass_offloop)
        assert "asyncio.to_thread" in persist_src
        collector_persist = inspect.getsource(
            ReadOnlyCrossVenueCollector._persist_universe_catalogue_from_pairs
        )
        collector_terminal = inspect.getsource(
            ReadOnlyCrossVenueCollector._mark_universe_catalogue_terminal
        )
        assert "persist_universe_catalogue_pass_offloop" in collector_persist
        assert "persist_universe_catalogue_pass_offloop" in collector_terminal
        assert persist_universe_catalogue_pass.__doc__
    finally:
        store.close()


class _SlowCatalogueStore(SqliteApprovedMarketCatalogueStore):
    """Deliberately blocking store for event-loop dispatch regressions."""

    def __init__(self, database: str | Path = ":memory:", delay: float = 0.5) -> None:
        super().__init__(database)
        self.delay = delay
        self.started = threading.Event()
        self.threads: list[int] = []

    def run_in_transaction(self, mutator: Any) -> Any:
        self.threads.append(threading.get_ident())
        self.started.set()
        time.sleep(self.delay)
        return super().run_in_transaction(mutator)


@pytest.mark.asyncio
async def test_blocking_catalogue_persist_keeps_hot_heartbeat_dispatchable() -> None:
    store = _SlowCatalogueStore(":memory:", delay=0.5)
    coordinator = LiveRefreshCoordinator(clock=lambda: datetime.now(UTC))
    loop_thread = threading.get_ident()
    heartbeats = 0

    async def idle_tick(plan=None):
        del plan
        return None

    persist_task = asyncio.create_task(
        persist_universe_catalogue_pass_offloop(
            store,
            canonical_event_id="evt-block",
            competition="EPL",
            home_canonical="brentford",
            away_canonical="chelsea",
            kickoff_utc=NOW,
            pairs=[],
            now=NOW,
            generation_id="1",
            listed_ok=False,
            terminal=False,
        )
    )
    for _ in range(50):
        if store.started.is_set():
            break
        await asyncio.sleep(0.02)
    assert store.started.is_set()
    assert not persist_task.done()

    hot_task = asyncio.create_task(coordinator._hot_loop(idle_tick))
    try:
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            if coordinator.status.hot.last_heartbeat_at is not None:
                heartbeats += 1
                break
            await asyncio.sleep(0.02)
        assert not persist_task.done()
        assert coordinator.status.hot.last_heartbeat_at is not None
        assert heartbeats >= 1
        await persist_task
    finally:
        coordinator._stop.set()
        hot_task.cancel()
        with suppress(asyncio.CancelledError):
            await hot_task
        store.close()
    assert store.threads
    assert store.threads[0] != loop_thread


@pytest.mark.asyncio
async def test_catalogue_history_records_disappeared_restored_terminal_and_native_id() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        first = await _collect_catalogue(matchbook, kalshi, store, universe_generation_id=7)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        btts = next(
            row for row in store.list_active() if row.register_canonical_key == CANONICAL_BTTS_FT
        )
        match_result = next(
            row
            for row in store.list_active()
            if row.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        )
        insert_history = store.list_history(btts.catalogue_row_id)
        assert insert_history
        assert insert_history[0].prior_row_state is None
        assert insert_history[0].new_row_state == CatalogueRowState.ACTIVE.value
        assert insert_history[0].new_content_version == 1
        assert insert_history[0].generation_id == "7"

        kalshi.events = [
            _kalshi_game_event(rules=REGULATION),
            _kalshi_total_event("2.5"),
            _kalshi_ftts_event(),
        ]
        await _collect_catalogue(matchbook, kalshi, store, universe_generation_id=8)
        disappeared = store.get_row(btts.catalogue_row_id)
        assert disappeared is not None
        assert disappeared.row_state is CatalogueRowState.DISAPPEARED
        disappeared_history = store.list_history(btts.catalogue_row_id)
        assert [item.new_row_state for item in disappeared_history] == [
            CatalogueRowState.ACTIVE.value,
            CatalogueRowState.DISAPPEARED.value,
        ]
        assert disappeared_history[0].prior_row_state is None
        assert disappeared_history[1].prior_row_state == CatalogueRowState.ACTIVE.value
        assert disappeared_history[1].generation_id == "8"

        kalshi.events = _kalshi_events()
        await _collect_catalogue(matchbook, kalshi, store, universe_generation_id=9)
        restored = store.get_row(btts.catalogue_row_id)
        assert restored is not None
        assert restored.row_state is CatalogueRowState.ACTIVE
        restored_history = store.list_history(btts.catalogue_row_id)
        assert [item.new_row_state for item in restored_history] == [
            CatalogueRowState.ACTIVE.value,
            CatalogueRowState.DISAPPEARED.value,
            CatalogueRowState.ACTIVE.value,
        ]
        assert restored_history[1].new_row_state == CatalogueRowState.DISAPPEARED.value
        assert restored_history[2].generation_id == "9"

        original_match = store.get_row(match_result.catalogue_row_id)
        assert original_match is not None
        changed = _mb_match_odds()
        changed["id"] = 316099
        matchbook.markets_by_id = {
            str(MB_EVENT_ID): [changed, _mb_btts(), _mb_totals("2.5"), _mb_ftts()]
        }
        await _collect_catalogue(matchbook, kalshi, store, universe_generation_id=10)
        updated_match = store.get_row(match_result.catalogue_row_id)
        assert updated_match is not None
        assert updated_match.content_version == original_match.content_version + 1
        native_history = store.list_history(match_result.catalogue_row_id)
        assert native_history[0].new_content_version == 1
        assert native_history[-1].prior_content_version == original_match.content_version
        assert native_history[-1].new_content_version == updated_match.content_version
        prior_identity = json.loads(native_history[-1].prior_native_identity_json or "{}")
        new_identity = json.loads(native_history[-1].new_native_identity_json)
        assert prior_identity["matchbook_market_id"] == "316010"
        assert new_identity["matchbook_market_id"] == "316099"

        closed = _mb_event()
        closed["status"] = "closed"
        matchbook.events = [closed]
        await _collect_catalogue(matchbook, kalshi, store, universe_generation_id=11)
        terminal_row = store.get_row(match_result.catalogue_row_id)
        assert terminal_row is not None
        assert terminal_row.row_state is CatalogueRowState.TERMINAL
        terminal_history = store.list_history(match_result.catalogue_row_id)
        assert terminal_history[0].new_row_state == CatalogueRowState.ACTIVE.value
        assert terminal_history[-1].new_row_state == CatalogueRowState.TERMINAL.value
        assert terminal_history[-1].prior_row_state == CatalogueRowState.ACTIVE.value
        assert terminal_history[-1].generation_id == "11"
        btts_terminal = store.list_history(btts.catalogue_row_id)
        assert btts_terminal[0].new_row_state == CatalogueRowState.ACTIVE.value
        assert btts_terminal[-1].new_row_state == CatalogueRowState.TERMINAL.value
        assert fixture_id == match_result.canonical_event_id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_fee_snapshots_are_immutable_and_versioned_on_input_change() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): [_mb_match_odds()]})
    series_a = _series("KXEPLGAME")
    kalshi = OverlapKalshi(
        [_kalshi_game_event(rules=REGULATION)],
        series_by_ticker={"KXEPLGAME": series_a},
        books=_all_books(),
    )
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        row = store.list_active()[0]
        first_id = row.kalshi_fee_snapshot_id
        assert first_id
        first_snap = store.get_fee_snapshot(first_id)
        assert first_snap is not None
        assert first_snap.fee_resolution_status == FEE_STATUS_KNOWN
        assert first_snap.fee_multiplier == "1"
        assert first_snap.series_fee_multiplier in {1, "1"}
        expected_id = semantic_kalshi_fee_snapshot_id(first_snap)
        assert first_id == expected_id
        mutated = first_snap.model_copy(update={"fee_multiplier": "99"})
        store.upsert_fee_snapshot(mutated)
        unchanged = store.get_fee_snapshot(first_id)
        assert unchanged is not None
        assert unchanged.fee_multiplier == "1"

        series_b = dict(series_a)
        series_b["fee_multiplier"] = 2
        kalshi.series_by_ticker = {"KXEPLGAME": series_b}
        await _collect_catalogue(matchbook, kalshi, store)
        updated = store.list_active()[0]
        second_id = updated.kalshi_fee_snapshot_id
        assert second_id
        assert second_id != first_id
        assert updated.content_version == row.content_version + 1
        old_snap = store.get_fee_snapshot(first_id)
        new_snap = store.get_fee_snapshot(second_id)
        assert old_snap is not None and new_snap is not None
        assert old_snap.fee_multiplier == "1"
        assert new_snap.fee_multiplier == "2"
        assert new_snap.fee_resolution_status == FEE_STATUS_KNOWN
        assert catalogue_row_supports_paper_eligibility(updated, new_snap) is True
        fee_history = store.list_history(updated.catalogue_row_id)
        assert fee_history[-1].prior_kalshi_fee_snapshot_id == first_id
        assert fee_history[-1].new_kalshi_fee_snapshot_id == second_id
        assert fee_history[-1].new_content_version == updated.content_version

        game_partial_a = _kalshi_game_event(rules=REGULATION)
        game_partial_a["fee_type_override"] = "quadratic"
        kalshi.events = [game_partial_a]
        kalshi.series_by_ticker = {"KXEPLGAME": series_a}
        await _collect_catalogue(matchbook, kalshi, store)
        partial_a_row = store.list_active()[0]
        partial_a_id = partial_a_row.kalshi_fee_snapshot_id
        partial_a = store.get_fee_snapshot(partial_a_id or "")
        assert partial_a is not None
        assert partial_a.fee_resolution_status == FEE_STATUS_PARTIAL
        assert catalogue_row_supports_paper_eligibility(partial_a_row, partial_a) is False

        game_partial_b = _kalshi_game_event(rules=REGULATION)
        game_partial_b["fee_multiplier_override"] = 2
        kalshi.events = [game_partial_b]
        await _collect_catalogue(matchbook, kalshi, store)
        partial_b_row = store.list_active()[0]
        partial_b_id = partial_b_row.kalshi_fee_snapshot_id
        partial_b = store.get_fee_snapshot(partial_b_id or "")
        assert partial_b is not None
        assert partial_a_id != partial_b_id
        assert store.get_fee_snapshot(partial_a_id or "") is not None
        assert partial_b.fee_resolution_status == FEE_STATUS_PARTIAL
        assert partial_b.fee_multiplier is None
        assert catalogue_row_supports_paper_eligibility(partial_b_row, partial_b) is False
        assert semantic_kalshi_fee_snapshot_id(partial_a) == partial_a_id
        assert semantic_kalshi_fee_snapshot_id(partial_b) == partial_b_id
        snapshots = store.list_fee_snapshots()
        snapshot_ids = {item.snapshot_id for item in snapshots}
        assert first_id in snapshot_ids
        assert second_id in snapshot_ids
        assert partial_a_id in snapshot_ids
        assert partial_b_id in snapshot_ids
    finally:
        store.close()
