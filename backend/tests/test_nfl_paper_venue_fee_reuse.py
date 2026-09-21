"""NFL PAPER economics reuse existing Kalshi/Polymarket fee snapshots.

BACKGROUND/HOT reconstruct from catalogue identity. A Gamma wrapper with only
id/question must not erase previously captured venue fee truth. No NFL-specific
fee table. Unknown/ambiguous metadata still fails closed.

PAPER / read-only. Fixture/demo payloads. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fx_test_helpers import fresh_usd_ecb_close
from test_nfl_stage1b_paper_markets import (
    NOW,
    _kalshi_game_event,
    _kalshi_spread_event_and_market,
    _kalshi_total_event_and_market,
    _normalize_kalshi_game,
    _normalize_kalshi_spread,
    _normalize_kalshi_total,
    _normalize_pm_family,
)
from test_paper_scan_pipeline import OBSERVED

from sports_hedge.application.approved_market_catalogue import (
    FEE_STATUS_KNOWN,
    FEE_STATUS_UNKNOWN,
    derived_price_engine_working_set,
    polymarket_fee_snapshot_from_payload,
)
from sports_hedge.application.catalogue_maintenance import (
    pair_identity_from_markets,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.hot_market_relationships import HotVenueLeg
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import CataloguePriceEngine, RetrievedVenuePayload
from sports_hedge.application.provider_access import reset_shared_provider_access
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis
from sports_hedge.fees.polymarket import (
    POLYMARKET_TAKER_FORMULA,
    market_payload_has_fee_evidence,
    polymarket_cost_from_market,
    resolve_polymarket_fee_metadata,
)
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.nfl.constants import (
    CANONICAL_NFL_GAME_WINNER,
    CANONICAL_NFL_POINT_SPREAD,
    CANONICAL_NFL_TOTAL_POINTS,
)
from sports_hedge.paper.audit import build_paper_scan_record
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

KICKOFF = NOW + timedelta(hours=3)
KALSHI_SERIES_FEES = {
    "ticker": "KXNFLGAME",
    "fee_type": "quadratic_with_maker_fees",
    "fee_multiplier": "1",
}
PM_DISABLED = {"feesEnabled": False}
PM_ENABLED = {
    "feesEnabled": True,
    "feeSchedule": {"rate": "0.03", "exponent": 1, "takerOnly": True},
}


@pytest.fixture(autouse=True)
def _reset_shared_provider() -> Any:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


def _identity_stub(market_id: str) -> dict[str, Any]:
    return {"id": market_id, "question": "game winner", "conditionId": "0xabc"}


def _pm_books(tokens: list[str]) -> dict[str, dict[str, Any]]:
    return {
        tokens[0]: {
            "asset_id": tokens[0],
            "bids": [{"price": "0.54", "size": "200"}],
            "asks": [{"price": "0.56", "size": "200"}],
        },
        tokens[1]: {
            "asset_id": tokens[1],
            "bids": [{"price": "0.42", "size": "200"}],
            "asks": [{"price": "0.44", "size": "200"}],
        },
    }


def _kalshi_books(tickers: list[str]) -> dict[str, dict[str, Any]]:
    return {
        ticker: {
            "orderbook_fp": {
                "yes_dollars": [["0.45", "400.00"]],
                "no_dollars": [["0.50", "400.00"]],
            }
        }
        for ticker in tickers
    }


def _family_markets(kind: str):
    if kind == "game_winner":
        _, kalshi = _normalize_kalshi_game()
        event_payload = _kalshi_game_event()
        series = {**KALSHI_SERIES_FEES, "ticker": "KXNFLGAME"}
        _, polymarket, pm_raw = _normalize_pm_family("moneyline")
        key = CANONICAL_NFL_GAME_WINNER
    elif kind == "spread":
        _, kalshi = _normalize_kalshi_spread()
        event_payload, _market = _kalshi_spread_event_and_market()
        series = {**KALSHI_SERIES_FEES, "ticker": "KXNFLSPREAD"}
        _, polymarket, pm_raw = _normalize_pm_family("spreads")
        key = f"{CANONICAL_NFL_POINT_SPREAD}:-6.5"
    else:
        _, kalshi = _normalize_kalshi_total()
        event_payload, _market = _kalshi_total_event_and_market()
        series = {**KALSHI_SERIES_FEES, "ticker": "KXNFLTOTAL"}
        _, polymarket, pm_raw = _normalize_pm_family("totals")
        key = f"{CANONICAL_NFL_TOTAL_POINTS}:47.5"
    return key, kalshi, event_payload, series, polymarket, pm_raw


def _persist_pair(
    store: SqliteApprovedMarketCatalogueStore,
    *,
    kalshi,
    polymarket,
    event_payload: dict[str, Any],
    series: dict[str, Any],
    pm_payload: dict[str, Any],
    generation: str = "g1",
):
    identity = pair_identity_from_markets(
        kalshi,
        polymarket,
        kalshi_event_payload=event_payload,
        kalshi_series_payload=series,
        polymarket_market_payload=pm_payload,
    )
    assert identity is not None
    persist_universe_catalogue_pass(
        store,
        canonical_event_id="evt-indkc",
        competition="NFL",
        home_canonical="kansas city chiefs",
        away_canonical="indianapolis colts",
        kickoff_utc=KICKOFF,
        pairs=[identity],
        now=NOW,
        generation_id=generation,
        family_discovery=None,
        terminal=False,
        allow_disappearance=False,
    )


def _scan_service() -> tuple[PaperScanService, SqliteMarketIntelligenceRepository]:
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes([fresh_usd_ecb_close(Decimal("0.75000000"), as_of=OBSERVED)])
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(
        intelligence,
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        fx_service=fx,
    )
    return service, repository


def test_identity_stub_has_no_fee_evidence() -> None:
    assert market_payload_has_fee_evidence(_identity_stub("3482783")) is False
    assert market_payload_has_fee_evidence({**_identity_stub("3482783"), **PM_DISABLED}) is True


def test_builder_reuses_captured_disabled_fee_on_identity_stub() -> None:
    _, market, raw = _normalize_pm_family("moneyline")
    captured = resolve_polymarket_fee_metadata({**raw, **PM_DISABLED})
    stub = _identity_stub(market.source_market_id)
    observation = PolymarketObservationBuilder().build(
        {"id": market.event.source_event_id, "title": "IND vs KC"},
        stub,
        _pm_books([runner.source_runner_id for runner in market.runners]),
        canonical=market,
        observed_at=OBSERVED,
        quote_age_ms=80,
        fee_snapshot=captured,
    )
    assert observation.metadata["polymarket_fee"]["fees_enabled"] is False
    snapshot = polymarket_cost_from_market(
        observation.metadata["polymarket_fee"],
        captured_at=OBSERVED,
        source_market_id=market.source_market_id,
    )
    assert snapshot.is_economically_known()
    assert snapshot.fee_basis is FeeBasis.NONE_CONFIRMED


def test_builder_reuses_captured_enabled_schedule_on_identity_stub() -> None:
    _, market, raw = _normalize_pm_family("moneyline")
    captured = resolve_polymarket_fee_metadata({**raw, **PM_ENABLED})
    observation = PolymarketObservationBuilder().build(
        {"id": market.event.source_event_id, "title": "IND vs KC"},
        _identity_stub(market.source_market_id),
        _pm_books([runner.source_runner_id for runner in market.runners]),
        canonical=market,
        observed_at=OBSERVED,
        quote_age_ms=80,
        fee_snapshot=captured,
    )
    meta = observation.metadata["polymarket_fee"]
    assert meta["fees_enabled"] is True
    snapshot = polymarket_cost_from_market(meta, captured_at=OBSERVED)
    assert snapshot.formula_name == POLYMARKET_TAKER_FORMULA
    assert snapshot.formula_parameters["rate"] == Decimal("0.03")


def test_live_payload_wins_over_captured_snapshot() -> None:
    _, market, raw = _normalize_pm_family("moneyline")
    captured = resolve_polymarket_fee_metadata({**raw, **PM_ENABLED})
    live = {**raw, **PM_DISABLED}
    observation = PolymarketObservationBuilder().build(
        {"id": market.event.source_event_id, "title": "IND vs KC"},
        live,
        _pm_books([runner.source_runner_id for runner in market.runners]),
        canonical=market,
        observed_at=OBSERVED,
        quote_age_ms=80,
        fee_snapshot=captured,
    )
    assert observation.metadata["polymarket_fee"]["fees_enabled"] is False


def test_stub_without_captured_fee_still_fails_closed() -> None:
    _, market, _raw = _normalize_pm_family("moneyline")
    observation = PolymarketObservationBuilder().build(
        {"id": market.event.source_event_id, "title": "IND vs KC"},
        _identity_stub(market.source_market_id),
        _pm_books([runner.source_runner_id for runner in market.runners]),
        canonical=market,
        observed_at=OBSERVED,
        quote_age_ms=80,
    )
    snapshot = polymarket_cost_from_market(
        observation.metadata["polymarket_fee"], captured_at=OBSERVED
    )
    assert snapshot.known_status is CostKnownStatus.UNKNOWN
    assert snapshot.fee_basis is FeeBasis.UNKNOWN


@pytest.mark.parametrize("kind", ["game_winner", "spread", "total"])
def test_catalogue_background_reconstruction_preserves_known_fees(kind: str) -> None:
    key, kalshi, event_payload, series, polymarket, pm_raw = _family_markets(kind)
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist_pair(
            store,
            kalshi=kalshi,
            polymarket=polymarket,
            event_payload=event_payload,
            series=series,
            pm_payload={**pm_raw, **PM_DISABLED},
        )
        row = store.get_active("evt-indkc", key)
        assert row is not None
        kalshi_snap = store.get_fee_snapshot(row.kalshi_fee_snapshot_id or "")
        assert kalshi_snap is not None
        assert kalshi_snap.fee_resolution_status == FEE_STATUS_KNOWN
        assert kalshi_snap.fee_type == "quadratic_with_maker_fees"
        assert kalshi_snap.fee_multiplier == "1"
        pm_snap = store.get_polymarket_fee_snapshot(row.polymarket_fee_snapshot_id or "")
        assert pm_snap is not None
        assert pm_snap.fees_enabled is False
        assert pm_snap.is_known()

        identity_only = pair_identity_from_markets(kalshi, polymarket)
        assert identity_only is not None
        persist_universe_catalogue_pass(
            store,
            canonical_event_id="evt-indkc",
            competition="NFL",
            home_canonical="kansas city chiefs",
            away_canonical="indianapolis colts",
            kickoff_utc=KICKOFF,
            pairs=[identity_only],
            now=NOW,
            generation_id="g2",
            family_discovery=None,
            terminal=False,
            allow_disappearance=False,
        )
        held = store.get_active("evt-indkc", key)
        assert held is not None
        assert held.polymarket_fee_snapshot_id == row.polymarket_fee_snapshot_id
        assert held.kalshi_fee_snapshot_id == row.kalshi_fee_snapshot_id

        working = derived_price_engine_working_set([held])
        assert working
        engine = CataloguePriceEngine(catalogue_store=store, clock=lambda: NOW)
        tokens = [item.native_id for item in working[0].polymarket_token_ids]
        reconstructed = engine._build_polymarket_obs(
            working[0],
            {
                token: RetrievedVenuePayload(payload=book, retrieved_at=NOW)
                for token, book in _pm_books(tokens).items()
            },
            NOW,
        )
        assert reconstructed.metadata["polymarket_fee"]["fees_enabled"] is False
        kalshi_obs = engine._build_kalshi_obs(
            working[0],
            {
                ticker: RetrievedVenuePayload(payload=book, retrieved_at=NOW)
                for ticker, book in _kalshi_books(working[0].kalshi_market_tickers).items()
            },
            NOW,
        )
        assert kalshi_obs.metadata["kalshi_fee"]["fee_type"] == "quadratic_with_maker_fees"
        assert str(kalshi_obs.metadata["kalshi_fee"]["fee_multiplier"]) == "1"
    finally:
        store.close()


def test_catalogue_enabled_schedule_reconstructs_formula() -> None:
    _key, kalshi, event_payload, series, polymarket, pm_raw = _family_markets("game_winner")
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist_pair(
            store,
            kalshi=kalshi,
            polymarket=polymarket,
            event_payload=event_payload,
            series=series,
            pm_payload={**pm_raw, **PM_ENABLED},
        )
        row = store.get_active("evt-indkc", CANONICAL_NFL_GAME_WINNER)
        assert row is not None
        snapshot = store.get_polymarket_fee_snapshot(row.polymarket_fee_snapshot_id or "")
        assert snapshot is not None
        cost = polymarket_cost_from_market(snapshot.observation_metadata(), captured_at=NOW)
        assert cost.formula_name == POLYMARKET_TAKER_FORMULA
        engine = CataloguePriceEngine(catalogue_store=store, clock=lambda: NOW)
        identity = derived_price_engine_working_set([row])[0]
        tokens = [item.native_id for item in identity.polymarket_token_ids]
        reconstructed = engine._build_polymarket_obs(
            identity,
            {
                token: RetrievedVenuePayload(payload=book, retrieved_at=NOW)
                for token, book in _pm_books(tokens).items()
            },
            NOW,
        )
        cost = polymarket_cost_from_market(
            reconstructed.metadata["polymarket_fee"], captured_at=NOW
        )
        assert cost.is_economically_known()
        assert cost.formula_name == POLYMARKET_TAKER_FORMULA
    finally:
        store.close()


def test_missing_polymarket_fee_metadata_still_fails_closed() -> None:
    record = polymarket_fee_snapshot_from_payload(
        {"id": "bare"},
        captured_at=NOW,
        source="gamma_market_payload",
        source_market_id="bare",
    )
    assert record.fee_resolution_status == FEE_STATUS_UNKNOWN
    assert record.fees_enabled is None
    cost = polymarket_cost_from_market(record.observation_metadata(), captured_at=NOW)
    assert not cost.is_economically_known()


def test_paper_scan_reconstructed_nfl_game_winner_computes_edges() -> None:
    _key, kalshi_market, _event_payload, _series, pm_market, pm_raw = _family_markets("game_winner")
    kalshi_tickers = [
        runner.source_runner_id.rsplit(":", 1)[0] for runner in kalshi_market.runners
    ]
    kalshi_obs = KalshiObservationBuilder().build_from_canonical(
        kalshi_market,
        _kalshi_books(kalshi_tickers),
        observed_at=OBSERVED,
        quote_age_ms=90,
        fee_snapshot={
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": "1",
            "fee_provenance": "series",
        },
    )
    pm_obs = PolymarketObservationBuilder().build(
        {"id": pm_market.event.source_event_id, "title": "IND vs KC"},
        _identity_stub(pm_market.source_market_id),
        _pm_books([runner.source_runner_id for runner in pm_market.runners]),
        canonical=pm_market,
        observed_at=OBSERVED,
        quote_age_ms=110,
        fee_snapshot=resolve_polymarket_fee_metadata({**pm_raw, **PM_DISABLED}),
    )
    service, repository = _scan_service()
    try:
        decision = service.scan_pair(kalshi_obs, pm_obs, maximum_execution_risk=100)
        joined = " ".join(decision.rejection_reasons)
        assert "unknown_required_venue_cost" not in joined
        assert decision.market_match.matched is True
        assert decision.market_match.confidence == 1.0
        assert all(snapshot.is_economically_known() for snapshot in decision.venue_costs)
        assert decision.payoff_scan is not None or decision.depth_scan is not None
        history = repository.list_snapshots()
        record = build_paper_scan_record(decision, history)
        assert record.gross_edge is not None
        assert record.net_edge is not None
    finally:
        repository.close()


def test_paper_scan_missing_fee_metadata_fails_closed_without_edges() -> None:
    _key, kalshi_market, _event, _series, pm_market, _pm_raw = _family_markets("game_winner")
    kalshi_tickers = [
        runner.source_runner_id.rsplit(":", 1)[0] for runner in kalshi_market.runners
    ]
    kalshi_obs = KalshiObservationBuilder().build_from_canonical(
        kalshi_market,
        _kalshi_books(kalshi_tickers),
        observed_at=OBSERVED,
        quote_age_ms=90,
        fee_snapshot={
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": "1",
            "fee_provenance": "series",
        },
    )
    pm_obs = PolymarketObservationBuilder().build(
        {"id": pm_market.event.source_event_id, "title": "IND vs KC"},
        _identity_stub(pm_market.source_market_id),
        _pm_books([runner.source_runner_id for runner in pm_market.runners]),
        canonical=pm_market,
        observed_at=OBSERVED,
        quote_age_ms=110,
    )
    service, repository = _scan_service()
    try:
        decision = service.scan_pair(kalshi_obs, pm_obs, maximum_execution_risk=100)
        assert any(
            "unknown_required_venue_cost:polymarket" in reason
            or reason.startswith("missing_venue_cost:polymarket")
            for reason in decision.rejection_reasons
        )
        assert decision.payoff_scan is None
        assert decision.depth_scan is None
    finally:
        repository.close()


def test_hot_identity_stub_reuses_leg_fee_snapshot() -> None:
    _, market, raw = _normalize_pm_family("moneyline")
    captured = resolve_polymarket_fee_metadata({**raw, **PM_DISABLED})
    leg = HotVenueLeg(
        venue=VenueName.POLYMARKET,
        source_event_id=market.event.source_event_id,
        source_market_id=market.source_market_id,
        source_runner_ids=[runner.source_runner_id for runner in market.runners],
        canonical_identity=market.model_dump(mode="json"),
        fee_snapshot=captured,
    )
    observation = PolymarketObservationBuilder().build(
        {"id": leg.source_event_id, "title": "IND vs KC"},
        {"id": leg.source_market_id, "question": "game_winner"},
        _pm_books(leg.source_runner_ids),
        canonical=market,
        observed_at=OBSERVED,
        quote_age_ms=40,
        fee_snapshot=leg.fee_snapshot,
    )
    assert observation.metadata["polymarket_fee"]["fees_enabled"] is False
