"""OPEN PAPER trades must ACTIVE-refresh from persisted exact IDs.

The live failure was TOTAL_GOALS 5.5 and 7.5: HOT → Price-2 → both legs
filled, then every ACTIVE exact-ID refresh returned REVALIDATION_NEEDED.
Fixture/demo books only. PAPER. No live venue orders.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue316_catalogue_registry import REGULATION, _costs
from test_issue344_price_engine import FakeKalshi, FakeMatchbook, _engine

from sports_hedge.api import paper as paper_api
from sports_hedge.application.active_trade_lane import (
    get_active_trade_registry,
    identity_from_open_trade,
    reset_active_trade_registry,
)
from sports_hedge.application.approved_market_catalogue import (
    DerivedPriceEngineItem,
    OutcomeNativeId,
)
from sports_hedge.application.catalogue_maintenance import (
    pair_identity_from_markets,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.execution_reprice import ExecutionRepriceResult
from sports_hedge.application.live_refresh import (
    DualCadencePlan,
    LiveRefreshCoordinator,
    _active_revalidation_operator_copy,
    _active_revalidation_reason,
)
from sports_hedge.application.opportunity_viability import reset_opportunity_viability_cache
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import (
    PriceEngineItemStatus,
    PriceEnginePriority,
    kalshi_canonical_identity_gap,
)
from sports_hedge.application.provider_access import reset_shared_provider_access
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.catalogue.corpus import REGULATION as _REGULATION_CHECK
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.normalization.venues import KalshiNormalizer, MatchbookNormalizer
from sports_hedge.paper.active_trade_journal import ActiveTradeEventType, ActiveTradeReasonCode
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.trades import PaperActiveTradePhase, PaperTradeState
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

CLOCK_NOW = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
KICKOFF = CLOCK_NOW + timedelta(minutes=20)
HOME = "Brentford"
AWAY = "Chelsea"
assert _REGULATION_CHECK == REGULATION


class _RecordingScan(PaperScanService):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[PaperScanDecision] = []

    def scan_pair(self, left, right, **kwargs):
        decision = super().scan_pair(left, right, **kwargs)
        self.seen.append(decision)
        return decision


class _FreshMatchbook(FakeMatchbook):
    def __init__(self, market: dict[str, Any], clock: FakeClock) -> None:
        super().__init__()
        self.market = market
        self.clock = clock

    async def get_market(self, event_id: int | str, market_id: int | str, **filters: Any):
        self.payloads[str(market_id)] = _stamp(self.market, self.clock())
        return await super().get_market(event_id, market_id, **filters)


class _MutableKalshi(FakeKalshi):
    def __init__(self) -> None:
        super().__init__()
        self.book = _book("0.20", "0.70")

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ):
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        if ticker in self.missing:
            return None
        return self.book


def _stamp(market: dict[str, Any], at: datetime) -> dict[str, Any]:
    stamped = deepcopy(market)
    text = at.isoformat()
    for runner in stamped.get("runners") or []:
        runner["last-updated"] = text
        for price in runner.get("prices") or []:
            if isinstance(price, dict):
                price["last-updated"] = text
    return stamped


def _book(yes: str, no: str, size: str = "500.00") -> dict[str, Any]:
    return {"orderbook_fp": {"yes_dollars": [[yes, size]], "no_dollars": [[no, size]]}}


def _prices(odds: str, amount: str = "500") -> list[dict[str, str]]:
    lay = str(Decimal(odds) + Decimal("0.02"))
    return [
        {"side": "back", "odds": odds, "available-amount": amount},
        {"side": "lay", "odds": lay, "available-amount": amount},
    ]


def _runner(runner_id: int, name: str, odds: str) -> dict[str, Any]:
    return {"id": runner_id, "name": name, "status": "open", "prices": _prices(odds)}


def _spec(kind: str, line: str | None) -> dict[str, Any]:
    if kind == "btts":
        return {
            "kind": "btts",
            "line": None,
            "register_key": "BTTS_FT",
            "canonical_event_id": "evt-active-btts",
            "event_id": 880201,
            "market_id": 316201,
            "event_ticker": "KXEPLBTTS-ACTIVE",
            "ticker": "KXEPLBTTS-ACTIVE-BTTS",
            "series": "KXEPLBTTS",
        }
    assert line is not None
    slug = line.replace(".", "")
    return {
        "kind": "total",
        "line": line,
        "register_key": f"TOTAL_GOALS_FT:{line}",
        "canonical_event_id": f"evt-active-tg-{slug}",
        "event_id": 880000 + int(slug),
        "market_id": 316000 + int(slug),
        "event_ticker": f"KXEPLTOTAL-ACTIVE-{slug}",
        "ticker": f"KXEPLTOTAL-ACTIVE-{slug}-OU",
        "series": "KXEPLTOTAL",
    }


def _matchbook_market(spec: dict[str, Any]) -> dict[str, Any]:
    market_id = int(spec["market_id"])
    if spec["kind"] == "btts":
        return {
            "id": market_id,
            "name": "Both Teams To Score",
            "status": "open",
            "runners": [
                _runner(market_id * 10 + 1, "Yes", "2.20"),
                _runner(market_id * 10 + 2, "No", "1.80"),
            ],
        }
    line = spec["line"]
    return {
        "id": market_id,
        "name": f"Over/Under {line} Goals",
        "status": "open",
        "line": line,
        "runners": [
            _runner(market_id * 10 + 1, f"Over {line}", "2.20"),
            _runner(market_id * 10 + 2, f"Under {line}", "1.80"),
        ],
    }


def _kalshi_payloads(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    series = {
        "ticker": spec["series"],
        "title": "Premier League",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    if spec["kind"] == "btts":
        market = {
            "ticker": spec["ticker"],
            "event_ticker": spec["event_ticker"],
            "title": "Both Teams To Score",
            "yes_sub_title": "Yes",
            "rules_primary": REGULATION,
        }
    else:
        line = spec["line"]
        market = {
            "ticker": spec["ticker"],
            "event_ticker": spec["event_ticker"],
            "title": f"{HOME} vs {AWAY} Total Goals {line}",
            "yes_sub_title": f"Over {line}",
            "rules_primary": REGULATION,
            "strike": line,
        }
    event = {
        "event_ticker": spec["event_ticker"],
        "series_ticker": spec["series"],
        "title": f"{HOME} vs {AWAY}",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "milestone": {"start_date": KICKOFF.isoformat()},
        "product_metadata": {"competition": "Premier League", "competition_scope": "Game"},
        "markets": [market],
    }
    return event, market, series


def _seed(spec: dict[str, Any]) -> tuple[SqliteApprovedMarketCatalogueStore, dict[str, Any]]:
    event_payload, market_payload, series = _kalshi_payloads(spec)
    mb_event_payload = {
        "id": spec["event_id"],
        "name": f"{HOME} vs {AWAY}",
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
        "status": "open",
    }
    mb_market_payload = _matchbook_market(spec)
    mb_event = MatchbookNormalizer().normalize_event(mb_event_payload)
    ks_event = KalshiNormalizer().normalize_event(event_payload, series=series)
    mb_market = MatchbookNormalizer().normalize_market(mb_event, mb_market_payload)
    ks_market = KalshiNormalizer().normalize_market(ks_event, market_payload)
    key = registered_canonical_key(mb_market, ks_market)
    assert key == spec["register_key"], (
        key,
        mb_market.family,
        ks_market.family,
        mb_market.line,
        ks_market.line,
        mb_market.settlement,
        ks_market.settlement,
    )
    pair = pair_identity_from_markets(
        ks_market,
        mb_market,
        kalshi_event_payload=event_payload,
        kalshi_series_payload=series,
    )
    assert pair is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    rows = persist_universe_catalogue_pass(
        store,
        canonical_event_id=spec["canonical_event_id"],
        competition="Premier League",
        home_canonical=mb_event.home_team,
        away_canonical=mb_event.away_team,
        kickoff_utc=KICKOFF,
        pairs=[pair],
        now=CLOCK_NOW,
        generation_id=f"g-{spec['canonical_event_id']}",
        family_discovery=None,
        terminal=False,
        allow_disappearance=False,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.register_canonical_key == spec["register_key"]
    assert row.kickoff_utc == KICKOFF
    assert row.kalshi_fee_snapshot_id
    assert row.content_version >= 1
    if spec["kind"] == "total":
        assert str(row.line) == spec["line"]
    outcomes = {item.outcome for item in row.kalshi_outcome_ids}
    if spec["kind"] == "total":
        assert outcomes == {"over", "under"}
    else:
        assert outcomes == {"yes", "no"}
    assert all(":YES" in item.native_id or ":NO" in item.native_id for item in row.kalshi_outcome_ids)
    return store, mb_market_payload


def _bind_operator(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
    bind_runtime_operator_scanner_settings_store(store)
    store.save_settings(
        min_net_edge=Decimal("0"),
        max_execution_risk=100,
        hot_cadence_seconds=30,
        max_allocated_per_trade_gbp=Decimal("5000"),
        max_event_gbp=Decimal("5000"),
        max_opportunity_gbp=Decimal("5000"),
        max_one_time_gbp=Decimal("5000"),
    )


def _cleanup() -> None:
    bind_runtime_operator_scanner_settings_store(None)
    reset_active_trade_registry()
    reset_opportunity_viability_cache()
    reset_shared_provider_access()


async def _open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spec: dict[str, Any],
):
    reset_active_trade_registry()
    reset_opportunity_viability_cache()
    reset_shared_provider_access()
    _bind_operator(tmp_path)
    store, mb_market = _seed(spec)
    clock = FakeClock(CLOCK_NOW)
    settings = Settings(
        paper_autofill_enabled=True,
        min_net_edge=0,
        max_execution_risk=100,
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
    )
    assert settings.sports_hedge_execution_enabled is False
    repository = SqliteMarketIntelligenceRepository()
    scan = _RecordingScan(
        MarketIntelligenceService(repository),
        settings=settings,
        liquidity=SqlitePaperLiquidityRepository(
            tmp_path / "liquidity.sqlite",
            matchbook_gbp=Decimal("5000"),
            polymarket_usd=Decimal("5000"),
            kalshi_usd=Decimal("5000"),
        ),
        clock=clock,
    )
    matchbook = _FreshMatchbook(mb_market, clock)
    kalshi = _MutableKalshi()
    engine, _, _, _layer = _engine(
        store.list_active(),
        store=store,
        matchbook=matchbook,
        kalshi=kalshi,
        paper_scan=scan,
        clock=clock,
        timeout=2,
    )
    engine.venue_costs = _costs()
    engine.fx_snapshots = [
        FxRateSnapshot(
            currency="USD",
            gbp_per_unit=Decimal("0.75"),
            source="test_fx",
            captured_at=CLOCK_NOW,
        ),
        FxRateSnapshot(
            currency="GBP",
            gbp_per_unit=Decimal("1"),
            source="functional_currency",
            captured_at=CLOCK_NOW,
        ),
    ]
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=Decimal("5000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    watchlist = WatchlistService(
        SqliteWatchlistRepository(tmp_path / "watch.sqlite"),
        max_quote_age_ms=10_000,
    )
    operations = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )

    def operations_factory(watchlist_arg=None, alerts=None):
        del alerts
        if watchlist_arg is not None:
            operations.watchlist = watchlist_arg
        operations.settings = settings
        return operations

    monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
    monkeypatch.setattr(paper_api, "server_owned_paper_operations", lambda: operations)
    paper_api.bind_price_engine_item_persist(
        engine,
        service=scan,
        audit=SqlitePaperScanRepository(tmp_path / "audit.sqlite"),
        watchlist=watchlist,
    )
    result = await engine.run_slice(PriceEnginePriority.HOT, now=CLOCK_NOW)
    await engine.drain_item_captures()
    return {
        "spec": spec,
        "store": store,
        "engine": engine,
        "matchbook": matchbook,
        "kalshi": kalshi,
        "scan": scan,
        "operations": operations,
        "watchlist": watchlist,
        "ledger": ledger,
        "repository": repository,
        "clock": clock,
        "result": result,
        "settings": settings,
    }


def _close(bundle: dict[str, Any]) -> None:
    bundle["repository"].close()
    bundle["ledger"].close()
    bundle["store"].close()
    _cleanup()


def _trade(bundle: dict[str, Any]):
    trades = [
        trade
        for trade in bundle["operations"].list_active_trades()
        if trade.state is PaperTradeState.OPEN
    ]
    assert len(trades) == 1, (
        [trade.state for trade in bundle["operations"].list_active_trades()],
        [
            (
                decision.eligible_for_paper_simulation,
                decision.rejection_reasons,
                decision.canonical_market_id,
            )
            for decision in bundle["scan"].seen
        ],
    )
    return trades[0]


def _exposure(trade) -> tuple[Any, tuple[Any, ...]]:
    return (
        trade.capital_locked_gbp,
        tuple(
            (leg.venue, leg.outcome, leg.filled_stake, leg.source_runner_id)
            for leg in trade.legs
        ),
    )


async def _active_tick(bundle: dict[str, Any]) -> None:
    trade = _trade(bundle)
    coordinator = LiveRefreshCoordinator(
        clock=bundle["clock"],
        price_engine=bundle["engine"],
        catalogue_store=bundle["store"],
    )
    await coordinator._run_active_trade_tick(
        DualCadencePlan(lane="active_trade", identity_scope=[trade.trade_id])
    )


def _events(bundle: dict[str, Any], trade_id: str):
    return bundle["operations"].query_active_trade_events(trade_id=trade_id, limit=50)


def _refresh_results(bundle: dict[str, Any], trade_id: str):
    return [
        event
        for event in _events(bundle, trade_id)
        if event.event_type is ActiveTradeEventType.ACTIVE_REFRESH_RESULT
    ]


def _assert_native_ids(bundle: dict[str, Any], trade) -> None:
    spec = bundle["spec"]
    filled = [leg for leg in trade.legs if leg.filled_stake > 0]
    venues = {leg.venue for leg in filled}
    assert VenueName.MATCHBOOK in venues
    assert VenueName.KALSHI in venues
    matchbook = next(leg for leg in filled if leg.venue is VenueName.MATCHBOOK)
    kalshi = next(leg for leg in filled if leg.venue is VenueName.KALSHI)
    assert matchbook.source_event_id == str(spec["event_id"])
    assert matchbook.source_market_id == str(spec["market_id"])
    assert matchbook.source_runner_id
    assert kalshi.source_event_id == spec["event_ticker"]
    assert kalshi.source_market_id == spec["ticker"]
    runner = str(kalshi.source_runner_id or "")
    assert runner.endswith(":YES") or runner.endswith(":NO")
    assert runner.startswith(spec["ticker"])
    assert trade.canonical_event_id == spec["canonical_event_id"]
    assert trade.canonical_market_id
    assert trade.canonical_market_id.startswith("mkt:")
    assert trade.paper_only is True
    assert trade.places_orders is False
    assert trade.active_trade_phase is PaperActiveTradePhase.ACCUMULATING
    assert get_active_trade_registry().get(trade.trade_id) is not None
    if spec["kind"] == "total":
        assert trade.line == Decimal(spec["line"])


def _assert_reconstructed(bundle: dict[str, Any], trade) -> None:
    spec = bundle["spec"]
    rows = bundle["store"].list_rows_for_event(spec["canonical_event_id"])
    identity = identity_from_open_trade(trade, catalogue_rows=rows)
    assert identity is not None
    assert identity.catalogue_row_id == f"active-trade:{trade.trade_id}"
    assert identity.content_version == 0
    assert identity.register_canonical_key == spec["register_key"]
    assert identity.kickoff_utc == KICKOFF
    assert identity.matchbook_event_id == str(spec["event_id"])
    assert identity.matchbook_market_id == str(spec["market_id"])
    assert identity.kalshi_event_ticker == spec["event_ticker"]
    assert identity.kalshi_market_tickers == [spec["ticker"]]
    assert identity.kalshi_fee_snapshot_id
    if spec["kind"] == "total":
        assert identity.line == spec["line"]
        assert {item.outcome for item in identity.kalshi_outcome_ids} == {"over", "under"}
    else:
        assert {item.outcome for item in identity.kalshi_outcome_ids} == {"yes", "no"}
    assert kalshi_canonical_identity_gap(identity) is None
    bare = identity_from_open_trade(trade)
    assert bare is not None
    assert bare.kickoff_utc is None
    assert kalshi_canonical_identity_gap(bare) == "missing_kickoff_identity"


@pytest.mark.parametrize(
    ("kind", "line"),
    [("total", "5.5"), ("total", "7.5"), ("btts", None)],
)
async def test_hot_fill_then_active_exact_id_refresh_is_evaluated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    line: str | None,
) -> None:
    spec = _spec(kind, line)
    bundle = await _open(tmp_path, monkeypatch, spec)
    try:
        trade = _trade(bundle)
        _assert_native_ids(bundle, trade)
        _assert_reconstructed(bundle, trade)
        assert bundle["matchbook"].list_events_calls == 0
        assert bundle["kalshi"].list_events_calls == 0
        assert bundle["matchbook"].list_markets_calls == []
        assert bundle["kalshi"].list_markets_calls == []
        assert bundle["result"].decisions
        assert any(decision.eligible_for_paper_simulation for decision in bundle["scan"].seen)
        plan = bundle["operations"]._plans[trade.opportunity_id]
        assert plan.pricing_lane == "hot"

        calls = {"reprice": 0, "maybe": 0, "fill": 0}
        engine = bundle["engine"]
        operations = bundle["operations"]
        original_reprice = engine.reprice_for_paper_entry
        original_maybe = operations.maybe_top_up_open_trade
        original_fill = operations.fill_from_execution_snapshot

        async def _reprice(*args, **kwargs):
            calls["reprice"] += 1
            return await original_reprice(*args, **kwargs)

        def _maybe(*args, **kwargs):
            calls["maybe"] += 1
            return original_maybe(*args, **kwargs)

        def _fill(*args, **kwargs):
            calls["fill"] += 1
            return original_fill(*args, **kwargs)

        engine.reprice_for_paper_entry = _reprice
        operations.maybe_top_up_open_trade = _maybe
        operations.fill_from_execution_snapshot = _fill
        before_books = len(bundle["matchbook"].get_market_calls)
        before_kalshi = len(bundle["kalshi"].book_calls)
        await _active_tick(bundle)
        loaded = bundle["operations"].trades.get(trade.trade_id)
        assert loaded is not None
        results = _refresh_results(bundle, trade.trade_id)
        assert results
        latest = results[-1]
        assert latest.reason_code is ActiveTradeReasonCode.REFRESH_EVALUATED
        assert latest.payload["status"] == "evaluated"
        assert latest.payload["revalidation_reason"] is None
        assert latest.payload["trade_id"] == trade.trade_id
        assert latest.payload["canonical_market_id"] == trade.canonical_market_id
        assert latest.payload["register_canonical_key"] == spec["register_key"]
        assert str(latest.payload["catalogue_row_id"]).startswith("active-trade:")
        assert "revalidation" not in latest.operator_copy.casefold()
        assert len(bundle["matchbook"].get_market_calls) > before_books
        assert len(bundle["kalshi"].book_calls) > before_kalshi
        assert bundle["matchbook"].list_events_calls == 0
        assert bundle["kalshi"].list_events_calls == 0
        assert bundle["matchbook"].list_markets_calls == []
        assert bundle["kalshi"].list_markets_calls == []
        assert calls["reprice"] >= 1
        assert calls["maybe"] == 0
        if calls["fill"]:
            assert loaded.capital_locked_gbp >= trade.capital_locked_gbp
        else:
            assert _exposure(loaded) == _exposure(trade)
    finally:
        _close(bundle)


async def test_prices_moved_evaluates_without_topup_or_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = await _open(tmp_path, monkeypatch, _spec("total", "5.5"))
    try:
        trade = _trade(bundle)
        opening = _exposure(trade)
        bundle["kalshi"].book = _book("0.55", "0.55")
        market = bundle["matchbook"].market
        for runner, odds in zip(market["runners"], ("1.40", "1.40"), strict=True):
            runner["prices"] = _prices(odds)
        calls = {"maybe": 0, "fill": 0}
        operations = bundle["operations"]
        original_maybe = operations.maybe_top_up_open_trade
        original_fill = operations.fill_from_execution_snapshot

        def _maybe(*args, **kwargs):
            calls["maybe"] += 1
            return original_maybe(*args, **kwargs)

        def _fill(*args, **kwargs):
            calls["fill"] += 1
            return original_fill(*args, **kwargs)

        operations.maybe_top_up_open_trade = _maybe
        operations.fill_from_execution_snapshot = _fill
        await _active_tick(bundle)
        loaded = bundle["operations"].trades.get(trade.trade_id)
        latest = _refresh_results(bundle, trade.trade_id)[-1]
        assert latest.payload["status"] == "evaluated"
        assert latest.payload["revalidation_reason"] is None
        assert latest.reason_code is not ActiveTradeReasonCode.REFRESH_REVALIDATION_NEEDED
        assert calls["maybe"] == 0
        assert calls["fill"] == 0
        assert _exposure(loaded) == opening
        stale = [
            event
            for event in _events(bundle, trade.trade_id)
            if event.reason_code is ActiveTradeReasonCode.NO_ACTION_STALE_REFRESH
        ]
        assert stale == []
    finally:
        _close(bundle)


async def test_provider_unavailable_fail_closed_without_stale_topup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = await _open(tmp_path, monkeypatch, _spec("total", "7.5"))
    try:
        trade = _trade(bundle)
        opening = _exposure(trade)
        assert bundle["operations"]._plans.get(trade.opportunity_id) is not None
        bundle["kalshi"].missing.add(bundle["spec"]["ticker"])
        calls = {"maybe": 0, "fill": 0}
        operations = bundle["operations"]
        original_maybe = operations.maybe_top_up_open_trade
        original_fill = operations.fill_from_execution_snapshot

        def _maybe(*args, **kwargs):
            calls["maybe"] += 1
            return original_maybe(*args, **kwargs)

        def _fill(*args, **kwargs):
            calls["fill"] += 1
            return original_fill(*args, **kwargs)

        operations.maybe_top_up_open_trade = _maybe
        operations.fill_from_execution_snapshot = _fill
        await _active_tick(bundle)
        loaded = bundle["operations"].trades.get(trade.trade_id)
        latest = _refresh_results(bundle, trade.trade_id)[-1]
        assert latest.reason_code is ActiveTradeReasonCode.REFRESH_RETRY_WAIT
        assert latest.payload["status"] == "retry_wait"
        assert latest.payload["revalidation_reason"] is None
        assert latest.payload["trade_id"] == trade.trade_id
        assert "revalidation_reason=" not in latest.operator_copy
        stale = [
            event
            for event in _events(bundle, trade.trade_id)
            if event.reason_code is ActiveTradeReasonCode.NO_ACTION_STALE_REFRESH
        ]
        assert stale
        assert "stale prior plan" in stale[-1].operator_copy
        assert calls["maybe"] == 0
        assert calls["fill"] == 0
        assert _exposure(loaded) == opening
        assert bundle["matchbook"].list_events_calls == 0
        assert bundle["kalshi"].list_markets_calls == []
    finally:
        _close(bundle)


async def test_rejected_price2_cannot_top_up_from_the_opening_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = await _open(tmp_path, monkeypatch, _spec("total", "5.5"))
    try:
        trade = _trade(bundle)
        opening = _exposure(trade)
        calls = {"reprice": 0, "maybe": 0, "fill": 0}

        async def _reject(*_args, **_kwargs):
            calls["reprice"] += 1
            return ExecutionRepriceResult(reason="execution_reprice_no_longer_qualifying")

        def _maybe(*_args, **_kwargs):
            calls["maybe"] += 1
            raise AssertionError("stale opening plan must not top up")

        def _fill(*_args, **_kwargs):
            calls["fill"] += 1
            raise AssertionError("fill requires an accepted Price-2 snapshot")

        bundle["engine"].reprice_for_paper_entry = _reject
        bundle["operations"].maybe_top_up_open_trade = _maybe
        bundle["operations"].fill_from_execution_snapshot = _fill
        await _active_tick(bundle)
        loaded = bundle["operations"].trades.get(trade.trade_id)
        latest = _refresh_results(bundle, trade.trade_id)[-1]
        assert latest.payload["status"] == "evaluated"
        assert calls["reprice"] >= 1
        assert calls["maybe"] == 0
        assert calls["fill"] == 0
        assert _exposure(loaded) == opening
    finally:
        _close(bundle)


async def test_missing_catalogue_attachment_revalidates_after_books_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live predicate: books succeed, then kickoff is still absent."""

    bundle = await _open(tmp_path, monkeypatch, _spec("total", "5.5"))
    try:
        trade = _trade(bundle)
        opening = _exposure(trade)
        before_books = len(bundle["matchbook"].get_market_calls)
        before_kalshi = len(bundle["kalshi"].book_calls)

        def _no_rows(*_args, **_kwargs):
            return []

        monkeypatch.setattr(
            "sports_hedge.paper.provider_identity.catalogue_rows_for_trade",
            _no_rows,
        )
        await _active_tick(bundle)
        latest = _refresh_results(bundle, trade.trade_id)[-1]
        assert len(bundle["matchbook"].get_market_calls) > before_books
        assert len(bundle["kalshi"].book_calls) > before_kalshi
        assert latest.reason_code is ActiveTradeReasonCode.REFRESH_REVALIDATION_NEEDED
        assert latest.payload["status"] == "revalidation_needed"
        assert latest.payload["revalidation_reason"] == "missing_kickoff_identity"
        assert latest.payload["failure_predicate"] == "revalidation_reason=missing_kickoff_identity"
        assert latest.payload["stage"] == "catalogue_revalidation"
        assert latest.payload["venue"] == "matchbook_kalshi"
        assert latest.payload["trade_id"] == trade.trade_id
        assert latest.payload["canonical_market_id"] == trade.canonical_market_id
        assert "revalidation_reason=missing_kickoff_identity" in latest.operator_copy
        stale = [
            event
            for event in _events(bundle, trade.trade_id)
            if event.reason_code is ActiveTradeReasonCode.NO_ACTION_STALE_REFRESH
        ]
        assert stale
        loaded = bundle["operations"].trades.get(trade.trade_id)
        assert _exposure(loaded) == opening
        assert bundle["matchbook"].list_events_calls == 0
        assert bundle["kalshi"].list_events_calls == 0
    finally:
        _close(bundle)


async def test_corrupted_native_identity_stays_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = await _open(tmp_path, monkeypatch, _spec("btts", None))
    try:
        trade = _trade(bundle)
        opening = _exposure(trade)
        before_books = len(bundle["matchbook"].get_market_calls)
        corrupted_legs = []
        for leg in trade.legs:
            if leg.venue is VenueName.MATCHBOOK:
                corrupted_legs.append(
                    leg.model_copy(update={"source_event_id": trade.canonical_event_id})
                )
            else:
                corrupted_legs.append(leg)
        trade.legs = corrupted_legs
        bundle["operations"].trades.save(trade)
        assert identity_from_open_trade(trade, catalogue_rows=bundle["store"].list_active()) is None
        await _active_tick(bundle)
        latest = _refresh_results(bundle, trade.trade_id)[-1]
        assert latest.reason_code is ActiveTradeReasonCode.REFRESH_MISSING_IDENTITY
        assert latest.payload["status"] is None
        assert len(bundle["matchbook"].get_market_calls) == before_books
        loaded = bundle["operations"].trades.get(trade.trade_id)
        assert _exposure(loaded) == opening
        stale = [
            event
            for event in _events(bundle, trade.trade_id)
            if event.reason_code is ActiveTradeReasonCode.NO_ACTION_STALE_REFRESH
        ]
        assert stale
    finally:
        _close(bundle)


def test_identity_gap_predicates_are_specific_and_ignore_content_version() -> None:
    kickoff = KICKOFF
    complete = DerivedPriceEngineItem(
        catalogue_row_id="active-trade:demo",
        content_version=0,
        canonical_event_id="evt-demo",
        register_canonical_key="TOTAL_GOALS_FT:5.5",
        kalshi_event_ticker="KXEPLTOTAL-ACTIVE-55",
        kalshi_market_tickers=["KXEPLTOTAL-ACTIVE-55-OU"],
        kalshi_outcome_ids=[
            OutcomeNativeId(outcome="over", native_id="KXEPLTOTAL-ACTIVE-55-OU:YES"),
            OutcomeNativeId(outcome="under", native_id="KXEPLTOTAL-ACTIVE-55-OU:NO"),
        ],
        line="5.5",
        required_outcomes=["over", "under"],
        kickoff_utc=kickoff,
    )
    assert kalshi_canonical_identity_gap(complete) is None
    assert (
        kalshi_canonical_identity_gap(complete.model_copy(update={"kalshi_event_ticker": None}))
        == "missing_kalshi_identity"
    )
    assert (
        kalshi_canonical_identity_gap(complete.model_copy(update={"kickoff_utc": None}))
        == "missing_kickoff_identity"
    )
    assert (
        kalshi_canonical_identity_gap(
            complete.model_copy(update={"register_canonical_key": "mkt:hashed", "family": None})
        )
        == "missing_register_key"
    )
    assert (
        kalshi_canonical_identity_gap(complete.model_copy(update={"line": None}))
        == "missing_line_identity"
    )
    assert (
        kalshi_canonical_identity_gap(complete.model_copy(update={"kalshi_outcome_ids": []}))
        == "missing_kalshi_outcome_identity"
    )
    btts = complete.model_copy(
        update={
            "register_canonical_key": "BTTS_FT",
            "line": None,
            "required_outcomes": ["yes", "no"],
            "kalshi_outcome_ids": [
                OutcomeNativeId(outcome="yes", native_id="T:YES"),
                OutcomeNativeId(outcome="no", native_id="T:NO"),
            ],
        }
    )
    assert kalshi_canonical_identity_gap(btts) is None


def test_revalidation_journal_keeps_the_machine_readable_reason() -> None:
    assert _active_revalidation_reason("revalidation_reason=missing_kickoff_identity") == (
        "missing_kickoff_identity"
    )
    assert _active_revalidation_reason("revalidation_reason=missing_line_identity extra") == (
        "missing_line_identity"
    )
    assert _active_revalidation_reason("hot_revalidation_needed:gone") == "market_gone"
    runtime = type("Runtime", (), {"last_error_detail": "revalidation_reason=missing_register_key"})()
    copy = _active_revalidation_operator_copy(runtime)
    assert copy == (
        "ACTIVE refresh needs catalogue revalidation revalidation_reason=missing_register_key"
    )
    assert "price_engine" not in copy
    assert PriceEngineItemStatus.REVALIDATION_NEEDED.value == "revalidation_needed"
