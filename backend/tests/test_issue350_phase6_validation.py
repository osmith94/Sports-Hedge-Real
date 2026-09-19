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
    DEFAULT_SOAK_DURATION_SECONDS,
    ScannerValidationSnapshot,
    SoakEndpointSample,
    SoakRowState,
    accumulate_soak_report,
    catalogue_register_identities,
    compare_register_parity,
    evaluate_soak_acceptance,
    legacy_register_identities_from_payloads,
    observer_snapshot,
    pair_identity_uses_register_only,
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
    assert SCAN_BUDGET_EXHAUSTED_REASON not in report.model_dump_json()
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
    engine, _mb, _ks, _layer = _engine([hot_row, bg_row], store=store)
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
    engine, _mb, _ks, _layer = _engine(
        [btts, match],
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
        assert SCAN_BUDGET_EXHAUSTED_REASON not in repr(payload)


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

    await test_one_slow_item_lets_fast_siblings_use_remaining_slots()
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
