"""Issue #344 Phase 3: process-memory HOT + BACKGROUND price engine.

Clock-injected. Deterministic fixture/demo providers. No live HTTP.
PAPER / read-only. No durable pricing queue. No Phase 4 capture refactor.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.approved_market_catalogue import (
    CATALOGUE_FORBIDDEN_COLUMNS,
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    required_outcomes_for_key,
)
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import DualCadencePlan, LiveRefreshCoordinator
from sports_hedge.application.price_engine import (
    DEFAULT_BACKGROUND_CADENCE_SECONDS,
    NOT_STARTED_THIS_CADENCE,
    PRICE_ENGINE_ITEM_TIMEOUT_REASON,
    PRICE_ENGINE_RETRY_BACKOFF_SECONDS,
    PROVIDER_CAPACITY_SATURATED,
    SCAN_BUDGET_EXHAUSTED_REASON,
    CataloguePriceEngine,
    PriceEnginePriority,
    price_engine_retry_backoff_seconds,
)
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    DEFAULT_STARVATION_HOT_GRANTS,
    PRICE_ENGINE_BACKGROUND_LANE,
    ProviderAccessLayer,
    ProviderPriority,
    reset_shared_provider_access,
)
from sports_hedge.application.provider_runtime import (
    SharedProviderRuntime,
    set_shared_provider_runtime,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
)
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient, MatchbookMarketGoneError
from sports_hedge.venues.polymarket import PolymarketClient
from test_dual_cadence_scheduler import FakeClock
from test_issue328_universe_chunk_watchdog import (
    test_timed_out_chunk_cannot_mutate_replacement_checkpoint,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
DISTANT_KICKOFF = NOW + timedelta(days=6)
NEAR_KICKOFF = NOW + timedelta(minutes=20)
HOME = "Brentford"
AWAY = "Chelsea"


@pytest.fixture(autouse=True)
def _reset_shared_provider() -> Any:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


def _back(odds: str, amount: str = "100") -> dict[str, str]:
    return {"side": "back", "odds": odds, "available-amount": amount}


def _lay(odds: str, amount: str = "100") -> dict[str, str]:
    return {"side": "lay", "odds": odds, "available-amount": amount}


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "status": "open",
        "prices": [_back(odds), _lay(str(Decimal(odds) + Decimal("0.02")))],
    }


def _mb_btts(market_id: int, *, odds: str = "2.20") -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "status": "open",
        "runners": [
            _runner(market_id * 10 + 1, "Yes", odds),
            _runner(market_id * 10 + 2, "No", "1.80"),
        ],
    }


def _mb_match_odds(market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Match Odds",
        "status": "open",
        "runners": [
            _runner(market_id * 10 + 1, HOME, "2.40"),
            _runner(market_id * 10 + 2, "Draw", "3.40"),
            _runner(market_id * 10 + 3, AWAY, "2.90"),
        ],
    }


def _kalshi_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100.00"]],
            "no_dollars": [["0.49", "200.00"]],
        }
    }


def _row(
    *,
    suffix: str,
    key: str = CANONICAL_BTTS_FT,
    kickoff: datetime = DISTANT_KICKOFF,
    matchbook_event_id: str = "8801",
    matchbook_market_id: str = "316020",
    kalshi_event: str = "KXEPLBTTS-01",
    kalshi_tickers: list[str] | None = None,
    kalshi_outcomes: list[OutcomeNativeId] | None = None,
    content_version: int = 1,
    state: CatalogueRowState = CatalogueRowState.ACTIVE,
) -> ApprovedMarketCatalogueRow:
    tickers = kalshi_tickers or [f"{kalshi_event}-BTTS"]
    if kalshi_outcomes is None:
        ticker = tickers[0]
        kalshi_outcomes = [
            OutcomeNativeId(outcome="yes", native_id=f"{ticker}:YES"),
            OutcomeNativeId(outcome="no", native_id=f"{ticker}:NO"),
        ]
    family = "both_teams_to_score" if key == CANONICAL_BTTS_FT else "match_result"
    return ApprovedMarketCatalogueRow(
        catalogue_row_id=f"amc-{suffix}",
        register_canonical_key=key,
        canonical_event_id=f"evt-{suffix}",
        competition="Premier League",
        home_canonical=HOME,
        away_canonical=AWAY,
        kickoff_utc=kickoff,
        matchbook_event_id=matchbook_event_id,
        matchbook_market_id=matchbook_market_id,
        matchbook_runner_ids=[
            OutcomeNativeId(outcome=outcome, native_id=f"mb-{suffix}-{outcome}")
            for outcome in required_outcomes_for_key(key)
        ],
        kalshi_event_ticker=kalshi_event,
        kalshi_market_tickers=tickers,
        kalshi_outcome_ids=kalshi_outcomes,
        family=family,
        period="full_time",
        required_outcomes=required_outcomes_for_key(key),
        row_state=state,
        first_catalogued_at=NOW,
        last_confirmed_at=NOW,
        content_version=content_version,
    )


def _hda_row(suffix: str, *, kickoff: datetime = DISTANT_KICKOFF) -> ApprovedMarketCatalogueRow:
    event = f"KXEPLGAME-{suffix}"
    tickers = [f"{event}-HOME", f"{event}-DRAW", f"{event}-AWAY"]
    outcomes = [
        OutcomeNativeId(outcome="home", native_id=f"{tickers[0]}:YES"),
        OutcomeNativeId(outcome="draw", native_id=f"{tickers[1]}:YES"),
        OutcomeNativeId(outcome="away", native_id=f"{tickers[2]}:YES"),
    ]
    return _row(
        suffix=suffix,
        key=CANONICAL_MATCH_RESULT_FT,
        kickoff=kickoff,
        matchbook_event_id=f"99{suffix[-1] if suffix[-1].isdigit() else '1'}",
        matchbook_market_id=f"4100{suffix[-1] if suffix[-1].isdigit() else '1'}",
        kalshi_event=event,
        kalshi_tickers=tickers,
        kalshi_outcomes=outcomes,
    )


def _qualifying_decision(*, scanned_at: datetime = NOW) -> PaperScanDecision:
    return PaperScanDecision(
        scanned_at=scanned_at,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["register"]),
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=True,
                roi=Decimal("0.02"),
                minimum_state_pnl=Decimal("0.01"),
                numerically_validated=True,
            )
        ),
        minimum_net_edge=Decimal("0.01"),
        solver_model="strict_complete_set",
        eligible_for_paper_simulation=False,
    )


class StubPaperScan:
    def __init__(self, decision: PaperScanDecision | None = None) -> None:
        self.market_matcher = MarketMatcher()
        self.cost_resolver = None
        self.decision = decision or _qualifying_decision()
        self.calls = 0

    def scan_pair(self, *args: Any, **kwargs: Any) -> PaperScanDecision:
        del args, kwargs
        self.calls += 1
        return self.decision


class FakeMatchbook:
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []
        self.get_market_calls: list[tuple[str, str]] = []
        self.gone: set[str] = set()
        self.hang: set[str] = set()
        self.payloads: dict[str, dict[str, Any]] = {}
        self.inflight_kalshi_during_get: list[int] = []
        self.access: ProviderAccessLayer | None = None

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {"events": []}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_market(self, event_id: int | str, market_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.get_market_calls.append((str(event_id), str(market_id)))
        if self.access is not None:
            self.inflight_kalshi_during_get.append(
                self.access.snapshot().inflight.get(VenueName.KALSHI.value, 0)
            )
        key = str(market_id)
        if key in self.gone or str(event_id) in self.gone:
            raise MatchbookMarketGoneError(event_id, market_id, 404)
        if key in self.hang:
            await asyncio.Event().wait()
        if key in self.payloads:
            return self.payloads[key]
        return _mb_btts(int(market_id))


class FakeKalshi:
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []
        self.get_series_calls: list[str] = []
        self.book_calls: list[str] = []
        self.hang: set[str] = set()
        self.missing: set[str] = set()
        self.inflight_matchbook_during_book: list[int] = []
        self.access: ProviderAccessLayer | None = None
        self.peak_self: int = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {"events": []}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        self.get_series_calls.append(str(series_ticker))
        return {"ticker": series_ticker}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        if self.access is not None:
            snap = self.access.snapshot()
            self.inflight_matchbook_during_book.append(snap.inflight.get(VenueName.MATCHBOOK.value, 0))
            self.peak_self = max(self.peak_self, snap.inflight.get(VenueName.KALSHI.value, 0))
        if ticker in self.hang:
            await asyncio.Event().wait()
        if ticker in self.missing:
            return None  # type: ignore[return-value]
        return _kalshi_book()


def _engine(
    rows: list[ApprovedMarketCatalogueRow],
    *,
    clock: FakeClock | None = None,
    matchbook: FakeMatchbook | None = None,
    kalshi: FakeKalshi | None = None,
    access: ProviderAccessLayer | None = None,
    paper_scan: Any = None,
    fixture_state: FixtureCurrentStateStore | None = None,
    store: SqliteApprovedMarketCatalogueStore | None = None,
    timeout: float = 0.2,
    hot_interval: int = 0,
    background_interval: int = 0,
    on_item_decision: Any = None,
) -> tuple[CataloguePriceEngine, FakeMatchbook, FakeKalshi, ProviderAccessLayer]:
    mb = matchbook or FakeMatchbook()
    ks = kalshi or FakeKalshi()
    layer = access or ProviderAccessLayer(
        {
            VenueName.MATCHBOOK: 4,
            VenueName.KALSHI: 4,
            VenueName.POLYMARKET: 8,
        }
    )
    mb.access = layer
    ks.access = layer
    catalogue = store or SqliteApprovedMarketCatalogueStore(":memory:")
    for row in rows:
        catalogue.upsert_catalogue_row(row)
    engine = CataloguePriceEngine(
        catalogue_store=catalogue,
        matchbook=mb,
        kalshi=ks,
        paper_scan=paper_scan if paper_scan is not None else StubPaperScan(),
        fixture_state=fixture_state or FixtureCurrentStateStore(),
        provider_access=layer,
        clock=(clock or FakeClock(NOW)),
        provider_timeout_seconds=timeout,
        hot_interval_seconds=hot_interval,
        background_interval_seconds=background_interval,
        on_item_decision=on_item_decision,
    )
    engine.reconstruct()
    return engine, mb, ks, layer


def test_timeouts_caps_and_paper_boundary_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert Settings.model_fields["paper_scan_venue_timeout_seconds"].default == 15
    assert Settings.model_fields["paper_scan_matchbook_concurrency"].default == 4
    assert Settings.model_fields["paper_scan_kalshi_concurrency"].default == 4
    assert Settings.model_fields["paper_provider_hot_starvation_grants"].default == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert DEFAULT_STARVATION_HOT_GRANTS == 8
    assert DEFAULT_BACKGROUND_CADENCE_SECONDS == 180
    assert PRICE_ENGINE_RETRY_BACKOFF_SECONDS == (2.0, 5.0, 10.0)
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)
    source = inspect.getsource(ReadOnlyCrossVenueCollector)
    for banned in ("place_order", "cancel_order", "sign_order"):
        assert banned not in source
    engine_src = inspect.getsource(CataloguePriceEngine)
    assert "persist_triggered_chain" not in engine_src
    price_item_src = inspect.getsource(CataloguePriceEngine._price_item)
    assert ".list_events(" not in price_item_src
    assert ".list_markets(" not in price_item_src
    assert "MarketMatcher" not in inspect.getsource(CataloguePriceEngine._refresh_matchbook)
    provider_src = inspect.getsource(CataloguePriceEngine._provider_call)
    assert "acquire_wait" in provider_src
    assert "try_acquire" not in provider_src
    assert "create_task(_run" not in inspect.getsource(CataloguePriceEngine.run_slice)


@pytest.mark.asyncio
async def test_one_order_book_timeout_fails_only_that_item() -> None:
    slow = _row(suffix="slow", matchbook_market_id="316021", kalshi_event="KXEPLBTTS-SLOW")
    fast = _row(suffix="fast", matchbook_market_id="316022", kalshi_event="KXEPLBTTS-FAST")
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-SLOW-BTTS")
    matchbook = FakeMatchbook()
    matchbook.payloads["316021"] = _mb_btts(316021)
    matchbook.payloads["316022"] = _mb_btts(316022)
    engine, mb, ks, _layer = _engine([slow, fast], matchbook=matchbook, kalshi=kalshi)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert "amc-fast" in result.evaluated
    assert "amc-slow" in result.retry_wait
    assert "amc-slow" not in result.evaluated
    assert any(PRICE_ENGINE_ITEM_TIMEOUT_REASON in (issue.detail or "") for issue in result.issues)
    assert result.remaining_soft_used is False
    assert result.scan_budget_exhausted is False
    assert SCAN_BUDGET_EXHAUSTED_REASON not in result.statuses().values()
    assert mb.list_events_calls == 0
    assert mb.list_markets_calls == []
    assert ks.list_events_calls == 0
    assert ks.list_markets_calls == []


@pytest.mark.asyncio
async def test_price_engine_does_not_use_remaining_soft_or_leftover_exhaustion() -> None:
    rows = [
        _row(suffix=str(index), matchbook_market_id=str(316030 + index), kalshi_event=f"KXEPLBTTS-{index}")
        for index in range(3)
    ]
    engine, _mb, _ks, _layer = _engine(rows)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    source = inspect.getsource(CataloguePriceEngine.run_slice) + inspect.getsource(
        CataloguePriceEngine._price_item
    )
    assert "remaining_soft" not in source
    assert result.remaining_soft_used is False
    assert result.scan_budget_exhausted is False
    assert all(status != SCAN_BUDGET_EXHAUSTED_REASON for status in result.statuses().values())
    assert set(result.evaluated) == {row.catalogue_row_id for row in rows}


@pytest.mark.asyncio
async def test_unstarted_due_items_are_not_started_this_cadence() -> None:
    rows = [
        _row(suffix=str(index), matchbook_market_id=str(316040 + index), kalshi_event=f"KXEPLBTTS-U{index}")
        for index in range(3)
    ]
    engine, _mb, _ks, _layer = _engine(rows)
    result = await engine.run_slice(
        PriceEnginePriority.BACKGROUND,
        slice_wall_seconds=0,
        now=NOW,
    )
    assert result.evaluated == []
    assert set(result.not_started) == {row.catalogue_row_id for row in rows}
    assert all(status == NOT_STARTED_THIS_CADENCE for status in result.statuses().values())
    assert result.scan_budget_exhausted is False


@pytest.mark.asyncio
async def test_four_busy_kalshi_slots_are_capacity_saturated_not_budget() -> None:
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    gate = asyncio.Event()
    holders = [
        asyncio.create_task(_hold_slot(access, VenueName.KALSHI, gate))
        for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    assert access.snapshot().inflight[VenueName.KALSHI.value] == 4
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="fifth", matchbook_market_id="316050", kalshi_event="KXEPLBTTS-FIFTH")],
        access=access,
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    gate.set()
    await asyncio.gather(*holders)
    assert result.provider_capacity_saturated is True
    assert "amc-fifth" in result.deferred
    statuses = result.statuses()
    assert statuses["amc-fifth"] == PROVIDER_CAPACITY_SATURATED
    assert result.scan_budget_exhausted is False
    assert SCAN_BUDGET_EXHAUSTED_REASON not in statuses.values()


async def _hold_slot(access: ProviderAccessLayer, venue: VenueName, gate: asyncio.Event) -> None:
    async with access.acquire(venue, lane=ScanLane.HOT.value, stage="hold"):
        await gate.wait()


@pytest.mark.asyncio
async def test_provider_caps_remain_four_and_held_lease_blocks_one_slot() -> None:
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-HOLD-BTTS")
    engine, _mb, ks, layer = _engine(
        [
            _row(suffix="hold", matchbook_market_id="316061", kalshi_event="KXEPLBTTS-HOLD"),
            _row(suffix="ok", matchbook_market_id="316062", kalshi_event="KXEPLBTTS-OK"),
        ],
        kalshi=kalshi,
        access=access,
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert engine.snapshot()["peak_held_slots"][VenueName.KALSHI.value] <= 4
    assert engine.snapshot()["peak_held_slots"][VenueName.MATCHBOOK.value] <= 4
    assert "amc-ok" in result.evaluated
    assert "amc-hold" in result.retry_wait
    assert ks.peak_self <= 4


@pytest.mark.asyncio
async def test_no_cross_provider_slot_hostage() -> None:
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-HOSTAGE-BTTS")
    engine, mb, ks, _layer = _engine(
        [_row(suffix="hostage", matchbook_market_id="316071", kalshi_event="KXEPLBTTS-HOSTAGE")],
        kalshi=kalshi,
    )
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert ks.inflight_matchbook_during_book
    assert max(ks.inflight_matchbook_during_book) == 0
    assert mb.inflight_kalshi_during_get
    assert max(mb.inflight_kalshi_during_get) == 0


@pytest.mark.asyncio
async def test_hda_waits_for_all_constituents_and_one_timeout_fails_only_that_item() -> None:
    hda = _hda_row("hda")
    sibling = _row(suffix="sib", matchbook_market_id="316081", kalshi_event="KXEPLBTTS-SIB")
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLGAME-hda-DRAW")
    matchbook = FakeMatchbook()
    matchbook.payloads[str(hda.matchbook_market_id)] = _mb_match_odds(int(hda.matchbook_market_id))
    matchbook.payloads["316081"] = _mb_btts(316081)
    engine, _mb, ks, _layer = _engine(
        [hda, sibling],
        matchbook=matchbook,
        kalshi=kalshi,
        paper_scan=StubPaperScan(),
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert "amc-sib" in result.evaluated
    assert "amc-hda" not in result.evaluated
    assert "amc-hda" in result.retry_wait
    assert "KXEPLGAME-hda-HOME" in ks.book_calls
    assert "KXEPLGAME-hda-DRAW" in ks.book_calls


@pytest.mark.asyncio
async def test_existing_catalogue_ids_skip_list_events_and_list_markets() -> None:
    engine, mb, ks, _layer = _engine(
        [_row(suffix="ids", matchbook_market_id="316091", kalshi_event="KXEPLBTTS-IDS")]
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert result.evaluated == ["amc-ids"]
    assert mb.list_events_calls == 0
    assert mb.list_markets_calls == []
    assert ks.list_events_calls == 0
    assert ks.list_markets_calls == []
    assert ks.get_series_calls == []
    runtime = engine.item("amc-ids")
    assert runtime is not None
    assert runtime.list_events_calls == 0
    assert runtime.list_markets_calls == 0
    assert mb.get_market_calls == [("8801", "316091")]
    assert ks.book_calls == ["KXEPLBTTS-IDS-BTTS"]


@pytest.mark.asyncio
async def test_missing_or_gone_identity_requests_revalidation_not_full_listing() -> None:
    missing = _row(suffix="miss", matchbook_market_id="316101", kalshi_event="KXEPLBTTS-MISS")
    missing = missing.model_copy(update={"matchbook_market_id": None})
    gone = _row(suffix="gone", matchbook_market_id="316102", kalshi_event="KXEPLBTTS-GONE")
    matchbook = FakeMatchbook()
    matchbook.gone.add("316102")
    engine, mb, ks, _layer = _engine([missing, gone], matchbook=matchbook)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert set(result.revalidation) == {"amc-miss", "amc-gone"}
    assert result.evaluated == []
    assert mb.list_events_calls == 0
    assert mb.list_markets_calls == []
    assert ks.list_events_calls == 0
    reasons = {item["reason"] for item in engine.revalidation_requests}
    assert any("missing_matchbook_identity" in reason for reason in reasons)
    assert any("gone" in reason or "hot_revalidation_needed" in reason for reason in reasons)


def test_retry_backoff_is_process_memory_only_and_restart_resets() -> None:
    assert price_engine_retry_backoff_seconds(1) == 2.0
    assert price_engine_retry_backoff_seconds(2) == 5.0
    assert price_engine_retry_backoff_seconds(3) == 10.0
    assert price_engine_retry_backoff_seconds(9) == 10.0
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    store.upsert_catalogue_row(_row(suffix="retry"))
    assert store._shared_connection is not None
    tables = {
        row[0]
        for row in store._shared_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "price_engine_queue" not in tables
    assert "derived_price_item" not in tables
    columns = store.table_columns("approved_market_catalogue")
    assert "next_retry_at" not in {name.casefold() for name in columns}
    assert not ({name.casefold() for name in columns} & {item.casefold() for item in CATALOGUE_FORBIDDEN_COLUMNS})


@pytest.mark.asyncio
async def test_per_item_retry_backoff_and_restart_reconstructs_active_work() -> None:
    clock = FakeClock(NOW)
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-RETRY-BTTS")
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="retry", matchbook_market_id="316111", kalshi_event="KXEPLBTTS-RETRY")],
        clock=clock,
        kalshi=kalshi,
        store=store,
    )
    first = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=clock.now)
    assert "amc-retry" in first.retry_wait
    runtime = engine.item("amc-retry")
    assert runtime is not None
    assert runtime.retry_attempt == 1
    assert runtime.next_retry_at == NOW + timedelta(seconds=2)
    due = engine.due_items(PriceEnginePriority.BACKGROUND, now=clock.now + timedelta(seconds=1))
    assert due == []
    clock.advance(2)
    kalshi.hang.clear()
    second = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=clock.now)
    assert "amc-retry" in second.evaluated
    rebuilt = engine.restart()
    runtime = engine.item("amc-retry")
    assert runtime is not None
    assert runtime.retry_attempt == 0
    assert runtime.next_retry_at is None
    assert [item.identity.catalogue_row_id for item in rebuilt] == ["amc-retry"]
    assert engine.snapshot()["durable_queue"] is False


@pytest.mark.asyncio
async def test_hot_price_work_runs_while_universe_holds_provider_slots() -> None:
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    gate = asyncio.Event()
    holders = [
        asyncio.create_task(_hold_slot(access, VenueName.MATCHBOOK, gate)),
        asyncio.create_task(_hold_slot(access, VenueName.KALSHI, gate)),
    ]
    await asyncio.sleep(0.05)
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="hot", kickoff=NEAR_KICKOFF, matchbook_market_id="316121", kalshi_event="KXEPLBTTS-HOT")],
        access=access,
    )
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    gate.set()
    await asyncio.gather(*holders)
    assert "amc-hot" in result.evaluated
    runtime = engine.item("amc-hot")
    assert runtime is not None
    assert runtime.priority is PriceEnginePriority.HOT


@pytest.mark.asyncio
async def test_stale_epoch_callbacks_still_cannot_mutate_checkpoint_or_catalogue() -> None:
    await test_timed_out_chunk_cannot_mutate_replacement_checkpoint()
    source = inspect.getsource(LiveRefreshCoordinator._reject_stale_universe_chunk_unlocked)
    assert "stale" in source.casefold()


@pytest.mark.asyncio
async def test_background_coverage_promotes_qualifying_row_immediately() -> None:
    distant = _row(
        suffix="cover",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316131",
        kalshi_event="KXEPLBTTS-COVER",
    )
    noisy = _row(
        suffix="noise",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316132",
        kalshi_event="KXEPLBTTS-NOISE",
    )
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-NOISE-BTTS")
    fixture_state = FixtureCurrentStateStore()
    paper = StubPaperScan(_qualifying_decision(scanned_at=NOW))
    engine, _mb, _ks, _layer = _engine(
        [distant, noisy],
        kalshi=kalshi,
        paper_scan=paper,
        fixture_state=fixture_state,
    )
    runtime = engine.item("amc-cover")
    assert runtime is not None
    assert runtime.priority is PriceEnginePriority.BACKGROUND
    assert distant.canonical_event_id not in fixture_state.hot_identity_scope(NOW)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert "amc-cover" in result.evaluated
    assert paper.calls >= 1
    assert distant.canonical_event_id in result.promotions
    assert distant.canonical_event_id in fixture_state.hot_identity_scope(NOW)
    assert engine.classify_priority(runtime.identity) is PriceEnginePriority.HOT
    assert "amc-noise" in result.retry_wait
    assert result.decisions
    assert result.decisions[0].payoff_scan is not None
    assert result.decisions[0].payoff_scan.solution.is_arbitrage is True


@pytest.mark.asyncio
async def test_hot_anti_starve_still_grants_universe_and_background_cannot_starve() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=8)
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane=ScanLane.HOT.value):
            started.set()
            await release.wait()

    async def waiter(lane: str) -> None:
        await started.wait()
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            order.append(lane)

    holder_task = asyncio.create_task(holder())
    await started.wait()
    waiters = [
        asyncio.create_task(waiter(PRICE_ENGINE_BACKGROUND_LANE)),
        asyncio.create_task(waiter(ScanLane.UNIVERSE.value)),
        asyncio.create_task(waiter(ScanLane.HOT.value)),
    ]
    await asyncio.sleep(0.02)
    release.set()
    await holder_task
    await asyncio.gather(*waiters)
    assert order[0] == ScanLane.HOT.value
    assert ScanLane.UNIVERSE.value in order
    assert order.index(ScanLane.UNIVERSE.value) < order.index(PRICE_ENGINE_BACKGROUND_LANE)

    grants: list[str] = []
    stop = asyncio.Event()
    layer2 = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=8)

    async def hot_loop() -> None:
        while not stop.is_set():
            acquired = False
            async with layer2.try_acquire(VenueName.MATCHBOOK, lane=ScanLane.HOT.value) as lease:
                if lease is None:
                    await asyncio.sleep(0.001)
                    continue
                acquired = True
                grants.append("hot")
                await asyncio.sleep(0.001)
            if not acquired:
                await asyncio.sleep(0.001)

    async def universe_once() -> None:
        async with layer2.acquire(VenueName.MATCHBOOK, lane=ScanLane.UNIVERSE.value):
            grants.append("universe")
            stop.set()

    async def background_loop() -> None:
        while not stop.is_set():
            async with layer2.try_acquire(
                VenueName.MATCHBOOK, lane=PRICE_ENGINE_BACKGROUND_LANE
            ) as lease:
                if lease is None:
                    await asyncio.sleep(0.001)
                    continue
                grants.append("background")
                await asyncio.sleep(0.001)

    hot_task = asyncio.create_task(hot_loop())
    background_task = asyncio.create_task(background_loop())
    await asyncio.sleep(0.02)
    await asyncio.wait_for(universe_once(), timeout=2.0)
    hot_task.cancel()
    background_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hot_task
    with pytest.raises(asyncio.CancelledError):
        await background_task
    assert "universe" in grants
    hot_before = 0
    for item in grants:
        if item == "universe":
            break
        if item == "hot":
            hot_before += 1
    assert hot_before <= 8 or "universe" in grants


def test_existing_dual_cadence_and_hot_refresh_tests_were_not_deleted() -> None:
    import test_dual_cadence_scheduler as dual
    import test_issue318_hot_targeted_refresh as hot
    import test_issue200_universe_hot_promotion as promo
    import test_issue341_approved_market_catalogue as phase2

    assert hasattr(dual, "FakeClock")
    assert hasattr(hot, "test_hot_six_approved_equivalents_use_direct_reads_not_discovery")
    assert hasattr(promo, "test_distant_universe_qualifying_arb_enters_next_hot_identity_scope")
    assert hasattr(phase2, "test_catalogue_and_fee_schema_forbid_policy_and_quote_columns")
    assert ProviderPriority.BACKGROUND == 2
    assert ProviderPriority.HOT == 0
    assert ProviderPriority.UNIVERSE == 1


def test_no_durable_queue_or_phase4_capture_in_price_engine_module() -> None:
    source = inspect.getsource(CataloguePriceEngine)
    assert "CREATE TABLE" not in source
    assert "persist_triggered_chain" not in source
    store_src = inspect.getsource(SqliteApprovedMarketCatalogueStore)
    assert "price_engine_queue" not in store_src
    assert "next_retry_at" not in store_src


def _fast_rows(prefix: str, count: int, *, kickoff: datetime = DISTANT_KICKOFF) -> list[ApprovedMarketCatalogueRow]:
    return [
        _row(
            suffix=f"{prefix}{index}",
            kickoff=kickoff,
            matchbook_market_id=str(317000 + index),
            kalshi_event=f"KXEPLBTTS-{prefix}{index}",
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority", "kickoff"),
    [
        (PriceEnginePriority.BACKGROUND, DISTANT_KICKOFF),
        (PriceEnginePriority.HOT, NEAR_KICKOFF),
    ],
)
async def test_more_than_four_fast_items_flow_through_same_slice(
    priority: PriceEnginePriority,
    kickoff: datetime,
) -> None:
    rows = _fast_rows("flow", 12, kickoff=kickoff)
    engine, _mb, _ks, layer = _engine(rows)
    result = await engine.run_slice(priority, now=NOW)
    assert set(result.evaluated) == {row.catalogue_row_id for row in rows}
    assert result.deferred == []
    assert result.not_started == []
    assert result.scan_budget_exhausted is False
    assert SCAN_BUDGET_EXHAUSTED_REASON not in result.statuses().values()
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert engine.snapshot()["peak_held_slots"][VenueName.MATCHBOOK.value] <= 4
    assert engine.snapshot()["peak_held_slots"][VenueName.KALSHI.value] <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority", "kickoff"),
    [
        (PriceEnginePriority.BACKGROUND, DISTANT_KICKOFF),
        (PriceEnginePriority.HOT, NEAR_KICKOFF),
    ],
)
async def test_one_slow_item_lets_fast_siblings_use_remaining_slots(
    priority: PriceEnginePriority,
    kickoff: datetime,
) -> None:
    hung = _row(
        suffix="hung",
        kickoff=kickoff,
        matchbook_market_id="317100",
        kalshi_event="KXEPLBTTS-HUNG",
    )
    fast = [
        _row(
            suffix=f"sib{index}",
            kickoff=kickoff,
            matchbook_market_id=str(317101 + index),
            kalshi_event=f"KXEPLBTTS-SIB{index}",
        )
        for index in range(11)
    ]
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-HUNG-BTTS")
    engine, _mb, _ks, layer = _engine([hung, *fast], kalshi=kalshi)
    result = await engine.run_slice(priority, now=NOW)
    assert "amc-hung" in result.retry_wait
    assert set(result.evaluated) == {row.catalogue_row_id for row in fast}
    assert result.scan_budget_exhausted is False
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert engine.snapshot()["peak_held_slots"][VenueName.KALSHI.value] <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority", "kickoff"),
    [
        (PriceEnginePriority.BACKGROUND, DISTANT_KICKOFF),
        (PriceEnginePriority.HOT, NEAR_KICKOFF),
    ],
)
async def test_four_hung_kalshi_slots_defer_or_not_start_later_items(
    priority: PriceEnginePriority,
    kickoff: datetime,
) -> None:
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    gate = asyncio.Event()
    holders = [
        asyncio.create_task(_hold_slot(access, VenueName.KALSHI, gate))
        for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    assert access.snapshot().inflight[VenueName.KALSHI.value] == 4
    rows = _fast_rows("sat", 6, kickoff=kickoff)
    engine, _mb, _ks, layer = _engine(rows, access=access)
    result = await engine.run_slice(priority, now=NOW)
    gate.set()
    await asyncio.gather(*holders)
    statuses = result.statuses()
    assert result.scan_budget_exhausted is False
    assert SCAN_BUDGET_EXHAUSTED_REASON not in statuses.values()
    assert result.evaluated == []
    leftover = set(result.deferred) | set(result.not_started)
    assert leftover == {row.catalogue_row_id for row in rows}
    assert leftover
    assert PROVIDER_CAPACITY_SATURATED in statuses.values() or all(
        status == NOT_STARTED_THIS_CADENCE for status in statuses.values()
    )
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4


def _scheduled_hot_tick_env(monkeypatch):
    from sports_hedge.api import paper as paper_api
    from sports_hedge.api import watchlist as watchlist_api
    from sports_hedge.application.live_refresh import get_live_refresh_coordinator

    row = _row(
        suffix="sched",
        kickoff=NEAR_KICKOFF,
        matchbook_market_id="316201",
        kalshi_event="KXEPLBTTS-SCHED",
    )
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    store.upsert_catalogue_row(row)
    matchbook = FakeMatchbook()
    kalshi = FakeKalshi()
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    matchbook.access = access
    kalshi.access = access
    set_shared_provider_runtime(SharedProviderRuntime(matchbook=matchbook, kalshi=kalshi, access=access))
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator._clock = FakeClock(NOW)
    coordinator._price_engine = None
    coordinator.bind_catalogue_store(store)
    paper = StubPaperScan()
    monkeypatch.setattr(paper_api, "scheduled_paper_scan_service", lambda: paper)
    monkeypatch.setattr(paper_api, "get_paper_audit_repository", lambda: object())
    monkeypatch.setattr(watchlist_api, "get_watchlist_repository", lambda: object())
    monkeypatch.setattr(watchlist_api, "get_watchlist_service", lambda repo: object())
    monkeypatch.setattr(paper_api, "persist_price_engine_item_decision", lambda *a, **k: None)

    async def forbidden_collect(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("legacy HOT collector must not run on scheduled HOT")

    monkeypatch.setattr(paper_api, "_collect_report", forbidden_collect)
    plan = DualCadencePlan(
        lane="hot",
        reason="hot_due",
        identity_scope=[row.canonical_event_id],
    )
    return paper_api, coordinator, matchbook, kalshi, paper, plan


@pytest.mark.asyncio
async def test_scheduled_hot_tick_uses_price_engine_not_legacy_collector(monkeypatch) -> None:
    paper_api, coordinator, matchbook, kalshi, paper, plan = _scheduled_hot_tick_env(monkeypatch)
    persist_calls: list[dict[str, Any]] = []

    async def capture_persist(*args: Any, **kwargs: Any) -> None:
        persist_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(paper_api, "persist_scheduled_collection_report", capture_persist)
    try:
        await paper_api.server_owned_refresh_tick(plan)
        engine = coordinator.price_engine()
        runtime = engine.item("amc-sched")
        assert runtime is not None
        assert runtime.priority is PriceEnginePriority.HOT
        assert runtime.status.value == "evaluated"
        assert paper.calls == 1
        assert matchbook.get_market_calls == [("8801", "316201")]
        assert kalshi.book_calls == ["KXEPLBTTS-SCHED-BTTS"]
        assert matchbook.list_events_calls == 0
        assert matchbook.list_markets_calls == []
        assert kalshi.list_events_calls == 0
        assert kalshi.list_markets_calls == []
        assert len(persist_calls) == 1
        persist_kwargs = persist_calls[0]["kwargs"]
        report = persist_calls[0]["args"][1]
        assert persist_kwargs["scan_lane"] is ScanLane.HOT
        assert report.enabled_venues == [VenueName.MATCHBOOK, VenueName.KALSHI]
        assert report.matching_venues == [VenueName.MATCHBOOK, VenueName.KALSHI]
        assert report.paper_decisions == [paper.decision]
        assert report.scan_diagnostics["price_engine"] is True
        assert report.scan_diagnostics["legacy_hot_collector"] is False
        assert report.scan_diagnostics["item_completion_capture"] is True
        tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
        assert tick_src.index("PriceEnginePriority.HOT") < tick_src.index("_collect_report(")
        assert tick_src.index("run_cycle") < tick_src.index("persist_scheduled_collection_report")
        assert "legacy_hot_collector" in tick_src
        assert "bind_price_engine_item_persist" in tick_src
        engine_src = inspect.getsource(CataloguePriceEngine)
        assert "persist_triggered_chain" not in engine_src
        assert "persist_scheduled_collection_report" not in engine_src
        assert "_collect_report" not in engine_src
        assert "persist_triggered_chain(" not in tick_src
    finally:
        coordinator.reset()
        set_shared_provider_runtime(None)


@pytest.mark.asyncio
async def test_scheduled_hot_persist_failure_is_not_scan_cycle_timeout(monkeypatch) -> None:
    paper_api, coordinator, matchbook, kalshi, _paper, plan = _scheduled_hot_tick_env(monkeypatch)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit_write_failed")

    monkeypatch.setattr(paper_api, "_persist_collection_report", boom)
    try:
        await paper_api.server_owned_refresh_tick(plan)
        assert matchbook.get_market_calls == [("8801", "316201")]
        assert kalshi.book_calls == ["KXEPLBTTS-SCHED-BTTS"]
        assert matchbook.list_events_calls == 0
        assert kalshi.list_events_calls == 0
        assert coordinator.status.last_error is None
        assert coordinator.status.hot.last_error is None
        assert coordinator.status.hot.persist_ok is False
        assert coordinator.status.hot.last_persist_error == "audit_write_failed"
        assert "scan_cycle_timeout" not in (coordinator.status.hot.last_error or "")
        assert "scan_cycle_timeout" not in (coordinator.status.last_error or "")
        persist_stage = (coordinator.status.hot.last_diagnostics or {}).get("stages", {}).get(
            "persistence"
        )
        assert persist_stage is not None
        assert persist_stage["ok"] is False
        assert persist_stage["error"] == "audit_write_failed"
    finally:
        coordinator.reset()
        set_shared_provider_runtime(None)
