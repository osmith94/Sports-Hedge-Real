"""Issue #350 Phase 6: synthetic parity, empty-catalogue fallback, soak observer.

Clock-injected. Deterministic fixture/demo providers. No live HTTP.
Does not fabricate owner-live soak results. PAPER / read-only.

Validation-first: no scanner redesign, no timeout/cap increase, no durable
price queue, no second matcher.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import (
    SCAN_BUDGET_EXHAUSTED_REASON,
    CataloguePriceEngine,
    PriceEngineItemStatus,
    PriceEnginePriority,
)
from sports_hedge.application.provider_access import HEALTH_CAPACITY_SATURATED
from sports_hedge.application.scanner_observability import PriceEnginePublicStatus, PriceEngineTierStatus
from sports_hedge.application.scanner_phase6 import (
    DATA_CLASS_FIXTURE_DEMO,
    DATA_CLASS_OWNER_LIVE_OBSERVATION,
    DEFAULT_SOAK_DURATION_SECONDS,
    ScannerValidationSnapshot,
    SoakCaptureSummary,
    SoakEndpointSample,
    SoakLaneProgress,
    SoakRowState,
    accumulate_soak_report,
    capture_summary_from_reads,
    catalogue_register_identities,
    compare_register_parity,
    evaluate_soak_acceptance,
    legacy_register_identities_from_payloads,
    observer_snapshot,
    pair_identity_uses_register_only,
    snapshot_from_http_payloads,
    soak_harness_is_observer_only,
    venue_write_boundary_evidence,
)
from sports_hedge.config import Settings
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue316_catalogue_registry import (
    MB_EVENT_ID,
    _all_books,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _kalshi_total_event,
    _mb_event,
    _mb_ftts,
    _mb_match_odds,
    _mb_totals,
)
from test_issue341_approved_market_catalogue import (
    FOUR_KEYS,
    NOW,
    _collect_catalogue,
    _kalshi_events,
    _mb_markets,
    _series_map,
)
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
    FakeMatchbook,
    NEAR_KICKOFF,
    StubPaperScan,
    _engine,
    _hda_row,
    _hold_slot,
    _mb_btts as _price_engine_mb_btts,
    _mb_match_odds as _price_engine_mb_match_odds,
    _row,
)
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from sports_hedge.catalogue.corpus import ET_RULES, GAMEWIN_TEMPLATE, REGULATION
from sports_hedge.domain.models import VenueName


LOCKED_KEYS = {
    CANONICAL_MATCH_RESULT_FT,
    CANONICAL_BTTS_FT,
    "TOTAL_GOALS_FT:2.5",
    CANONICAL_FTTS_FT,
}


def _legacy_and_catalogue(
    store: SqliteApprovedMarketCatalogueStore,
    *,
    matchbook_event: dict[str, Any] | None = None,
    matchbook_markets: list[dict[str, Any]] | None = None,
    kalshi_events: list[dict[str, Any]] | None = None,
    series_by_ticker: dict[str, dict[str, Any]] | None = None,
) -> Any:
    event = matchbook_event or _mb_event()
    markets = matchbook_markets if matchbook_markets is not None else _mb_markets()
    events = kalshi_events if kalshi_events is not None else _kalshi_events()
    series = series_by_ticker or _series_map()
    legacy, forbidden = legacy_register_identities_from_payloads(
        matchbook_event=event,
        matchbook_markets=markets,
        kalshi_events=events,
        series_by_ticker=series,
    )
    catalogue = catalogue_register_identities(store.list_active())
    return compare_register_parity(legacy, catalogue, forbidden_admissions=forbidden)


@pytest.mark.asyncio
async def test_synthetic_old_vs_new_parity_for_four_locked_families() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        assert store.list_active() == []
        await _collect_catalogue(matchbook, kalshi, store)
        report = _legacy_and_catalogue(store)
        assert set(report.legacy_keys) == LOCKED_KEYS
        assert set(report.catalogue_keys) == LOCKED_KEYS
        assert report.forbidden_admissions == []
        assert report.dual_matcher is False
        assert report.matches is True
        assert pair_identity_uses_register_only() is True
        by_key = {row.register_canonical_key: row for row in store.list_active()}
        assert by_key[CANONICAL_MATCH_RESULT_FT].matchbook_market_id == "316010"
        assert {item.outcome for item in by_key[CANONICAL_MATCH_RESULT_FT].matchbook_runner_ids} == {
            "home",
            "draw",
            "away",
        }
        assert {item.outcome for item in by_key[CANONICAL_FTTS_FT].kalshi_outcome_ids} == {
            "home",
            "away",
            "no_goal",
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_totals_lines_remain_distinct_and_unsupported_fail_closed() -> None:
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
    integer_event = _kalshi_total_event("2.0")
    quarter_event = _kalshi_total_event("2.25")
    integer_event["event_ticker"] = "KXEPLTOTAL-26SEP20BRECHE-INT"
    quarter_event["event_ticker"] = "KXEPLTOTAL-26SEP20BRECHE-QTR"
    kalshi_events = [_kalshi_game_event(rules=REGULATION), total_event, integer_event, quarter_event]
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): mb_markets})
    kalshi = OverlapKalshi(kalshi_events, series_by_ticker=_series_map(), books=_all_books())
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        keys = {row.register_canonical_key for row in store.list_active()}
        assert "TOTAL_GOALS_FT:2.5" in keys
        assert "TOTAL_GOALS_FT:3.5" in keys
        assert "TOTAL_GOALS_FT:2.0" not in keys
        assert "TOTAL_GOALS_FT:2.25" not in keys
        report = _legacy_and_catalogue(
            store,
            matchbook_markets=mb_markets,
            kalshi_events=kalshi_events,
        )
        assert "TOTAL_GOALS_FT:2.5" in report.legacy_keys
        assert "TOTAL_GOALS_FT:3.5" in report.legacy_keys
        assert "TOTAL_GOALS_FT:2.0" not in report.legacy_keys
        assert report.matches is True
        two = next(row for row in store.list_active() if row.register_canonical_key == "TOTAL_GOALS_FT:2.5")
        three = next(row for row in store.list_active() if row.register_canonical_key == "TOTAL_GOALS_FT:3.5")
        assert two.matchbook_market_id != three.matchbook_market_id
        assert two.line == "2.5"
        assert three.line == "3.5"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_extra_time_to_qualify_and_missing_no_goal_never_enter_parity() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    missing_ng = _kalshi_ftts_event()
    missing_ng["markets"] = missing_ng["markets"][:2]
    matchbook = OverlapMatchbook(
        [_mb_event()],
        {str(MB_EVENT_ID): [_mb_match_odds(), _mb_ftts()]},
    )
    kalshi = OverlapKalshi(
        [_kalshi_game_event(rules=ET_RULES), missing_ng],
        series_by_ticker=_series_map(),
        books=_all_books(),
    )
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        report = _legacy_and_catalogue(
            store,
            matchbook_markets=[_mb_match_odds(), _mb_ftts()],
            kalshi_events=[_kalshi_game_event(rules=ET_RULES), missing_ng],
        )
        assert CANONICAL_MATCH_RESULT_FT not in report.legacy_keys
        assert CANONICAL_FTTS_FT not in report.legacy_keys
        assert CANONICAL_MATCH_RESULT_FT not in report.catalogue_keys
        assert CANONICAL_FTTS_FT not in report.catalogue_keys
        assert report.matches is True
        to_qualify = _kalshi_game_event(rules=GAMEWIN_TEMPLATE, drop_draw=True)
        incomplete_store = SqliteApprovedMarketCatalogueStore(":memory:")
        await _collect_catalogue(
            matchbook,
            OverlapKalshi([to_qualify], series_by_ticker=_series_map(), books=_all_books()),
            incomplete_store,
        )
        incomplete = _legacy_and_catalogue(
            incomplete_store,
            matchbook_markets=[_mb_match_odds(), _mb_ftts()],
            kalshi_events=[to_qualify],
        )
        assert CANONICAL_MATCH_RESULT_FT not in incomplete.catalogue_keys
        incomplete_store.close()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_empty_catalogue_universe_insert_derives_hot_and_background_items() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        assert store.list_active() == []
        await _collect_catalogue(matchbook, kalshi, store)
        rows = store.list_active()
        assert {row.register_canonical_key for row in rows} == FOUR_KEYS
        assert all(row.kalshi_fee_snapshot_id for row in rows)
        for row in rows:
            snapshot = store.get_fee_snapshot(row.kalshi_fee_snapshot_id or "")
            assert snapshot is not None
        engine, mb, ks, _layer = _engine(rows, store=store)
        working = engine.reconstruct()
        keys = {item.identity.register_canonical_key for item in working}
        assert keys == FOUR_KEYS
        assert mb.list_events_calls == 0
        assert mb.list_markets_calls == []
        assert ks.list_events_calls == 0
        assert ks.list_markets_calls == []
        distant = [item for item in working if item.priority is PriceEnginePriority.BACKGROUND]
        near = [item for item in working if item.priority is PriceEnginePriority.HOT]
        assert distant or near
        result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        assert result.scan_budget_exhausted is False
        assert mb.list_events_calls == 0
        assert mb.list_markets_calls == []
        assert ks.list_events_calls == 0
        assert "price_engine_queue" not in inspect.getsource(CataloguePriceEngine)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_from_populated_catalogue_prices_without_list_calls(tmp_path: Path) -> None:
    database = tmp_path / "phase6-catalogue.sqlite"
    store = SqliteApprovedMarketCatalogueStore(database)
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    try:
        await _collect_catalogue(matchbook, kalshi, store)
        expected = {
            row.register_canonical_key: (row.matchbook_market_id, tuple(row.kalshi_market_tickers))
            for row in store.list_active()
        }
        store.close()
        restarted = SqliteApprovedMarketCatalogueStore(database)
        engine, mb, ks, _layer = _engine([], store=restarted)
        rebuilt = engine.restart()
        assert {
            item.identity.register_canonical_key: (
                item.identity.matchbook_market_id,
                tuple(item.identity.kalshi_market_tickers),
            )
            for item in rebuilt
        } == expected
        await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert mb.list_events_calls == 0
        assert mb.list_markets_calls == []
        assert ks.list_events_calls == 0
        assert ks.list_markets_calls == []
        restarted.close()
    finally:
        if store._shared_connection is not None:
            store.close()


def _snapshot_from_engine(
    engine: CataloguePriceEngine,
    store: SqliteApprovedMarketCatalogueStore,
    *,
    coordinator: LiveRefreshCoordinator | None = None,
    hot: bool = False,
    universe: bool = False,
    background: bool = False,
) -> ScannerValidationSnapshot:
    coord = coordinator or LiveRefreshCoordinator(clock=lambda: NOW)
    coord.bind_price_engine(engine)
    coord.bind_catalogue_store(store)
    coord._hot_in_progress = hot
    coord._universe_in_progress = universe
    coord._background_in_progress = background
    return observer_snapshot(
        coordinator=coord,
        catalogue_store=store,
        settings=Settings(),
        now=NOW,
        data_kind=DATA_CLASS_FIXTURE_DEMO,
        soak_http_methods=["GET"],
    )


@pytest.mark.asyncio
async def test_soak_report_coverage_lists_unevaluated_rows_with_truthful_state() -> None:
    evaluated = _row(
        suffix="eval",
        kickoff=NEAR_KICKOFF,
        matchbook_market_id="316601",
        kalshi_event="KXEPLBTTS-EVAL",
    )
    deferred = _row(
        suffix="def",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316602",
        kalshi_event="KXEPLBTTS-DEF",
    )
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    engine, _mb, _ks, _layer = _engine([evaluated], store=store)
    await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    store.upsert_catalogue_row(deferred)
    engine.reconstruct()
    first = _snapshot_from_engine(engine, store, hot=True, universe=True)
    runtime = engine.item("amc-def")
    assert runtime is not None
    runtime.status = PriceEngineItemStatus.DEFERRED
    second = _snapshot_from_engine(engine, store, hot=True, universe=True, background=True)
    report = accumulate_soak_report([first, second], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.active_catalogue_row_count == 2
    assert report.rows_evaluated_at_least_once == 1
    assert report.coverage_ratio == 0.5
    assert [row.catalogue_row_id for row in report.unevaluated_rows] == ["amc-def"]
    assert report.unevaluated_rows[0].reason in {
        PriceEngineItemStatus.DEFERRED.value,
        "provider_capacity_saturated",
    }
    assert report.scan_budget_exhausted_price_engine == 0
    assert SCAN_BUDGET_EXHAUSTED_REASON not in first.price_engine.model_dump_json()
    assert SCAN_BUDGET_EXHAUSTED_REASON not in second.price_engine.model_dump_json()
    assert report.data_kind == DATA_CLASS_FIXTURE_DEMO


def test_soak_report_never_treats_scan_budget_exhausted_as_price_engine_state() -> None:
    silent = SoakRowState(
        catalogue_row_id="amc-silent",
        register_canonical_key=CANONICAL_BTTS_FT,
        canonical_event_id="evt-silent",
        row_state="ACTIVE",
        status="unknown",
        reason="",
        evaluated_at_least_once=False,
    )
    budget = SoakRowState(
        catalogue_row_id="amc-budget",
        register_canonical_key=CANONICAL_BTTS_FT,
        canonical_event_id="evt-budget",
        row_state="ACTIVE",
        status=SCAN_BUDGET_EXHAUSTED_REASON,
        reason=SCAN_BUDGET_EXHAUSTED_REASON,
        evaluated_at_least_once=False,
    )
    from sports_hedge.application.scanner_phase6 import SoakReport

    bad = SoakReport(
        data_kind=DATA_CLASS_FIXTURE_DEMO,
        unevaluated_rows=[silent, budget],
        scan_budget_exhausted_price_engine=1,
        paper_only=True,
    )
    codes = {item.code for item in evaluate_soak_acceptance(bad)}
    assert "scan_budget_exhausted" in codes
    assert "silent_active_row" in codes
    healthy_status = PriceEnginePublicStatus(
        hot=PriceEngineTierStatus(evaluated=1, working_set=1),
        background=PriceEngineTierStatus(not_started_this_cadence=1, working_set=1),
        durable_queue=False,
    )
    dumped = healthy_status.model_dump()
    assert SCAN_BUDGET_EXHAUSTED_REASON not in repr(dumped)


@pytest.mark.asyncio
async def test_provider_operation_and_tier_attribution_remain_truthful() -> None:
    access = __import__("asyncio")
    from sports_hedge.application.provider_access import ProviderAccessLayer

    layer = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    gate = access.Event()
    holders = [
        access.create_task(_hold_slot(layer, VenueName.KALSHI, gate)) for _ in range(4)
    ]
    await access.sleep(0.05)
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="cap6", kickoff=NEAR_KICKOFF, matchbook_market_id="316620", kalshi_event="KXEPLBTTS-CAP6")],
        access=layer,
    )
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    gate.set()
    await access.gather(*holders)
    assert result.scan_budget_exhausted is False
    assert result.provider_capacity_saturated is True
    hot = engine.public_status(now=NOW).hot
    kalshi_ops = hot.operation_health.get(VenueName.KALSHI.value) or {}
    assert kalshi_ops.get("order_book") == HEALTH_CAPACITY_SATURATED
    assert hot.venue_health.get(VenueName.MATCHBOOK.value) not in {
        "failed",
        "unavailable",
        "timeout",
        "market_timeout",
    }


@pytest.mark.asyncio
async def test_hot_overlaps_universe_and_background_receives_work() -> None:
    hot_row = _row(
        suffix="ovh",
        kickoff=NEAR_KICKOFF,
        matchbook_market_id="316630",
        kalshi_event="KXEPLBTTS-OVH",
    )
    bg_row = _row(
        suffix="ovb",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316631",
        kalshi_event="KXEPLBTTS-OVB",
    )
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    engine, _mb, _ks, _layer = _engine(
        [hot_row, bg_row],
        store=store,
        paper_scan=StubPaperScan(
            __import__("test_issue348_phase5_observability", fromlist=["_flat_decision"])._flat_decision()
        ),
    )
    await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    sample = _snapshot_from_engine(engine, store, hot=True, universe=True, background=True)
    report = accumulate_soak_report([sample], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.hot_universe_overlap_samples == 1
    assert report.hot_evaluated >= 1
    assert report.background_evaluated >= 1
    assert sample.hot_in_progress and sample.universe_in_progress


@pytest.mark.asyncio
async def test_background_to_hot_promotion_and_multi_market_aggregate() -> None:
    btts = _row(
        suffix="promo",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316640",
        kalshi_event="KXEPLBTTS-PROMO",
    )
    match = _hda_row("promo2", kickoff=DISTANT_KICKOFF)
    fixture_state = FixtureCurrentStateStore()
    matchbook = FakeMatchbook()
    matchbook.payloads[str(btts.matchbook_market_id)] = _price_engine_mb_btts(
        int(btts.matchbook_market_id)
    )
    matchbook.payloads[str(match.matchbook_market_id)] = _price_engine_mb_match_odds(
        int(match.matchbook_market_id)
    )
    engine, _mb, _ks, _layer = _engine(
        [btts, match],
        matchbook=matchbook,
        paper_scan=StubPaperScan(),
        fixture_state=fixture_state,
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert btts.canonical_event_id in result.promotions
    assert engine.classify_priority(engine.item("amc-promo").identity) is PriceEnginePriority.HOT
    await engine.drain_observability()
    assert btts.canonical_event_id in fixture_state.hot_identity_scope(NOW)
    assert engine.classify_priority(engine.item("amc-promo2").identity) is PriceEnginePriority.HOT


@pytest.mark.asyncio
async def test_health_build_info_live_refresh_and_validation_are_observer_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(paper_api, "get_live_refresh_coordinator", lambda: coordinator)
    source = inspect.getsource(paper_api.scanner_validation_snapshot)
    assert "collect_and_scan" not in source
    assert "_collect_report" not in source
    assert "list_events" not in source
    assert "bind_catalogue_store" not in source
    assert "reconstruct(" not in source
    assert "run_price_engine_slice" not in source
    assert "run_slice" not in source
    live_src = inspect.getsource(paper_api.live_refresh_status)
    assert "collect_and_scan" not in live_src
    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        build = await client.get("/build-info")
        live = await client.get("/paper/live-refresh")
        validation = await client.get("/paper/scanner-validation")
        assert health.status_code == 200
        assert build.status_code == 200
        assert live.status_code == 200
        assert validation.status_code == 200
        payload = validation.json()
        assert payload["observer_only"] is True
        assert payload["triggered_discovery"] is False
        assert payload["triggered_pricing"] is False
        assert payload["execution_enabled"] is False
        assert payload["scan_budget_exhausted_price_engine"] == 0
        assert SCAN_BUDGET_EXHAUSTED_REASON not in repr(payload.get("price_engine") or {})
        assert payload["coordinator_catalogue_bound"] is False
        assert payload["coordinator_price_engine_bound"] is False
        assert coordinator._catalogue_store is None
        assert coordinator._price_engine is None
        assert coordinator._next_hot_due is None
        assert coordinator._next_universe_due is None
        assert coordinator._next_background_due is None
        assert coordinator._hot_in_progress is False

        store = SqliteApprovedMarketCatalogueStore(":memory:")
        try:
            row = _row(
                suffix="getbind",
                kickoff=NEAR_KICKOFF,
                matchbook_market_id="316701",
                kalshi_event="KXEPLBTTS-GETBIND",
            )
            store.upsert_catalogue_row(row)
            engine, _mb, _ks, _layer = _engine([row], store=store)
            coordinator.bind_catalogue_store(store)
            coordinator.bind_price_engine(engine)
            coordinator._next_hot_due = NOW
            coordinator._next_universe_due = NOW
            coordinator._next_background_due = NOW
            coordinator._hot_in_progress = True
            bound = await client.get("/paper/scanner-validation")
            assert bound.status_code == 200
            bound_payload = bound.json()
            assert bound_payload["coordinator_catalogue_bound"] is True
            assert bound_payload["coordinator_price_engine_bound"] is True
            assert coordinator._catalogue_store is store
            assert coordinator._price_engine is engine
            assert coordinator._next_hot_due == NOW
            assert coordinator._next_universe_due == NOW
            assert coordinator._next_background_due == NOW
            assert coordinator._hot_in_progress is True
            assert coordinator._universe_in_progress is False
            assert coordinator._background_in_progress is False
        finally:
            store.close()


def test_soak_harness_read_only_boundary_and_timeouts_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert Settings.model_fields["paper_scan_matchbook_concurrency"].default == 4
    assert Settings.model_fields["paper_scan_kalshi_concurrency"].default == 4
    evidence = venue_write_boundary_evidence(settings=settings, soak_http_methods=["GET"])
    assert evidence.venue_write_invoked is False
    assert evidence.place_cancel_sign_reachable is False
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)
    assert soak_harness_is_observer_only() is True
    assert DEFAULT_SOAK_DURATION_SECONDS == 720
    soak_src = inspect.getsource(
        __import__("sports_hedge.application.scanner_phase6", fromlist=["run_http_soak"]).run_http_soak
    )
    assert "client.post(" not in soak_src
    assert "place_order" not in soak_src


def test_phase2_to_phase5_regression_bundles_remain_importable() -> None:
    import test_issue341_approved_market_catalogue as phase2
    import test_issue344_price_engine as phase3
    import test_issue346_item_completion_capture as phase4
    import test_issue348_phase5_observability as phase5

    assert hasattr(phase2, "test_universe_persists_four_canonical_keys_without_books")
    assert hasattr(phase3, "test_one_slow_item_lets_fast_siblings_use_remaining_slots")
    assert hasattr(phase4, "test_eligible_autofill_on_opens_at_item_completion")
    assert hasattr(phase4, "test_repeated_observation_is_idempotent")
    assert hasattr(phase4, "test_opening_leg_failure_is_explicit_paper_fill_rejected")
    assert hasattr(phase5, "test_health_and_build_info_dispatchable_during_hot_background_universe_load")
    assert hasattr(phase5, "test_four_busy_kalshi_slots_are_kalshi_deferred_not_matchbook_failed")
    assert hasattr(phase5, "test_positive_near_background_promotes_before_projection_drains")


@pytest.mark.asyncio
async def test_one_slow_matchbook_and_kalshi_item_do_not_fail_siblings() -> None:
    from test_issue344_price_engine import test_one_slow_item_lets_fast_siblings_use_remaining_slots

    await test_one_slow_item_lets_fast_siblings_use_remaining_slots(
        PriceEnginePriority.HOT, NEAR_KICKOFF
    )
    matchbook = FakeMatchbook()
    matchbook.hang.add("316650")
    rows = [
        _row(
            suffix="slowmb",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316650",
            kalshi_event="KXEPLBTTS-SLOWMB",
        ),
        _row(
            suffix="fastmb",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316651",
            kalshi_event="KXEPLBTTS-FASTMB",
        ),
    ]
    engine, _mb, _ks, _layer = _engine(rows, matchbook=matchbook, timeout=0.05)
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert "amc-fastmb" in result.evaluated
    assert "amc-slowmb" in result.retry_wait
    assert result.scan_budget_exhausted is False


def test_soak_example_report_is_fixture_demo_only() -> None:
    sample = ScannerValidationSnapshot(
        observed_at=NOW,
        data_kind=DATA_CLASS_FIXTURE_DEMO,
        build={"git_sha": "fixture", "git_branch": "test", "source": "unavailable"},
        active_catalogue_row_count=2,
        rows=[
            SoakRowState(
                catalogue_row_id="amc-a",
                content_version=1,
                register_canonical_key=CANONICAL_BTTS_FT,
                canonical_event_id="evt-a",
                row_state="ACTIVE",
                priority="hot",
                status="evaluated",
                reason="evaluated",
                evaluated_at_least_once=True,
                last_priced_at=NOW,
            ),
            SoakRowState(
                catalogue_row_id="amc-b",
                content_version=1,
                register_canonical_key=CANONICAL_MATCH_RESULT_FT,
                canonical_event_id="evt-b",
                row_state="ACTIVE",
                priority="background",
                status="not_started_this_cadence",
                reason="not_started_this_cadence",
                evaluated_at_least_once=False,
            ),
        ],
        price_engine=PriceEnginePublicStatus(
            hot=PriceEngineTierStatus(working_set=1, evaluated=1),
            background=PriceEngineTierStatus(working_set=1, not_started_this_cadence=1),
        ),
        hot_in_progress=True,
        universe_in_progress=True,
        endpoint_samples=[
            SoakEndpointSample(path="/health", status_code=200, available=True, elapsed_ms=2.0)
        ],
    )
    later = sample.model_copy(update={"observed_at": NOW + timedelta(minutes=12)})
    report = accumulate_soak_report([sample, later], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.data_kind == DATA_CLASS_FIXTURE_DEMO
    assert report.coverage_ratio == 0.5
    assert report.unevaluated_rows[0].catalogue_row_id == "amc-b"
    assert report.hot_universe_overlap_samples == 2
    assert report.accepted is True
    assert "owner_live" not in report.data_kind


def _core_endpoints(
    *,
    health: bool = True,
    build: bool = True,
    live: bool = True,
    validation: bool = True,
    trades: bool = True,
    activity: bool = True,
) -> list[SoakEndpointSample]:
    def _sample(path: str, available: bool) -> SoakEndpointSample:
        return SoakEndpointSample(
            path=path,
            status_code=200 if available else None,
            available=available,
            elapsed_ms=1.5,
            error=None if available else "unavailable",
        )

    return [
        _sample("/health", health),
        _sample("/build-info", build),
        _sample("/paper/live-refresh", live),
        _sample("/paper/scanner-validation", validation),
        _sample("/paper/trades/active", trades),
        _sample("/paper/watchlist/activity", activity),
    ]


def _row_state(
    row_id: str,
    *,
    version: int = 1,
    evaluated: bool = False,
    status: str = "not_started_this_cadence",
    reason: str = "not_started_this_cadence",
    priority: str = "hot",
    error: str | None = None,
) -> SoakRowState:
    return SoakRowState(
        catalogue_row_id=row_id,
        content_version=version,
        register_canonical_key=CANONICAL_BTTS_FT,
        canonical_event_id=f"evt-{row_id}",
        row_state="ACTIVE",
        priority=priority,
        status=status,
        reason=reason,
        evaluated_at_least_once=evaluated,
        last_priced_at=NOW if evaluated else None,
        last_error_detail=error,
    )


def _usable_snapshot(
    rows: list[SoakRowState],
    *,
    sha: str = "fixture",
    endpoints: list[SoakEndpointSample] | None = None,
    capture: SoakCaptureSummary | None = None,
    promoted_ids: list[str] | None = None,
    hot: bool = True,
    universe: bool = True,
    background: bool = False,
    lane: SoakLaneProgress | None = None,
    bound: bool = True,
    count: int | None = None,
) -> ScannerValidationSnapshot:
    return ScannerValidationSnapshot(
        observed_at=NOW,
        data_kind=DATA_CLASS_FIXTURE_DEMO,
        usable=True,
        coordinator_catalogue_bound=bound,
        coordinator_price_engine_bound=bound,
        build={"git_sha": sha, "git_branch": "test"},
        active_catalogue_row_count=len(rows) if count is None else count,
        rows=rows,
        price_engine=PriceEnginePublicStatus(
            hot=PriceEngineTierStatus(working_set=1, evaluated=1 if any(r.evaluated_at_least_once for r in rows) else 0),
            background=PriceEngineTierStatus(
                working_set=int(any((r.priority or "") == "background" for r in rows)),
                evaluated=int(
                    any(
                        (r.priority or "") == "background" and r.evaluated_at_least_once
                        for r in rows
                    )
                ),
            ),
        ),
        hot_in_progress=hot,
        universe_in_progress=universe,
        background_in_progress=background,
        promoted_hot_ids=list(promoted_ids or []),
        lane_progress=lane or SoakLaneProgress(),
        capture=capture or SoakCaptureSummary(),
        endpoint_samples=endpoints
        or [SoakEndpointSample(path="/health", status_code=200, available=True, elapsed_ms=1.0)],
    )


def test_zero_samples_are_rejected() -> None:
    report = accumulate_soak_report([], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.accepted is False
    assert {item.code for item in report.hard_fails} >= {"no_usable_samples"}


def test_all_core_gets_unavailable_are_rejected() -> None:
    snapshot = snapshot_from_http_payloads(
        observed_at=NOW,
        health=None,
        build=None,
        live=None,
        validation=None,
        trades=None,
        activity=None,
        endpoint_samples=_core_endpoints(
            health=False, build=False, live=False, validation=False, trades=False, activity=False
        ),
        data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION,
        trades_available=False,
        activity_available=False,
    )
    assert snapshot.usable is False
    assert snapshot.rows == []
    assert snapshot.active_catalogue_row_count == 0
    report = accumulate_soak_report([snapshot], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION)
    codes = {item.code for item in report.hard_fails}
    assert report.accepted is False
    assert "no_usable_samples" in codes
    assert "core_endpoints_unavailable" in codes
    assert report.endpoint_samples


def test_missing_scanner_validation_does_not_pass_as_empty_catalogue() -> None:
    snapshot = snapshot_from_http_payloads(
        observed_at=NOW,
        health={"mode": "paper", "execution_enabled": False, "live_refresh": {"hot_in_progress": False}},
        build={"git_sha": "abc"},
        live={"price_engine": {"hot": {"working_set": 9}, "background": {"working_set": 4}}},
        validation=None,
        trades=[],
        activity=[],
        endpoint_samples=_core_endpoints(validation=False),
        data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION,
        trades_available=True,
        activity_available=True,
    )
    assert snapshot.usable is False
    assert snapshot.active_catalogue_row_count == 0
    assert snapshot.rows == []
    report = accumulate_soak_report([snapshot], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION)
    codes = {item.code for item in report.hard_fails}
    assert report.accepted is False
    assert "scanner_validation_unavailable" in codes
    assert report.active_catalogue_row_count == 0
    assert report.coverage_ratio is None


def test_stated_active_count_mismatch_is_rejected() -> None:
    snapshot = _usable_snapshot(
        [_row_state("amc-a", evaluated=True, status="evaluated", reason="evaluated")],
        count=2,
        endpoints=_core_endpoints(),
    )
    via_http = snapshot_from_http_payloads(
        observed_at=NOW,
        health={"mode": "paper"},
        build={"git_sha": "fixture"},
        live={},
        validation=snapshot.model_dump(mode="json"),
        trades=[],
        activity=[],
        endpoint_samples=_core_endpoints(),
        data_kind=DATA_CLASS_FIXTURE_DEMO,
        trades_available=True,
        activity_available=True,
    )
    assert via_http.usable is False
    report = accumulate_soak_report([via_http], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.accepted is False
    assert "catalogue_count_mismatch" in {item.code for item in report.hard_fails}


def test_build_sha_change_mid_soak_is_rejected() -> None:
    first = _usable_snapshot(
        [_row_state("amc-a", evaluated=True, status="evaluated", reason="evaluated")],
        sha="sha-one",
        hot=True,
        universe=True,
    )
    second = first.model_copy(
        update={
            "observed_at": NOW + timedelta(minutes=12),
            "build": {"git_sha": "sha-two", "git_branch": "test"},
        }
    )
    report = accumulate_soak_report([first, second], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.accepted is False
    assert report.build_sha_changed is True
    assert "build_sha_changed" in {item.code for item in report.hard_fails}


def test_coverage_is_content_version_identity() -> None:
    v1 = _usable_snapshot(
        [_row_state("amc-x", version=1, evaluated=True, status="evaluated", reason="evaluated")],
        hot=True,
        universe=True,
    )
    v2 = v1.model_copy(
        update={
            "observed_at": NOW + timedelta(minutes=1),
            "rows": [
                _row_state(
                    "amc-x",
                    version=2,
                    evaluated=False,
                    status="not_started_this_cadence",
                    reason="not_started_this_cadence",
                )
            ],
            "active_catalogue_row_count": 1,
        }
    )
    report = accumulate_soak_report([v1, v2], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.active_catalogue_row_count == 1
    assert report.rows_evaluated_at_least_once == 0
    assert report.coverage_ratio == 0.0
    assert report.unevaluated_rows[0].catalogue_row_id == "amc-x"
    assert report.unevaluated_rows[0].content_version == 2
    assert "amc-x:1" in report.evaluated_identities
    assert "amc-x:2" not in report.evaluated_identities
    assert report.accepted is True


def test_capture_outcomes_are_correlated_per_opportunity() -> None:
    open_a = capture_summary_from_reads(
        paper_autofill_enabled=True,
        trades=[{"opportunity_id": "opp-a", "trade_id": "tr-a", "state": "OPEN"}],
        activity=[
            {"opportunity_id": "opp-a", "event_id": "e-a1", "event_type": "paper_fill_attempted"},
            {"opportunity_id": "opp-b", "event_id": "e-b1", "event_type": "paper_fill_attempted"},
        ],
    )
    assert open_a.silent_opportunity_ids == ["opp-b"]
    first = _usable_snapshot(
        [_row_state("amc-a", evaluated=True, status="evaluated", reason="evaluated")],
        capture=open_a,
        hot=True,
        universe=True,
    )
    later_activity = capture_summary_from_reads(
        paper_autofill_enabled=True,
        trades=[{"opportunity_id": "opp-a", "trade_id": "tr-a", "state": "OPEN"}],
        activity=[
            {"opportunity_id": "opp-a", "event_id": "e-a1", "event_type": "paper_fill_attempted"},
        ],
    )
    second = first.model_copy(
        update={"observed_at": NOW + timedelta(minutes=1), "capture": later_activity}
    )
    report = accumulate_soak_report([first, second], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert report.accepted is False
    assert "silent_eligible_capture" in {item.code for item in report.hard_fails}
    assert report.capture.silent_opportunity_ids == ["opp-b"]

    rejected_b = capture_summary_from_reads(
        paper_autofill_enabled=True,
        trades=[{"opportunity_id": "opp-a", "trade_id": "tr-a", "state": "OPEN"}],
        activity=[
            {"opportunity_id": "opp-b", "event_id": "e-b1", "event_type": "paper_fill_attempted"},
            {"opportunity_id": "opp-b", "event_id": "e-b2", "event_type": "paper_fill_rejected"},
        ],
    )
    recovered = first.model_copy(update={"capture": rejected_b, "observed_at": NOW + timedelta(minutes=2)})
    ok = accumulate_soak_report([first, recovered], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert ok.capture.silent_opportunity_ids == []
    assert ok.accepted is True

    duplicate = capture_summary_from_reads(
        paper_autofill_enabled=True,
        trades=[
            {"opportunity_id": "opp-a", "trade_id": "tr-a", "state": "OPEN"},
            {"opportunity_id": "opp-a", "trade_id": "tr-a2", "state": "OPEN"},
        ],
        activity=[{"opportunity_id": "opp-a", "event_id": "e-a1", "event_type": "paper_fill_attempted"}],
    )
    dup_report = accumulate_soak_report(
        [first.model_copy(update={"capture": duplicate})],
        data_kind=DATA_CLASS_FIXTURE_DEMO,
    )
    assert "duplicate_open" in {item.code for item in dup_report.hard_fails}

    idempotent = capture_summary_from_reads(
        paper_autofill_enabled=True,
        trades=[
            {"opportunity_id": "opp-a", "trade_id": "tr-a", "state": "OPEN"},
            {"opportunity_id": "opp-a", "trade_id": "tr-a", "state": "OPEN"},
        ],
        activity=[
            {"opportunity_id": "opp-a", "event_id": "e-a1", "event_type": "paper_fill_attempted"},
            {"opportunity_id": "opp-a", "event_id": "e-a1", "event_type": "paper_fill_attempted"},
        ],
    )
    idem_report = accumulate_soak_report(
        [first.model_copy(update={"capture": idempotent})],
        data_kind=DATA_CLASS_FIXTURE_DEMO,
    )
    assert idem_report.capture.duplicate_open_ids == []
    assert idem_report.accepted is True


def test_runtime_invariants_are_derived_not_notes() -> None:
    healthy = _usable_snapshot(
        [
            _row_state("amc-a", evaluated=True, status="evaluated", reason="evaluated"),
            _row_state(
                "amc-b",
                evaluated=False,
                status="not_started_this_cadence",
                reason="not_started_this_cadence",
                priority="background",
            ),
        ],
        hot=True,
        universe=True,
    )
    notes_report = accumulate_soak_report(
        [healthy],
        data_kind=DATA_CLASS_FIXTURE_DEMO,
        notes=[
            "hot_blocked_universe",
            "background_starvation",
            "unrelated_timeout_leftover",
            "dual_matcher",
            "stale_projection_resurrection",
        ],
    )
    codes = {item.code for item in notes_report.hard_fails}
    assert "hot_blocked_universe" not in codes
    assert "background_starvation" not in codes
    assert "unrelated_timeout_leftover" not in codes
    assert "dual_matcher" not in codes
    assert notes_report.accepted is True

    blocked = healthy.model_copy(
        update={
            "data_kind": DATA_CLASS_OWNER_LIVE_OBSERVATION,
            "universe_in_progress": False,
            "lane_progress": SoakLaneProgress(
                universe_next_due_at=NOW,
                hot_last_completed_at=NOW,
            ),
        }
    )
    blocked_report = accumulate_soak_report(
        [blocked, blocked.model_copy(update={"observed_at": NOW + timedelta(minutes=12)})],
        data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION,
    )
    assert "hot_blocked_universe" in {item.code for item in blocked_report.hard_fails}

    starved = healthy.model_copy(
        update={
            "data_kind": DATA_CLASS_OWNER_LIVE_OBSERVATION,
            "hot_in_progress": False,
            "universe_in_progress": False,
            "lane_progress": SoakLaneProgress(background_next_due_at=NOW, background_working_set=1),
        }
    )
    starved_report = accumulate_soak_report(
        [starved, starved.model_copy(update={"observed_at": NOW + timedelta(minutes=12)})],
        data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION,
    )
    assert "background_starvation" in {item.code for item in starved_report.hard_fails}

    timeout = _usable_snapshot(
        [
            _row_state(
                "amc-slow",
                evaluated=False,
                status="retry_wait",
                reason="retry_wait",
                error="kalshi order_book timeout",
            ),
            _row_state(
                "amc-other",
                evaluated=False,
                status="failed",
                reason="scan_budget_exhausted",
            ),
        ],
        hot=True,
        universe=True,
    )
    timeout_report = accumulate_soak_report([timeout], data_kind=DATA_CLASS_FIXTURE_DEMO)
    codes = {item.code for item in timeout_report.hard_fails}
    assert "unrelated_timeout_leftover" in codes
    assert "scan_budget_exhausted_row" in codes

    first_promo = _usable_snapshot(
        [_row_state("amc-a", evaluated=True, status="evaluated", reason="evaluated")],
        promoted_ids=["evt-old"],
        hot=True,
        universe=True,
        background=True,
    )
    second_promo = first_promo.model_copy(
        update={
            "observed_at": NOW + timedelta(seconds=30),
            "promoted_hot_ids": ["evt-old", "evt-new"],
        }
    )
    promo_report = accumulate_soak_report([first_promo, second_promo], data_kind=DATA_CLASS_FIXTURE_DEMO)
    assert promo_report.promoted_hot_ids_baseline == ["evt-old"]
    assert promo_report.promoted_hot_ids_observed == ["evt-new"]
    assert promo_report.background_hot_promotions == 1


def test_architecture_only_dual_matcher_and_reset_quarantine_remain_code_facts() -> None:
    from sports_hedge.application.catalogue_maintenance import pair_identity_from_markets
    from sports_hedge.application.live_refresh import LiveRefreshCoordinator
    from sports_hedge.application.price_engine import CataloguePriceEngine

    assert pair_identity_uses_register_only() is True
    pair_src = inspect.getsource(pair_identity_from_markets)
    assert "MarketMatcher(" not in pair_src
    engine_src = inspect.getsource(CataloguePriceEngine)
    assert "reset_generation" in engine_src
    live_src = inspect.getsource(LiveRefreshCoordinator)
    assert "quarantine" in live_src.casefold() or "reset_generation" in live_src


def test_no_active_catalogue_rows_fail_closed() -> None:
    empty = _usable_snapshot([], hot=True, universe=True)
    report = accumulate_soak_report([empty], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION)
    assert report.accepted is False
    assert "no_active_catalogue_rows" in {item.code for item in report.hard_fails}


def _owner_live_ok(
    *,
    sha: str = "abc123def",
    endpoints: list[SoakEndpointSample] | None = None,
    capture: SoakCaptureSummary | None = None,
    evidence_errors: list[str] | None = None,
    count: int | None = None,
    rows: list[SoakRowState] | None = None,
) -> ScannerValidationSnapshot:
    snapshot = _usable_snapshot(
        rows
        or [_row_state("amc-a", evaluated=True, status="evaluated", reason="evaluated")],
        sha=sha,
        endpoints=endpoints if endpoints is not None else _core_endpoints(),
        capture=capture,
        bound=True,
        hot=True,
        universe=True,
        count=count,
    )
    return snapshot.model_copy(
        update={
            "data_kind": DATA_CLASS_OWNER_LIVE_OBSERVATION,
            "evidence_errors": list(evidence_errors or []),
        }
    )


def test_owner_live_corrupt_sample_is_not_averaged_away() -> None:
    healthy = _owner_live_ok()
    later = healthy.model_copy(update={"observed_at": NOW + timedelta(minutes=12)})
    sha_bad = snapshot_from_http_payloads(
        observed_at=NOW,
        health={"mode": "paper", "paper_autofill_enabled": False},
        build={"git_sha": "other-sha"},
        live={},
        validation=healthy.model_dump(mode="json"),
        trades=[],
        activity=[],
        endpoint_samples=_core_endpoints(),
        data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION,
        trades_available=True,
        activity_available=True,
    )
    sha_report = accumulate_soak_report(
        [sha_bad, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert sha_bad.usable is False
    assert "build_sha_inconsistent" in sha_bad.evidence_errors
    assert sha_report.accepted is False
    assert "build_sha_inconsistent" in {item.code for item in sha_report.hard_fails}

    mismatch = _owner_live_ok(count=2)
    mismatch_report = accumulate_soak_report(
        [mismatch, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert mismatch_report.accepted is False
    assert "catalogue_count_mismatch" in {item.code for item in mismatch_report.hard_fails}

    capture_bad = _owner_live_ok(
        capture=SoakCaptureSummary(paper_autofill_enabled=True, evidence_usable=False),
        endpoints=_core_endpoints(trades=False, activity=False),
        evidence_errors=["capture_evidence_unavailable"],
    )
    capture_ok = _owner_live_ok(
        capture=SoakCaptureSummary(paper_autofill_enabled=True, evidence_usable=True),
    ).model_copy(update={"observed_at": NOW + timedelta(minutes=12)})
    capture_report = accumulate_soak_report(
        [capture_bad, capture_ok], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert capture_report.accepted is False
    assert "capture_evidence_unavailable" in {item.code for item in capture_report.hard_fails}


def test_owner_live_core_endpoint_must_stay_available() -> None:
    healthy = _owner_live_ok()
    later = healthy.model_copy(update={"observed_at": NOW + timedelta(minutes=12)})
    health_blip = _owner_live_ok(endpoints=_core_endpoints(health=False))
    health_report = accumulate_soak_report(
        [health_blip, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert health_report.accepted is False
    assert "core_endpoints_unavailable" in {item.code for item in health_report.hard_fails}
    assert any(not item.available and item.path.endswith("/health") for item in health_report.endpoint_samples)

    live_blip = _owner_live_ok(endpoints=_core_endpoints(live=False))
    live_report = accumulate_soak_report(
        [live_blip, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert live_report.accepted is False
    assert "core_endpoints_unavailable" in {item.code for item in live_report.hard_fails}

    ok = accumulate_soak_report([healthy, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION)
    assert ok.accepted is True
    assert ok.observed_build_sha == "abc123def"

    non_2xx = _owner_live_ok(
        endpoints=[
            SoakEndpointSample(path="/health", status_code=503, available=True, elapsed_ms=1.0),
            *[item for item in _core_endpoints() if item.path != "/health"],
        ]
    )
    non_2xx_report = accumulate_soak_report(
        [non_2xx, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert non_2xx_report.accepted is False
    assert "core_endpoints_unavailable" in {item.code for item in non_2xx_report.hard_fails}


def test_owner_live_missing_build_sha_is_rejected() -> None:
    snapshot = _owner_live_ok(sha="")
    report = accumulate_soak_report([snapshot], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION)
    assert report.accepted is False
    assert "missing_build_sha" in {item.code for item in report.hard_fails}
    via_http = snapshot_from_http_payloads(
        observed_at=NOW,
        health={"mode": "paper"},
        build={"git_branch": "owner-live"},
        live={},
        validation=_owner_live_ok(sha="").model_dump(mode="json"),
        trades=[],
        activity=[],
        endpoint_samples=_core_endpoints(),
        data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION,
        trades_available=True,
        activity_available=True,
    )
    http_report = accumulate_soak_report(
        [via_http], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert via_http.evidence_errors
    assert "missing_build_sha" in {item.code for item in http_report.hard_fails}

    later = _owner_live_ok().model_copy(update={"observed_at": NOW + timedelta(minutes=12)})
    mixed = accumulate_soak_report(
        [snapshot, later], data_kind=DATA_CLASS_OWNER_LIVE_OBSERVATION
    )
    assert mixed.accepted is False
    assert mixed.observed_build_sha == "abc123def"
    assert "missing_build_sha" in {item.code for item in mixed.hard_fails}
