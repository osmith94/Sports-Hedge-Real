"""Operator UNIVERSE competition scope + manual BACKGROUND / UNIVERSE now.

PAPER / read-only. Canonical football codes are the operator model.
Fixture/demo providers only. Not owner-live quotes.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    required_outcomes_for_key,
)
from sports_hedge.application.catalogue_maintenance import (
    FamilyDiscoveryCompleteness,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    get_live_refresh_coordinator,
)
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.target_competitions import (
    DEFAULT_OPERATOR_COMPETITION_CODES,
    KALSHI_SERIES_NOT_VERIFIED,
    NO_VERIFIED_CROSS_VENUE_MAPPING,
    OUT_OF_SCOPE_COMPETITION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    VERIFIED_ALL_3,
    competition_has_verified_cross_venue_mapping,
    default_operator_competition_code_values,
    kalshi_series_tickers_for_codes,
    operator_competition_catalog,
    operator_verification_matrix,
    polymarket_series_ids_for_codes,
    resolve_target_competition,
    resolve_target_competition_from_kalshi_ticker,
    scope_matchbook_event,
)
from sports_hedge.config import Settings
from sports_hedge.matching.approved_register import CANONICAL_MATCH_RESULT_FT
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.operator_universe_scope import (
    SqliteOperatorUniverseScopeStore,
    bind_runtime_operator_universe_scope_store,
    discovery_filters_for_codes,
    get_operator_universe_scope_store,
    resolve_operator_universe_scope,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
DEFAULT_EIGHT = tuple(code.value for code in DEFAULT_OPERATOR_COMPETITION_CODES)


def _bind_scope(
    tmp_path: Path,
) -> tuple[LiveRefreshCoordinator, SqliteOperatorUniverseScopeStore, SqliteOperatorScannerSettingsStore]:
    scope_store = SqliteOperatorUniverseScopeStore(tmp_path / "universe-scope.sqlite")
    settings_store = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.bind_universe_scope_store(scope_store)
    coordinator.bind_operator_settings_store(settings_store)
    return coordinator, scope_store, settings_store


def _unbind(
    coordinator: LiveRefreshCoordinator,
    scope_store: SqliteOperatorUniverseScopeStore,
    settings_store: SqliteOperatorScannerSettingsStore,
) -> None:
    memory_scope = SqliteOperatorUniverseScopeStore(":memory:")
    memory_settings = SqliteOperatorScannerSettingsStore(":memory:")
    coordinator.bind_universe_scope_store(memory_scope)
    coordinator.bind_operator_settings_store(memory_settings)
    coordinator.reset()
    bind_runtime_operator_universe_scope_store(None)
    bind_runtime_operator_scanner_settings_store(None)
    coordinator._universe_scope_store = None
    coordinator._operator_store = None
    get_operator_universe_scope_store.cache_clear()
    scope_store.close()
    settings_store.close()
    memory_scope.close()
    memory_settings.close()


def _catalogue_row(*, suffix: str, competition: str, event_id: str | None = None) -> ApprovedMarketCatalogueRow:
    key = CANONICAL_MATCH_RESULT_FT
    return ApprovedMarketCatalogueRow(
        catalogue_row_id=f"amc-{suffix}",
        register_canonical_key=key,
        canonical_event_id=event_id or f"evt-{suffix}",
        competition=competition,
        home_canonical="Home",
        away_canonical="Away",
        kickoff_utc=NOW + timedelta(hours=12),
        matchbook_event_id="8801",
        matchbook_market_id="316001",
        matchbook_runner_ids=[
            OutcomeNativeId(outcome=outcome, native_id=f"mb-{suffix}-{outcome}")
            for outcome in required_outcomes_for_key(key)
        ],
        kalshi_event_ticker=f"KX-{suffix}",
        kalshi_market_tickers=[f"KX-{suffix}-GAME"],
        kalshi_outcome_ids=[
            OutcomeNativeId(outcome=outcome, native_id=f"ks-{suffix}-{outcome}")
            for outcome in required_outcomes_for_key(key)
        ],
        family="match_result",
        period="full_time",
        required_outcomes=required_outcomes_for_key(key),
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=NOW,
        last_confirmed_at=NOW,
        content_version=1,
    )


def test_clean_install_defaults_to_current_eight_competitions(tmp_path: Path) -> None:
    store = SqliteOperatorUniverseScopeStore(tmp_path / "empty.sqlite")
    try:
        resolved = resolve_operator_universe_scope(store)
        assert resolved.needs_first_run_confirmation is True
        assert resolved.source == "env_default"
        assert tuple(resolved.selected_competition_codes) == DEFAULT_EIGHT
        assert resolved.selected_count == 8
        assert resolved.scope_version == 0
        catalog_codes = {row.code for row in resolved.catalog}
        assert catalog_codes >= set(DEFAULT_EIGHT)
        assert len(resolved.catalog) == 31
        assert resolved.saved_default_competition_codes == list(DEFAULT_EIGHT)
        assert resolved.is_session_override is False
        assert "champions_league" in catalog_codes
        assert all("KX" not in row.code for row in resolved.catalog)
        assert all("ticker" not in row.selector_label.lower() for row in resolved.catalog)
    finally:
        store.close()


def test_saved_scope_survives_backend_restart(tmp_path: Path) -> None:
    path = tmp_path / "scope.sqlite"
    first = SqliteOperatorUniverseScopeStore(path)
    saved = first.save_scope(["premier_league", "champions_league", "mls"])
    assert saved.source == "operator"
    assert saved.needs_first_run_confirmation is False
    assert saved.scope_version == 1
    first.close()
    restarted = SqliteOperatorUniverseScopeStore(path)
    loaded = restarted.load()
    assert loaded is not None
    assert loaded.selected_competition_codes == ["premier_league", "champions_league", "mls"]
    assert loaded.saved_default_competition_codes == [
        "premier_league",
        "champions_league",
        "mls",
    ]
    assert loaded.scope_version == 1
    assert loaded.needs_first_run_confirmation is False
    restarted.close()


def test_http_hydrates_backend_scope_and_apply_does_not_call_providers(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("Apply must not trigger scanner work")

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        fresh = client.get("/paper/universe-scope")
        assert fresh.status_code == 200
        body = fresh.json()
        assert body["needs_first_run_confirmation"] is True
        assert body["selected_competition_codes"] == list(DEFAULT_EIGHT)
        assert "KXUCLGAME" not in str(body)
        live = client.get("/paper/live-refresh").json()
        assert live["universe_scope"]["selected_count"] == 8
        response = client.put(
            "/paper/universe-scope",
            json={
                "selected_competition_codes": ["premier_league", "champions_league"],
                "run_universe_now": False,
            },
        )
        assert response.status_code == 200
        saved = response.json()["universe_scope"]
        assert saved["selected_competition_codes"] == ["premier_league", "champions_league"]
        assert saved["saved_default_competition_codes"] == list(DEFAULT_EIGHT)
        assert saved["is_session_override"] is True
        assert saved["needs_first_run_confirmation"] is False
        assert saved["source"] == "env_default"
        persist = scope_store.load()
        assert persist is not None
        assert persist.saved_default_competition_codes == list(DEFAULT_EIGHT)
        assert ticks == []
        put_src = inspect.getsource(paper_api.put_universe_scope)
        assert "collect_and_scan" not in put_src
        assert "run_price_engine_slice" not in put_src
        assert "get_shared_provider_runtime" not in put_src
        empty = client.put("/paper/universe-scope", json={"selected_competition_codes": []})
        assert empty.status_code == 422
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, scope_store, settings_store)


def test_apply_and_run_universe_makes_selected_scope_due_now(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    coordinator._next_universe_due = NOW + timedelta(minutes=10)
    try:
        coordinator.apply_universe_scope(
            ["premier_league", "champions_league", "europa_league"],
            run_universe_now=True,
        )
        assert coordinator._next_universe_due <= coordinator.now()
        plan = coordinator.plan_universe_tick(now=coordinator.now())
        assert plan.lane == "universe"
        client = TestClient(app)
        queued = client.post("/paper/collect/universe")
        assert queued.status_code == 200
        assert queued.json()["universe_scope"]["manual_universe_state"] in {
            "pending",
            "running",
            "idle",
        }
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_adding_a_league_changes_derived_venue_discovery_filters() -> None:
    default_filters = discovery_filters_for_codes(DEFAULT_EIGHT)
    expanded = discovery_filters_for_codes([*DEFAULT_EIGHT, "champions_league", "mls"])
    assert "KXUCLGAME" not in default_filters["kalshi_series_tickers"]
    assert "KXMLSGAME" not in default_filters["kalshi_series_tickers"]
    assert "10204" not in default_filters["polymarket_series_ids"]
    assert "KXUCLGAME" in expanded["kalshi_series_tickers"]
    assert "KXMLSGAME" in expanded["kalshi_series_tickers"]
    assert "10204" in expanded["polymarket_series_ids"]
    assert "10189" in expanded["polymarket_series_ids"]
    src = inspect.getsource(paper_api._collect_report)
    assert "selected_competition_codes" in src
    collect_src = inspect.getsource(ReadOnlyCrossVenueCollector.collect_and_scan)
    assert "kalshi_series_tickers_for_codes" in collect_src
    assert "polymarket_series_ids_for_codes" in collect_src


def test_deselecting_a_league_is_out_of_scope_without_deleting_history() -> None:
    epl = scope_matchbook_event(
        {
            "id": 1,
            "name": "Arsenal vs Chelsea",
            "sport-name": "Football",
            "competition-name": "Premier League",
        },
        selected_codes=["champions_league"],
    )
    assert epl.allowed is False
    assert epl.reason == OUT_OF_SCOPE_COMPETITION
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    row = _catalogue_row(suffix="epl", competition="Premier League")
    store.upsert_catalogue_row(row)
    persist_universe_catalogue_pass(
        store,
        canonical_event_id=row.canonical_event_id,
        competition="Premier League",
        home_canonical="Home",
        away_canonical="Away",
        kickoff_utc=row.kickoff_utc,
        pairs=[],
        now=NOW,
        generation_id="2",
        family_discovery=FamilyDiscoveryCompleteness(
            matchbook_listing_complete=True,
            kalshi_series_results=({"series": "KXUCLGAME", "status": "ok"},),
            target_competition_code="champions_league",
        ),
        terminal=False,
        generation_selected_codes=["champions_league"],
        allow_disappearance=True,
    )
    kept = store.list_rows_for_event(row.canonical_event_id)
    assert kept[0].row_state is CatalogueRowState.ACTIVE
    assert kept[0].catalogue_row_id == row.catalogue_row_id
    store.close()


def test_old_scope_incomplete_generation_cannot_invalidate_new_scope_rows() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    row = _catalogue_row(suffix="ucl", competition="UEFA Champions League")
    store.upsert_catalogue_row(row)
    persist_universe_catalogue_pass(
        store,
        canonical_event_id=row.canonical_event_id,
        competition="UEFA Champions League",
        home_canonical="Home",
        away_canonical="Away",
        kickoff_utc=row.kickoff_utc,
        pairs=[],
        now=NOW,
        generation_id="old",
        family_discovery=FamilyDiscoveryCompleteness(
            matchbook_listing_complete=True,
            kalshi_series_results=({"series": "KXUCLGAME", "status": "ok"},),
            target_competition_code="champions_league",
        ),
        terminal=False,
        generation_selected_codes=["premier_league"],
        allow_disappearance=False,
    )
    kept = store.list_rows_for_event(row.canonical_event_id)
    assert kept[0].row_state is CatalogueRowState.ACTIVE
    store.close()


def test_open_paper_trade_remains_in_price_engine_after_deselect(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    epl = _catalogue_row(suffix="epl", competition="Premier League", event_id="evt-epl")
    ucl = _catalogue_row(suffix="ucl", competition="UEFA Champions League", event_id="evt-ucl")
    store.upsert_catalogue_row(epl)
    store.upsert_catalogue_row(ucl)
    engine = CataloguePriceEngine(catalogue_store=store)
    coordinator = LiveRefreshCoordinator(catalogue_store=store, price_engine=engine)
    monkeypatch.setattr(coordinator, "_open_paper_event_ids", lambda: frozenset({"evt-ucl"}))
    coordinator.bind_universe_scope_store(SqliteOperatorUniverseScopeStore(":memory:"))
    coordinator.apply_universe_scope(["premier_league"])
    ids = {item.identity.canonical_event_id for item in engine.items()}
    assert "evt-epl" in ids
    assert "evt-ucl" in ids
    monkeypatch.setattr(coordinator, "_open_paper_event_ids", lambda: frozenset())
    coordinator._reconstruct_price_engine_for_scope()
    ids = {item.identity.canonical_event_id for item in engine.items()}
    assert "evt-epl" in ids
    assert "evt-ucl" not in ids
    store.close()


def test_partial_paper_trade_is_exempt_from_scope_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#379 PARTIAL buy-in recovery stays ACTIVE TRADE-managed after deselect."""

    class _PartialTrade:
        state = PaperTradeState.PARTIAL
        canonical_event_id = "evt-ucl"

    class _Ops:
        def list_active_trades(self) -> list[_PartialTrade]:
            return [_PartialTrade()]

    monkeypatch.setattr(paper_api, "get_paper_operations_service", lambda *args, **kwargs: _Ops())
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    epl = _catalogue_row(suffix="epl", competition="Premier League", event_id="evt-epl")
    ucl = _catalogue_row(suffix="ucl", competition="UEFA Champions League", event_id="evt-ucl")
    store.upsert_catalogue_row(epl)
    store.upsert_catalogue_row(ucl)
    engine = CataloguePriceEngine(catalogue_store=store)
    coordinator = LiveRefreshCoordinator(catalogue_store=store, price_engine=engine)
    coordinator.bind_universe_scope_store(SqliteOperatorUniverseScopeStore(":memory:"))
    assert coordinator._open_paper_event_ids() == frozenset({"evt-ucl"})
    coordinator.apply_universe_scope(["premier_league"])
    ids = {item.identity.canonical_event_id for item in engine.items()}
    assert "evt-epl" in ids
    assert "evt-ucl" in ids
    store.close()


def test_repeated_run_universe_now_coalesces_instead_of_parallel_workers(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    try:
        coordinator._universe_in_progress = True
        coordinator.status = coordinator.status.model_copy(
            update={
                "universe": coordinator.status.universe.model_copy(
                    update={"cycle_in_progress": True}
                )
            }
        )
        first = coordinator.request_universe_run_now()
        second = coordinator.request_universe_run_now()
        assert first == "pending"
        assert second == "pending"
        assert coordinator._universe_run_now_pending is True
        src = inspect.getsource(paper_api.run_universe_now)
        assert "collect_and_scan" not in src
        assert "ReadOnlyCrossVenueCollector" not in src
        assert "request_universe_run_now" in src
    finally:
        coordinator._universe_in_progress = False
        _unbind(coordinator, scope_store, settings_store)


@pytest.mark.asyncio
async def test_manual_background_uses_price_engine_and_not_discovery(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    calls: list[str] = []

    async def slice_stub(priority, **_kwargs):
        calls.append(getattr(priority, "value", str(priority)))
        return type(
            "Result",
            (),
            {
                "issues": [],
                "decisions": [],
                "evaluated": [],
                "deferred": [],
                "not_started": [],
                "venue_health": {},
                "operation_health": {},
                "persist_failures": [],
            },
        )()

    coordinator.run_price_engine_slice = slice_stub  # type: ignore[method-assign]
    try:
        await coordinator.run_manual_background()
        assert calls == ["background"]
        src = inspect.getsource(paper_api.refresh_background_read_only_market_data)
        assert "run_manual_background" in src
        assert "collect_and_scan" not in src
        engine_src = inspect.getsource(CataloguePriceEngine.run_slice)
        assert ".list_events(" not in engine_src
        assert ".list_markets(" not in engine_src
        diagnostic_doc = inspect.getdoc(paper_api.collect_read_only_market_data) or ""
        assert "not a UNIVERSE" in diagnostic_doc
        universe_doc = inspect.getdoc(paper_api.run_universe_now) or ""
        assert "UNIVERSE" in universe_doc
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_stop_refuses_manual_lanes_but_allows_scope_edit(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    client = TestClient(app)
    try:
        stopped = client.post("/paper/scanner/stop")
        assert stopped.status_code == 200
        assert client.post("/paper/collect/hot", json={"maximum_execution_risk": 60}).status_code == 409
        assert client.post("/paper/collect/background").status_code == 409
        assert client.post("/paper/collect/universe").status_code == 409
        saved = client.put(
            "/paper/universe-scope",
            json={
                "selected_competition_codes": ["premier_league", "la_liga"],
                "run_universe_now": True,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["scanner_stopped"] is True
        assert body["universe_scope"]["selected_competition_codes"] == [
            "premier_league",
            "la_liga",
        ]
        assert body["universe_scope"]["saved_default_competition_codes"] == list(DEFAULT_EIGHT)
        assert body["universe_scope"]["is_session_override"] is True
        assert coordinator._universe_run_now_pending is False
        assert coordinator.plan_universe_tick(now=NOW).reason == "operator_stopped"
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_verified_new_competition_mappings_and_no_guessed_tickers() -> None:
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert len(catalog) == PRINCIPAL_OPERATOR_COMPETITION_COUNT
    for code in (
        "champions_league",
        "europa_league",
        "conference_league",
        "super_lig",
        "mls",
        "ligue_1",
        "liga_mx",
        "brasileirao",
        "nfl",
    ):
        assert catalog[code]["selectable"] is True
        assert catalog[code]["unavailable_reason"] is None
        assert catalog[code]["verification_status"] == VERIFIED_ALL_3
        item = resolve_target_competition(catalog[code]["display_name"])
        assert item is not None
        assert competition_has_verified_cross_venue_mapping(item)
    for code in ("league_two", "south_african_premiership"):
        assert catalog[code]["selectable"] is False
        assert catalog[code]["unavailable_reason"] == KALSHI_SERIES_NOT_VERIFIED
        assert catalog[code]["verification_status"] != VERIFIED_ALL_3
    assert resolve_target_competition_from_kalshi_ticker("KXUCLGAME") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXUCLWGAME") is None
    assert resolve_target_competition_from_kalshi_ticker("KXMLSASTGAME") is None
    assert resolve_target_competition_from_kalshi_ticker("KXDENSUPERLIGAGAME") is None
    assert resolve_target_competition_from_kalshi_ticker("KXLIGUE2GAME") is None
    assert resolve_target_competition_from_kalshi_ticker("KXJ2LEAGUEGAME") is None
    assert resolve_target_competition_from_kalshi_ticker("KXNFLGAME") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXNFLGAME-26SEP20INDKC") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXNFLGAMEFG") is None
    assert catalog["nfl"]["default_selected"] is False
    settings = Settings()
    assert "KXUCLGAME" not in settings.kalshi_series_tickers
    assert "10204" not in settings.resolved_polymarket_series_ids()
    assert tuple(default_operator_competition_code_values()) == DEFAULT_EIGHT
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    scan = (frontend / "components" / "run-paper-scan.tsx").read_text(encoding="utf-8")
    modal = (frontend / "components" / "football-competitions-modal.tsx").read_text(encoding="utf-8")
    api = (frontend / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "Football competitions" in scan
    assert "Manual BACKGROUND refresh" in scan
    assert "Run UNIVERSE now" in scan
    assert "needs_first_run_confirmation" in scan
    assert "Apply & Run UNIVERSE now" in modal
    assert "Select defaults" in modal
    assert "Save this selection as my default" in modal
    assert "Restore saved default" in modal
    assert "unavailable_reason" in modal
    assert "KXUCL" not in modal
    assert "/paper/universe-scope" in api
    assert "save_as_default" in api
    assert "/paper/collect/background" in api
    assert "/paper/collect/universe" in api
    assert NO_VERIFIED_CROSS_VENUE_MAPPING == "No verified cross-venue mapping"
    assert discovery_filters_for_codes(["champions_league"])["kalshi_series_tickers"]
    assert polymarket_series_ids_for_codes(["champions_league"]) == ["10204"]
    assert "KXUCLW" not in "".join(kalshi_series_tickers_for_codes(["champions_league"]))


def test_session_scope_does_not_overwrite_saved_default_or_survive_restart(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    try:
        applied = coordinator.apply_universe_scope(
            ["premier_league", "ligue_1"],
            save_as_default=False,
        )
        assert applied.selected_competition_codes == ["premier_league", "ligue_1"]
        assert applied.saved_default_competition_codes == list(DEFAULT_EIGHT)
        assert applied.is_session_override is True
        persisted = scope_store.load()
        assert persisted is not None
        assert persisted.saved_default_competition_codes == list(DEFAULT_EIGHT)
        coordinator.reset()
        coordinator.bind_universe_scope_store(scope_store)
        restarted = coordinator.effective_universe_scope()
        assert restarted.selected_competition_codes == list(DEFAULT_EIGHT)
        assert restarted.is_session_override is False
        coordinator._ensure_universe_generation(NOW)
        assert tuple(coordinator._universe_generation_selected_codes) == DEFAULT_EIGHT
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_saved_default_survives_restart_and_first_universe_uses_it(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    try:
        saved = coordinator.apply_universe_scope(
            ["premier_league", "champions_league", "mls"],
            save_as_default=True,
        )
        assert saved.saved_default_competition_codes == [
            "premier_league",
            "champions_league",
            "mls",
        ]
        assert saved.is_session_override is False
        coordinator.apply_universe_scope(
            ["premier_league", "ligue_1"],
            save_as_default=False,
        )
        assert coordinator.effective_universe_scope().is_session_override is True
        coordinator.reset()
        coordinator.bind_universe_scope_store(scope_store)
        hydrated = coordinator.effective_universe_scope()
        assert hydrated.selected_competition_codes == [
            "premier_league",
            "champions_league",
            "mls",
        ]
        coordinator._next_universe_due = NOW
        plan = coordinator.plan_universe_tick(now=NOW)
        assert plan.lane == "universe"
        assert plan.selected_competition_codes == [
            "premier_league",
            "champions_league",
            "mls",
        ]
        assert coordinator.generation_discovery_codes() == (
            "premier_league",
            "champions_league",
            "mls",
        )
        coordinator._ensure_universe_generation(NOW)
        assert list(coordinator._universe_generation_selected_codes) == [
            "premier_league",
            "champions_league",
            "mls",
        ]
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_restore_saved_default_restores_current_scope(tmp_path: Path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    client = TestClient(app)
    try:
        coordinator.apply_universe_scope(
            ["premier_league", "mls"],
            save_as_default=True,
        )
        coordinator.apply_universe_scope(
            ["premier_league", "ligue_1", "liga_mx"],
            save_as_default=False,
        )
        restored = client.put(
            "/paper/universe-scope",
            json={"restore_saved_default": True, "run_universe_now": False},
        )
        assert restored.status_code == 200
        body = restored.json()["universe_scope"]
        assert body["selected_competition_codes"] == ["premier_league", "mls"]
        assert body["saved_default_competition_codes"] == ["premier_league", "mls"]
        assert body["is_session_override"] is False
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_thirty_row_matrix_only_verified_all_three_are_selectable() -> None:
    catalog = operator_competition_catalog()
    matrix = operator_verification_matrix()
    assert len(catalog) == PRINCIPAL_OPERATOR_COMPETITION_COUNT
    assert len(matrix) == PRINCIPAL_OPERATOR_COMPETITION_COUNT
    assert {row["code"] for row in catalog} == {row.code for row in matrix}
    selectable = [row for row in matrix if row.selectable]
    disabled = [row for row in matrix if not row.selectable]
    assert all(row.verification_status == VERIFIED_ALL_3 for row in selectable)
    assert all(
        row.matchbook_status.value == "verified"
        and row.kalshi_status.value == "verified"
        and row.polymarket_status.value == "verified"
        and row.kalshi_series_tickers
        and row.polymarket_gamma_series_id
        for row in selectable
    )
    assert {row.code for row in disabled} == {"league_two", "south_african_premiership"}
    for row in disabled:
        assert row.kalshi_status.value == "unverified"
        assert row.kalshi_series_tickers == ()
        assert row.unavailable_reason == KALSHI_SERIES_NOT_VERIFIED
    for row in matrix:
        assert row.code == row.code.lower()
        assert not row.code.startswith("KX")
        if row.selectable:
            assert "KX" in "".join(row.kalshi_series_tickers)
    docs = Path(__file__).resolve().parents[2] / "docs" / "OPERATOR_COMPETITION_VERIFICATION_MATRIX.md"
    text = docs.read_text(encoding="utf-8")
    assert "OPERATOR_COMPETITION_REGISTRY_VERSION = 4" in text
    assert "VERIFIED_ALL_3" in text
    for row in matrix:
        assert row.code in text
        assert row.display_name in text

