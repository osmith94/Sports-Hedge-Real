"""Registered MB↔PM rows consume one settlement, fee, and FX authority.

Fixture/demo payloads only. No network. Execution stays disabled.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sports_hedge.application.approved_market_catalogue import (
    FEE_STATUS_KNOWN,
    CatalogueRowState,
    derived_price_engine_working_set,
)
from sports_hedge.application.catalogue_maintenance import pair_identity_from_markets
from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.config import Settings
from sports_hedge.domain.football import (
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
from sports_hedge.fees.polymarket import apply_polymarket_taker
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.nfl.constants import CANONICAL_NFL_GAME_WINNER
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from test_mb_polymarket_price_engine_parity import (
    AWAY,
    HOME,
    KICKOFF,
    NOW,
    TOKEN_AWAY,
    TOKEN_DRAW,
    TOKEN_HOME,
    RecordingKalshi,
    RecordingMatchbook,
    RecordingPolymarket,
    _assert_no_discovery_or_kalshi,
    _football_event,
    _football_markets,
    _football_match_odds,
    _persist,
    _pm_books,
    _polymarket_cost,
    _price,
    _priced,
    _row_with_prices,
    _scan_service,
)
from test_nfl_stage1b_paper_markets import (
    _mb_indkc,
    _mb_market,
    _normalize_mb_family,
    _normalize_pm_family,
)

TOKEN_OVER = "713856789012345678901234567890444"
TOKEN_UNDER = "713856789012345678901234567890555"
PM_FORMULA = {
    "feesEnabled": True,
    "feeSchedule": {"rate": "0.05", "exponent": 1, "takerOnly": True},
}
SETTLEMENT_VETOES = (
    "incomplete_settlement",
    "unknown_settlement_scope",
    "settlement_mismatch",
)


def _grouped_formula_payload(market_id: str) -> dict:
    child = {"feesEnabled": True, **{"feeSchedule": PM_FORMULA["feeSchedule"]}}
    return {
        "id": market_id,
        "question": "Match result",
        "sportsMarketType": "moneyline",
        "assembled_match_result": True,
        "grouped_payloads": [
            {"id": "pm-home", **child},
            {"id": "pm-draw", **child},
            {"id": "pm-away", **child},
        ],
    }


def _assert_solver_economics(priced: dict) -> None:
    assert priced["result"].decisions
    decision = priced["result"].decisions[0]
    reasons = list(decision.rejection_reasons)
    for token in (*SETTLEMENT_VETOES, "missing_fx_rate", "unknown_required_venue_cost:polymarket"):
        assert token not in " ".join(reasons), reasons
    assert decision.depth_scan is not None or decision.payoff_scan is not None
    assert decision_net_edge(decision) is not None
    cost = _polymarket_cost(decision)
    assert cost.is_economically_known()
    assert cost.fee_basis is FeeBasis.FORMULA
    fee, _net = apply_polymarket_taker(
        cost, gross_decimal_odds=Decimal("2"), stake=Decimal("50")
    )
    assert fee == Decimal("1.25")
    assert fee != Decimal("50") * Decimal("0.05")
    market = _row_with_prices(priced["detail"])
    assert market.current_net_edge is not None
    assert market.entered_solver is True
    assert market.polymarket.fee_status == "known"
    assert market.polymarket.fee_basis == FeeBasis.FORMULA.value
    assert market.polymarket.fx_status == "known"
    assert market.polymarket.settlement_status == "paper_assumed"
    assert market.polymarket.settlement_complete is False
    assert Settings().sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_registered_football_grouped_fee_and_fx_produce_economics() -> None:
    matchbook, polymarket = _football_markets()
    assert not polymarket.settlement.is_economically_complete()
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload=_grouped_formula_payload(polymarket.source_market_id),
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
        assert snapshot.fee_resolution_status == FEE_STATUS_KNOWN
        assert snapshot.fees_enabled is True
        assert snapshot.fee_schedule is not None
        assert snapshot.fee_schedule["rate"] == "0.05"
        priced = await _price(
            store,
            canonical_event_id="evt-brentford",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
        )
    finally:
        store.close()
    _assert_no_discovery_or_kalshi(priced)
    _assert_solver_economics(priced)
    assert any(item.currency == "USD" for item in priced["result"].decisions[0].fx_snapshots)


def _football_total_markets() -> tuple[CanonicalMarket, CanonicalMarket, dict]:
    from sports_hedge.normalization.venues import MatchbookNormalizer

    event_payload = {
        "id": "8801",
        "name": f"{HOME} vs {AWAY}",
        "start": KICKOFF.isoformat(),
        "sport-name": "Football",
        "competition-name": "Premier League",
        "status": "open",
    }
    payload = {
        "id": 6102,
        "name": "Total Goals 2.5",
        "status": "open",
        "runners": [
            {"id": 11, "name": "Over 2.5", "status": "open"},
            {"id": 12, "name": "Under 2.5", "status": "open"},
        ],
    }
    event = MatchbookNormalizer().normalize_event(event_payload)
    matchbook = MatchbookNormalizer().normalize_market(event, payload)
    polymarket = CanonicalMarket(
        event=_football_event(VenueName.POLYMARKET, "pm-evt-brentford"),
        source_venue=VenueName.POLYMARKET,
        source_market_id="pm-mkt-total-25",
        family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        line=Decimal("2.5"),
        settlement=SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.FULL_TIME,
            line=Decimal("2.5"),
            extra_time_included=None,
            penalties_included=None,
            push_possible=False,
        ),
        runners=[
            CanonicalRunner(source_runner_id=TOKEN_OVER, outcome=CanonicalOutcome.OVER, label="Over 2.5"),
            CanonicalRunner(source_runner_id=TOKEN_UNDER, outcome=CanonicalOutcome.UNDER, label="Under 2.5"),
        ],
    )
    return matchbook, polymarket, payload


@pytest.mark.asyncio
async def test_registered_total_goals_row_produces_economics() -> None:
    matchbook, polymarket, payload = _football_total_markets()
    assert registered_canonical_key(matchbook, polymarket) is not None
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={"id": polymarket.source_market_id, **PM_FORMULA},
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [identity],
            canonical_event_id="evt-brentford-total",
            competition="Premier League",
            home=HOME,
            away=AWAY,
            kickoff=KICKOFF,
        )
        priced_payload = {
            **payload,
            "runners": [
                {
                    **runner,
                    "prices": [
                        {"side": "back", "odds": "1.90", "available-amount": "80"},
                        {"side": "lay", "odds": "2.00", "available-amount": "80"},
                    ],
                }
                for runner in payload["runners"]
            ],
        }
        priced = await _price(
            store,
            canonical_event_id="evt-brentford-total",
            matchbook_payloads={"6102": priced_payload},
            polymarket_books=_pm_books([TOKEN_OVER, TOKEN_UNDER]),
        )
    finally:
        store.close()
    _assert_solver_economics(priced)


@pytest.mark.asyncio
async def test_registered_nfl_winner_formula_fee_produces_economics() -> None:
    _, polymarket, raw = _normalize_pm_family("moneyline")
    market_payload = _priced(_mb_market(_mb_indkc(), name="Moneyline"))
    _, matchbook = _normalize_mb_family(market_payload)
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={**raw, **PM_FORMULA},
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            store,
            [identity],
            canonical_event_id="evt-indkc",
            competition="NFL",
            home=matchbook.event.home_team,
            away=matchbook.event.away_team,
            kickoff=matchbook.event.kickoff_utc,
        )
        row = store.list_active()[0]
        assert row.register_canonical_key == CANONICAL_NFL_GAME_WINNER
        tokens = [item.native_id for item in row.polymarket_token_ids]
        priced = await _price(
            store,
            canonical_event_id="evt-indkc",
            matchbook_payloads={str(market_payload["id"]): market_payload},
            polymarket_books=_pm_books(tokens),
        )
    finally:
        store.close()
    _assert_no_discovery_or_kalshi(priced)
    decision = priced["result"].decisions[0]
    for token in (*SETTLEMENT_VETOES, "missing_fx_rate", "unknown_required_venue_cost:polymarket"):
        assert token not in " ".join(decision.rejection_reasons)
    assert decision_net_edge(decision) is not None
    cost = _polymarket_cost(decision)
    assert cost.fee_basis is FeeBasis.FORMULA
    market = _row_with_prices(priced["detail"])
    assert market.current_net_edge is not None
    assert market.polymarket.fx_status == "known"
    assert market.polymarket.settlement_status == "paper_assumed"
    assert market.comparison_status.value == "paper_assumed_equivalent"


@pytest.mark.asyncio
async def test_missing_bound_fx_still_fails_closed() -> None:
    matchbook, polymarket = _football_markets()
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={"id": polymarket.source_market_id, **PM_FORMULA},
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
        priced = await _price(
            store,
            canonical_event_id="evt-brentford",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
            include_fx=False,
        )
    finally:
        store.close()
    decision = priced["result"].decisions[0]
    assert any(reason.startswith("missing_fx") for reason in decision.rejection_reasons)
    assert decision_net_edge(decision) is None
    market = _row_with_prices(priced["detail"])
    assert market.current_net_edge is None
    assert market.polymarket.fx_status == "missing"


@pytest.mark.asyncio
async def test_inventory_uses_scan_time_fx_after_engine_context_clears() -> None:
    matchbook_market, polymarket = _football_markets()
    identity = pair_identity_from_markets(
        matchbook_market,
        polymarket,
        polymarket_market_payload={"id": polymarket.source_market_id, **PM_FORMULA},
    )
    assert identity is not None
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    service, repository = _scan_service()
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
        matchbook = RecordingMatchbook({"41001": _football_match_odds()})
        polymarket_client = RecordingPolymarket(_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]))
        state = FixtureCurrentStateStore()
        engine = CataloguePriceEngine(
            catalogue_store=store,
            matchbook=matchbook,
            kalshi=RecordingKalshi(),
            polymarket=polymarket_client,
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
        engine.set_enabled_venues((VenueName.MATCHBOOK, VenueName.POLYMARKET))
        queued: list = []
        engine.observability.emit = lambda fn: queued.append(fn)  # type: ignore[method-assign]
        await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        assert queued
        assert any(item.currency == "USD" for item in engine.fx_snapshots)
        engine.fx_snapshots = []
        engine._scanner_fx_bound = True
        engine._fx_auto_resolved = False
        for callback in queued:
            callback()
        detail = state.detail("evt-brentford", now=NOW)
    finally:
        store.close()
        repository.close()
    market = _row_with_prices(detail)
    assert market.polymarket.fx_status == "known"
    assert market.current_net_edge is not None


def test_extra_time_pair_is_not_registered_and_is_rejected() -> None:
    matchbook, polymarket = _football_markets()
    blocked = polymarket.model_copy(
        update={
            "settlement": polymarket.settlement.model_copy(
                update={
                    "scope": SettlementScope.INCLUDING_EXTRA_TIME,
                    "extra_time_included": True,
                    "penalties_included": False,
                }
            )
        }
    )
    assert registered_canonical_key(matchbook, blocked) is None
    service, repository = _scan_service()
    try:
        matchbook_obs = MatchbookObservationBuilder().build_from_canonical(
            matchbook,
            _football_match_odds(),
            observed_at=NOW,
            quote_age_ms=0,
        )
        polymarket_obs = PolymarketObservationBuilder().build(
            {"id": "pm-evt-brentford"},
            {"id": blocked.source_market_id, **PM_FORMULA},
            _pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
            canonical=blocked,
            observed_at=NOW,
            quote_age_ms=0,
        )
        decision = service.scan_pair(
            matchbook_obs,
            polymarket_obs,
            maximum_execution_risk=Decimal("100"),
        )
    finally:
        repository.close()
    assert decision.market_match.matched is False
    assert decision.depth_scan is None
    assert decision.payoff_scan is None
    assert decision_net_edge(decision) is None
    assert decision.eligible_for_paper_simulation is False


@pytest.mark.asyncio
async def test_invalidated_catalogue_row_is_not_priced() -> None:
    matchbook, polymarket = _football_markets()
    identity = pair_identity_from_markets(
        matchbook,
        polymarket,
        polymarket_market_payload={"id": polymarket.source_market_id, **PM_FORMULA},
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
        store.upsert_catalogue_row(row.model_copy(update={"row_state": CatalogueRowState.INVALIDATED}))
        assert store.list_active() == []
        assert derived_price_engine_working_set(store.list_active()) == []
        priced = await _price(
            store,
            canonical_event_id="evt-brentford",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
        )
    finally:
        store.close()
    assert priced["result"].evaluated == []
    assert priced["result"].decisions == []
    assert priced["matchbook"].get_market_calls == []
