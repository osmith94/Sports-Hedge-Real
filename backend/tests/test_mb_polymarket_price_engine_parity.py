"""Matchbook ↔ Polymarket pricing without Kalshi.

Proves a registered MB+PM row is priced from exact persisted IDs, that a
disabled Kalshi venue is not fetched, and that Polymarket fee evidence reaches
the solver. Fixture/demo payloads only. No network. Execution stays disabled.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fx_test_helpers import fresh_usd_ecb_close
from test_nfl_stage1b_paper_markets import (
    NOW,
    _clone_mb_spread,
    _mb_indkc,
    _mb_market,
    _normalize_kalshi_game,
    _normalize_mb_family,
    _normalize_pm_family,
)

from sports_hedge.application.approved_market_catalogue import (
    FEE_STATUS_KNOWN,
    FEE_STATUS_UNKNOWN,
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    derived_price_engine_working_set,
)
from sports_hedge.application.catalogue_maintenance import (
    pair_identity_from_markets,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    EXECUTABLE_PRICE_NOT_REFRESHED,
    PM_FEE_SNAPSHOT_UNKNOWN,
    InventoryMarket,
    annotate_unpriced_registered_rows,
    apply_durable_polymarket_fee_evidence,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import (
    FABRICATED_POLYMARKET_CLOB_TOKEN,
    INSUFFICIENT_ENABLED_VENUES,
    MB_MARKET_PAYLOAD_MISSING,
    CataloguePriceEngine,
    PriceEnginePriority,
)
from sports_hedge.application.provider_access import (
    ProviderAccessLayer,
    reset_shared_provider_access,
)
from sports_hedge.config import Settings
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import FeeBasis
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_MATCH_RESULT_FT,
    registered_canonical_key,
)
from sports_hedge.nfl.constants import CANONICAL_NFL_GAME_WINNER, CANONICAL_NFL_POINT_SPREAD
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

KICKOFF = NOW + timedelta(hours=6)
HOME = "Brentford"
AWAY = "Chelsea"
TOKEN_HOME = "713856789012345678901234567890111"
TOKEN_DRAW = "713856789012345678901234567890222"
TOKEN_AWAY = "713856789012345678901234567890333"
PM_DISABLED = {"feesEnabled": False}
PM_ENABLED = {
    "feesEnabled": True,
    "feeSchedule": {"rate": "0.03", "exponent": 1, "takerOnly": True},
}
ENABLED = (VenueName.MATCHBOOK, VenueName.POLYMARKET)


@pytest.fixture(autouse=True)
def _reset_shared_provider() -> Any:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


def _back(odds: str) -> dict[str, str]:
    return {"side": "back", "odds": odds, "available-amount": "200"}


def _lay(odds: str) -> dict[str, str]:
    return {"side": "lay", "odds": odds, "available-amount": "200"}


def _priced(market: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(market)
    for runner in payload.get("runners") or []:
        runner["prices"] = [_back("2.20"), _lay("2.30")]
    return payload


def _football_match_odds() -> dict[str, Any]:
    return {
        "id": 41001,
        "name": "Match Odds",
        "status": "open",
        "market-type": "one_x_two",
        "runners": [
            {"id": 1, "name": HOME, "status": "open", "prices": [_back("2.40"), _lay("2.50")]},
            {"id": 2, "name": "Draw", "status": "open", "prices": [_back("3.40"), _lay("3.50")]},
            {"id": 3, "name": AWAY, "status": "open", "prices": [_back("2.90"), _lay("3.00")]},
        ],
    }


def _football_event(venue: VenueName, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition="Premier League",
        home_team=HOME,
        away_team=AWAY,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id,
    )


def _regulation() -> SettlementFingerprint:
    return SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        push_possible=False,
        penalties_included=False,
        extra_time_included=False,
    )


def _football_markets() -> tuple[CanonicalMarket, CanonicalMarket]:
    from sports_hedge.normalization.venues import MatchbookNormalizer

    event_payload = {
        "id": "8801",
        "name": f"{HOME} vs {AWAY}",
        "start": KICKOFF.isoformat(),
        "sport-name": "Football",
        "competition-name": "Premier League",
        "status": "open",
    }
    matchbook_event = MatchbookNormalizer().normalize_event(event_payload)
    matchbook = MatchbookNormalizer().normalize_market(matchbook_event, _football_match_odds())
    polymarket = CanonicalMarket(
        event=_football_event(VenueName.POLYMARKET, "pm-evt-brentford"),
        source_venue=VenueName.POLYMARKET,
        source_market_id="pm-mkt-1x2",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=None,
            penalties_included=None,
            push_possible=False,
        ),
        runners=[
            CanonicalRunner(source_runner_id=TOKEN_HOME, outcome=CanonicalOutcome.HOME, label=HOME),
            CanonicalRunner(source_runner_id=TOKEN_DRAW, outcome=CanonicalOutcome.DRAW, label="Draw"),
            CanonicalRunner(source_runner_id=TOKEN_AWAY, outcome=CanonicalOutcome.AWAY, label=AWAY),
        ],
    )
    return matchbook, polymarket


def _pm_books(tokens: list[str]) -> dict[str, dict[str, Any]]:
    books = {}
    for index, token in enumerate(tokens):
        ask = Decimal("0.40") + (Decimal(index) * Decimal("0.05"))
        books[token] = {
            "asset_id": token,
            "bids": [{"price": str(ask - Decimal("0.02")), "size": "200"}],
            "asks": [{"price": str(ask), "size": "200"}],
        }
    return books


class RecordingMatchbook:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self.payloads = payloads
        self.get_market_calls: list[tuple[str, str]] = []
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **_filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return {"events": []}

    async def list_markets(self, event_id: str, **_filters: Any) -> dict[str, Any]:
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_market(self, event_id: str, market_id: str, **_filters: Any) -> dict[str, Any]:
        self.get_market_calls.append((str(event_id), str(market_id)))
        payload = self.payloads.get(str(market_id))
        if payload is None:
            return None  # type: ignore[return-value]
        return payload


class RecordingPolymarket:
    def __init__(self, books: dict[str, dict[str, Any]]) -> None:
        self.books = books
        self.book_calls: list[tuple[str, str, str]] = []
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **_filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return {"events": []}

    async def list_markets(self, event_id: str, **_filters: Any) -> dict[str, Any]:
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_order_book(self, event_id: str, market_id: str, token_id: str) -> dict[str, Any]:
        self.book_calls.append((str(event_id), str(market_id), str(token_id)))
        return self.books[str(token_id)]


class RecordingKalshi:
    def __init__(self) -> None:
        self.book_calls: list[str] = []
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **_filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        return {"events": []}

    async def list_markets(self, event_ticker: str, **_filters: Any) -> dict[str, Any]:
        self.list_markets_calls.append(str(event_ticker))
        return {"markets": []}

    async def get_order_book(self, event_ticker: str, ticker: str) -> dict[str, Any]:
        self.book_calls.append(str(ticker))
        raise AssertionError("Kalshi must not be fetched for an MB+PM row")


def _scan_service() -> tuple[PaperScanService, SqliteMarketIntelligenceRepository]:
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes([fresh_usd_ecb_close(Decimal("0.75000000"), as_of=NOW)])
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(
        MarketIntelligenceService(repository),
        settings=Settings(
            min_net_edge=0,
            max_slippage_bps=0,
            fx_spread_bps=0,
            max_execution_risk=100,
        ),
        fx_service=fx,
        cost_resolver=VenueCostResolver(),
        clock=lambda: NOW,
    )
    return service, repository


def _persist(
    store: SqliteApprovedMarketCatalogueStore,
    pairs: list[Any],
    *,
    canonical_event_id: str,
    competition: str,
    home: str,
    away: str,
    kickoff: Any,
) -> None:
    persist_universe_catalogue_pass(
        store,
        canonical_event_id=canonical_event_id,
        competition=competition,
        home_canonical=home,
        away_canonical=away,
        kickoff_utc=kickoff,
        pairs=pairs,
        now=NOW,
        generation_id="g-mb-pm",
        family_discovery=None,
        terminal=False,
        allow_disappearance=False,
    )


async def _price(
    store: SqliteApprovedMarketCatalogueStore,
    *,
    canonical_event_id: str,
    matchbook_payloads: dict[str, dict[str, Any]],
    polymarket_books: dict[str, dict[str, Any]],
    enabled: tuple[VenueName, ...] = ENABLED,
    include_fx: bool = True,
) -> dict[str, Any]:
    if include_fx:
        service, repository = _scan_service()
    else:
        repository = SqliteMarketIntelligenceRepository()
        service = PaperScanService(
            MarketIntelligenceService(repository),
            settings=Settings(
                min_net_edge=0,
                max_slippage_bps=0,
                fx_spread_bps=0,
                max_execution_risk=100,
            ),
            fx_service=None,
            cost_resolver=VenueCostResolver(),
            clock=lambda: NOW,
        )
    matchbook = RecordingMatchbook(matchbook_payloads)
    polymarket = RecordingPolymarket(polymarket_books)
    kalshi = RecordingKalshi()
    state = FixtureCurrentStateStore()
    try:
        engine = CataloguePriceEngine(
            catalogue_store=store,
            matchbook=matchbook,
            kalshi=kalshi,
            polymarket=polymarket,
            paper_scan=service,
            fixture_state=state,
            provider_access=ProviderAccessLayer(
                {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
            ),
            clock=lambda: NOW,
            provider_timeout_seconds=2,
            hot_interval_seconds=0,
            background_interval_seconds=0,
        )
        engine.set_enabled_venues(enabled)
        result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        await engine.observability.drain()
        detail = state.detail(canonical_event_id, now=NOW)
        return {
            "engine": engine,
            "result": result,
            "detail": detail,
            "matchbook": matchbook,
            "polymarket": polymarket,
            "kalshi": kalshi,
            "working": derived_price_engine_working_set(store.list_active()),
        }
    finally:
        repository.close()


def _row_with_prices(detail: Any):
    assert detail is not None
    priced = [
        row
        for row in detail.markets
        if row.matchbook is not None
        and row.polymarket is not None
        and any(quote.decimal_odds is not None for quote in row.matchbook.best_backs)
        and any(quote.decimal_odds is not None for quote in row.polymarket.best_backs)
    ]
    assert priced, [
        (
            row.comparison_status,
            row.reason,
            row.rejection_reasons,
            None if row.matchbook is None else [quote.decimal_odds for quote in row.matchbook.best_backs],
            None if row.polymarket is None else [quote.decimal_odds for quote in row.polymarket.best_backs],
        )
        for row in detail.markets
    ]
    return priced[0]


def _assert_no_discovery_or_kalshi(priced: dict[str, Any]) -> None:
    assert priced["matchbook"].list_events_calls == 0
    assert priced["matchbook"].list_markets_calls == []
    assert priced["polymarket"].list_events_calls == 0
    assert priced["polymarket"].list_markets_calls == []
    assert priced["kalshi"].book_calls == []
    assert priced["kalshi"].list_events_calls == 0
    assert priced["kalshi"].list_markets_calls == []


def _polymarket_cost(decision: Any):
    costs = [
        cost
        for cost in decision.venue_costs
        if cost.venue is VenueName.POLYMARKET
    ]
    assert costs, decision.rejection_reasons
    return costs[0]


@pytest.mark.asyncio
async def test_football_match_result_prices_without_kalshi() -> None:
    matchbook, polymarket = _football_markets()
    assert registered_canonical_key(matchbook, polymarket) == CANONICAL_MATCH_RESULT_FT
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={
            "id": polymarket.source_market_id,
            **PM_DISABLED,
        },
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [identity],
            canonical_event_id="evt-brentford",
            competition="Premier League",
            home=HOME,
            away=AWAY,
            kickoff=KICKOFF,
        )
        row = store.list_active()[0]
        assert row.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        assert {item.native_id for item in row.polymarket_token_ids} == {
            TOKEN_HOME,
            TOKEN_DRAW,
            TOKEN_AWAY,
        }
        assert row.polymarket_fee_snapshot_id
        snapshot = store.get_polymarket_fee_snapshot(row.polymarket_fee_snapshot_id)
        assert snapshot is not None
        assert snapshot.fee_resolution_status == FEE_STATUS_KNOWN
        assert snapshot.fees_enabled is False
        priced = await _price(
            store,
            canonical_event_id="evt-brentford",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
        )
    finally:
        store.close()
    assert priced["working"]
    assert priced["working"][0].polymarket_fee_snapshot_id
    assert priced["result"].evaluated
    assert priced["matchbook"].get_market_calls == [("8801", "41001")]
    assert {call[2] for call in priced["polymarket"].book_calls} == {
        TOKEN_HOME,
        TOKEN_DRAW,
        TOKEN_AWAY,
    }
    _assert_no_discovery_or_kalshi(priced)
    assert priced["result"].decisions
    cost = _polymarket_cost(priced["result"].decisions[0])
    assert cost.is_economically_known()
    assert cost.fee_basis is FeeBasis.NONE_CONFIRMED
    market = _row_with_prices(priced["detail"])
    assert market.polymarket.fee_status == "known"
    assert market.polymarket.fee_label == "fee disabled · known zero"
    assert market.entered_solver or market.reason or market.rejection_reasons


@pytest.mark.asyncio
async def test_nfl_game_winner_prices_without_kalshi_even_if_kalshi_ids_exist() -> None:
    _, kalshi = _normalize_kalshi_game()
    _, polymarket, raw = _normalize_pm_family("moneyline")
    market_payload = _priced(_mb_market(_mb_indkc(), name="Moneyline"))
    _, matchbook = _normalize_mb_family(market_payload)
    mb_pm = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={**raw, **PM_DISABLED},
    )
    k_pm = pair_identity_from_markets(
        kalshi,
        polymarket,
        polymarket_market_payload={**raw, **PM_DISABLED},
        kalshi_event_payload={"event_ticker": kalshi.event.source_event_id},
        kalshi_series_payload={"ticker": "KXNFLGAME", "fee_type": "quadratic", "fee_multiplier": "1"},
    )
    assert mb_pm is not None and k_pm is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [mb_pm, k_pm],
            canonical_event_id="evt-indkc",
            competition="NFL",
            home=matchbook.event.home_team,
            away=matchbook.event.away_team,
            kickoff=matchbook.event.kickoff_utc,
        )
        row = store.list_active()[0]
        assert row.register_canonical_key == CANONICAL_NFL_GAME_WINNER
        assert row.kalshi_event_ticker
        assert row.polymarket_token_ids
        assert row.polymarket_fee_snapshot_id
        tokens = [item.native_id for item in row.polymarket_token_ids]
        priced = await _price(
            store,
            canonical_event_id="evt-indkc",
            matchbook_payloads={str(market_payload["id"]): market_payload},
            polymarket_books=_pm_books(tokens),
        )
    finally:
        store.close()
    assert priced["result"].evaluated
    assert priced["matchbook"].get_market_calls
    assert len(priced["polymarket"].book_calls) == len(tokens)
    _assert_no_discovery_or_kalshi(priced)
    market = _row_with_prices(priced["detail"])
    assert market.polymarket.fee_label == "fee disabled · known zero"
    assert priced["result"].decisions
    assert _polymarket_cost(priced["result"].decisions[0]).is_economically_known()


@pytest.mark.asyncio
async def test_nfl_exact_half_point_spread_prices_without_kalshi() -> None:
    payload = _priced(_clone_mb_spread(_mb_indkc(), home_line=Decimal("-6.5")))
    _, matchbook = _normalize_mb_family(payload)
    _, polymarket, raw = _normalize_pm_family("spreads")
    assert matchbook.line == Decimal("-6.5")
    assert polymarket.line == Decimal("-6.5")
    assert registered_canonical_key(matchbook, polymarket) == f"{CANONICAL_NFL_POINT_SPREAD}:-6.5"
    other = _clone_mb_spread(_mb_indkc(), home_line=Decimal("-3.5"))
    _, other_market = _normalize_mb_family(other)
    assert registered_canonical_key(other_market, polymarket) is None
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={**raw, **PM_ENABLED},
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [identity],
            canonical_event_id="evt-indkc-spread",
            competition="NFL",
            home=matchbook.event.home_team,
            away=matchbook.event.away_team,
            kickoff=matchbook.event.kickoff_utc,
        )
        row = store.list_active()[0]
        tokens = [item.native_id for item in row.polymarket_token_ids]
        snapshot = store.get_polymarket_fee_snapshot(str(row.polymarket_fee_snapshot_id))
        assert snapshot is not None and snapshot.fees_enabled is True
        assert snapshot.fee_resolution_status == FEE_STATUS_KNOWN
        priced = await _price(
            store,
            canonical_event_id="evt-indkc-spread",
            matchbook_payloads={str(payload["id"]): payload},
            polymarket_books=_pm_books(tokens),
        )
        mismatched = await _price(
            store,
            canonical_event_id="evt-indkc-spread",
            matchbook_payloads={str(payload["id"]): _priced(other)},
            polymarket_books=_pm_books(tokens),
        )
    finally:
        store.close()
    _assert_no_discovery_or_kalshi(priced)
    market = _row_with_prices(priced["detail"])
    assert market.line == Decimal("-6.5")
    assert "rate 0.03" in (market.polymarket.fee_label or "")
    cost = _polymarket_cost(priced["result"].decisions[0])
    assert cost.is_economically_known()
    assert cost.fee_basis is FeeBasis.FORMULA
    assert mismatched["result"].evaluated == []
    assert any("line_mismatch" in str(item.get("reason")) for item in mismatched["engine"].revalidation_requests)
    assert mismatched["polymarket"].book_calls == []


@pytest.mark.asyncio
async def test_missing_polymarket_fee_evidence_fails_closed() -> None:
    matchbook, polymarket = _football_markets()
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={"id": polymarket.source_market_id, "question": "Match Result"},
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [identity],
            canonical_event_id="evt-brentford",
            competition="Premier League",
            home=HOME,
            away=AWAY,
            kickoff=KICKOFF,
        )
        row = store.list_active()[0]
        snapshot = store.get_polymarket_fee_snapshot(str(row.polymarket_fee_snapshot_id))
        assert snapshot is not None
        assert snapshot.fee_resolution_status == FEE_STATUS_UNKNOWN
        assert snapshot.fees_enabled is None
        priced = await _price(
            store,
            canonical_event_id="evt-brentford",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
        )
    finally:
        store.close()
    _assert_no_discovery_or_kalshi(priced)
    assert priced["result"].decisions
    cost = _polymarket_cost(priced["result"].decisions[0])
    assert not cost.is_economically_known()
    assert cost.fee_basis is not FeeBasis.NONE_CONFIRMED
    assert any(
        "unknown_required_venue_cost:polymarket" in reason
        for decision in priced["result"].decisions
        for reason in decision.rejection_reasons
    )
    detail = priced["detail"]
    assert detail is not None
    polymarket_rows = [row for row in detail.markets if row.polymarket is not None]
    assert polymarket_rows
    assert polymarket_rows[0].polymarket.fee_status == "unknown"
    assert "fail closed" in (polymarket_rows[0].polymarket.fee_label or "")


@pytest.mark.asyncio
async def test_fabricated_polymarket_token_fails_closed_without_provider_calls() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        store.upsert_catalogue_row(
            ApprovedMarketCatalogueRow(
                catalogue_row_id="amc-fabricated",
                register_canonical_key=CANONICAL_MATCH_RESULT_FT,
                canonical_event_id="evt-fabricated",
                competition="Premier League",
                home_canonical=HOME,
                away_canonical=AWAY,
                kickoff_utc=KICKOFF,
                matchbook_event_id="8801",
                matchbook_market_id="41001",
                matchbook_runner_ids=[
                    OutcomeNativeId(outcome="home", native_id="1"),
                    OutcomeNativeId(outcome="draw", native_id="2"),
                    OutcomeNativeId(outcome="away", native_id="3"),
                ],
                polymarket_event_id="pm-evt-brentford",
                polymarket_market_id="pm-mkt-1x2",
                polymarket_token_ids=[
                    OutcomeNativeId(outcome="home", native_id="pm-mkt-1x2:0"),
                    OutcomeNativeId(outcome="draw", native_id="pm-mkt-1x2:1"),
                    OutcomeNativeId(outcome="away", native_id=TOKEN_AWAY),
                ],
                family="match_result",
                period="full_time",
                required_outcomes=["home", "draw", "away"],
                row_state=CatalogueRowState.ACTIVE,
                first_catalogued_at=NOW,
                last_confirmed_at=NOW,
            )
        )
        priced = await _price(
            store,
            canonical_event_id="evt-fabricated",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_AWAY]),
        )
    finally:
        store.close()
    assert priced["result"].evaluated == []
    assert any(
        FABRICATED_POLYMARKET_CLOB_TOKEN in str(item.get("reason"))
        for item in priced["engine"].revalidation_requests
    )
    assert priced["matchbook"].get_market_calls == []
    assert priced["polymarket"].book_calls == []
    _assert_no_discovery_or_kalshi(priced)


@pytest.mark.asyncio
async def test_kalshi_disabled_mb_only_row_does_not_wait_on_kalshi() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        store.upsert_catalogue_row(
            ApprovedMarketCatalogueRow(
                catalogue_row_id="amc-mb-only",
                register_canonical_key=CANONICAL_MATCH_RESULT_FT,
                canonical_event_id="evt-mb-only",
                competition="Premier League",
                home_canonical=HOME,
                away_canonical=AWAY,
                kickoff_utc=KICKOFF,
                matchbook_event_id="8801",
                matchbook_market_id="41001",
                kalshi_event_ticker="KXEPLGAME-DEMO",
                kalshi_market_tickers=["KXEPLGAME-DEMO-HOME"],
                family="match_result",
                period="full_time",
                required_outcomes=["home", "draw", "away"],
                row_state=CatalogueRowState.ACTIVE,
                first_catalogued_at=NOW,
                last_confirmed_at=NOW,
            )
        )
        priced = await _price(
            store,
            canonical_event_id="evt-mb-only",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books={},
        )
    finally:
        store.close()
    assert any(
        INSUFFICIENT_ENABLED_VENUES in str(item.get("reason"))
        for item in priced["engine"].revalidation_requests
    )
    assert priced["kalshi"].book_calls == []
    assert priced["matchbook"].get_market_calls == []


def test_missing_matchbook_payload_is_distinct_from_gone() -> None:
    assert MB_MARKET_PAYLOAD_MISSING != "catalogue_revalidation_needed:gone"


def test_durable_polymarket_fee_reaches_unpriced_inventory() -> None:
    matchbook, polymarket = _football_markets()
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={"id": polymarket.source_market_id, **PM_DISABLED},
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [identity],
            canonical_event_id="evt-brentford",
            competition="Premier League",
            home=HOME,
            away=AWAY,
            kickoff=KICKOFF,
        )
        item = InventoryMarket(
            venue=VenueName.POLYMARKET,
            source_event_id=polymarket.event.source_event_id,
            source_market_id=polymarket.source_market_id,
            raw_name="Match Result",
            canonical=polymarket,
        )
        apply_durable_polymarket_fee_evidence(
            [item],
            catalogue_store=store,
            canonical_event_id="evt-brentford",
        )
        assert item.durable_polymarket_fee is not None
        assert item.durable_polymarket_fee["fees_enabled"] is False
        from sports_hedge.application.fixture_inventory import assemble_fixture_inventory

        rows = assemble_fixture_inventory([], [item])
        assert rows[0].polymarket is not None
        assert rows[0].polymarket.fee_label == "fee disabled · known zero"
        rows[0].comparison_status = rows[0].comparison_status
        annotated = annotate_unpriced_registered_rows(rows)
        assert EXECUTABLE_PRICE_NOT_REFRESHED not in (annotated[0].rejection_reasons)
    finally:
        store.close()


def test_unpriced_equivalent_row_names_the_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    from sports_hedge.application.fixture_inventory import (
        FixtureMarketInventoryRow,
        InventoryComparisonStatus,
        VenueMarketFacts,
    )

    row = FixtureMarketInventoryRow(
        display_name="Match Result",
        family="match_result",
        comparison_status=InventoryComparisonStatus.MATCHED_EQUIVALENT,
        matchbook=VenueMarketFacts(
            venue=VenueName.MATCHBOOK,
            source_event_id="8801",
            source_market_id="41001",
        ),
        polymarket=VenueMarketFacts(
            venue=VenueName.POLYMARKET,
            source_event_id="pm-evt",
            source_market_id="pm-mkt",
            fee_status="missing",
        ),
    )
    annotated = annotate_unpriced_registered_rows([row])
    assert EXECUTABLE_PRICE_NOT_REFRESHED in annotated[0].pricing_diagnostics
    assert PM_FEE_SNAPSHOT_UNKNOWN in annotated[0].pricing_diagnostics
    assert EXECUTABLE_PRICE_NOT_REFRESHED not in annotated[0].rejection_reasons
    assert PM_FEE_SNAPSHOT_UNKNOWN not in annotated[0].rejection_reasons
    assert annotated[0].polymarket is not None
    assert annotated[0].polymarket.fee_status == "missing"
    assert annotated[0].comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
