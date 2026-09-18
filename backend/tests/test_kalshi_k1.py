from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus, assemble_fixture_inventory
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.models import LegExecutionMode
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily, SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import FeeBasis, MarketAction, OrderRole
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.normalization.venues import KalshiNormalizer, VenueNormalizationError
from sports_hedge.paper.liquidity import default_pools
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads
from venue_cost_helpers import profit_commission_cost


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REGULATION = "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."

KALSHI_EVENT = {
    "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
    "series_ticker": "KXEPLGAME",
    "title": "Newcastle United vs Chelsea",
    "category": "Sports",
    "strike_date": KICKOFF.isoformat(),
}

KALSHI_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}


def _btts_market() -> dict:
    return {
        "ticker": "KXEPLGAME-26SEP20NEWCHE-BTTS",
        "event_ticker": KALSHI_EVENT["event_ticker"],
        "title": "Both Teams To Score",
        "yes_sub_title": "Yes",
        "rules_primary": REGULATION,
    }


def _btts_book() -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100.00"]],
            "no_dollars": [["0.49", "200.00"]],
        }
    }


def _match_result_markets() -> list[dict]:
    return [
        {
            "ticker": "KXEPL-NEW",
            "event_ticker": KALSHI_EVENT["event_ticker"],
            "title": "Newcastle United vs Chelsea",
            "yes_sub_title": "Newcastle United",
            "rules_primary": REGULATION,
        },
        {
            "ticker": "KXEPL-DRAW",
            "event_ticker": KALSHI_EVENT["event_ticker"],
            "title": "Newcastle United vs Chelsea",
            "yes_sub_title": "Draw",
            "rules_primary": REGULATION,
        },
        {
            "ticker": "KXEPL-CHE",
            "event_ticker": KALSHI_EVENT["event_ticker"],
            "title": "Newcastle United vs Chelsea",
            "yes_sub_title": "Chelsea",
            "rules_primary": REGULATION,
        },
    ]


def test_venue_identity_matches_matchbook_capability_posture() -> None:
    client = TestClient(app)
    body = client.get("/venues").json()
    by_venue = {item["venue"]: item for item in body}
    assert "kalshi" in by_venue
    assert by_venue["kalshi"]["capabilities"] == by_venue["matchbook"]["capabilities"]
    assert by_venue["kalshi"]["capabilities"]["data_enabled"] is True
    assert by_venue["kalshi"]["capabilities"]["paper_enabled"] is True
    assert by_venue["kalshi"]["capabilities"]["execution_enabled"] is False
    assert by_venue["kalshi"]["integration"] == "official_api"


def test_kalshi_is_not_manual_external_and_uses_internal_paper_lifecycle() -> None:
    from sports_hedge.application.paper_scan import _default_execution_mode

    assert _default_execution_mode(VenueName.KALSHI) == LegExecutionMode.INTERNAL
    assert _default_execution_mode(VenueName.MATCHBOOK) == LegExecutionMode.INTERNAL
    assert _default_execution_mode(VenueName.POLYMARKET) == LegExecutionMode.EXTERNAL_OPERATOR


def test_kalshi_usd_pool_is_distinct_from_polymarket_usd() -> None:
    pools = default_pools(
        matchbook_gbp=Decimal("10"),
        polymarket_usd=Decimal("20"),
        kalshi_usd=Decimal("30"),
    )
    by_venue = {pool.venue: pool for pool in pools}
    assert by_venue[VenueName.KALSHI].native_currency == "USD"
    assert by_venue[VenueName.POLYMARKET].native_currency == "USD"
    assert by_venue[VenueName.KALSHI].available == Decimal("30")
    assert by_venue[VenueName.POLYMARKET].available == Decimal("20")
    assert by_venue[VenueName.KALSHI].included_in_solver is True


def test_kalshi_match_result_groups_home_draw_away_when_rules_match() -> None:
    normalizer = KalshiNormalizer()
    event = normalizer.normalize_event(KALSHI_EVENT, series=KALSHI_SERIES)
    markets = normalizer.assemble_canonical_markets(event, _match_result_markets(), series=KALSHI_SERIES)
    assert len(markets) == 1
    market = markets[0]
    assert market.family is MarketFamily.MATCH_RESULT
    assert market.settlement.scope is SettlementScope.REGULATION_TIME
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]


def test_kalshi_ftts_requires_three_states_and_regulation_rules() -> None:
    normalizer = KalshiNormalizer()
    event = normalizer.normalize_event(KALSHI_EVENT, series=KALSHI_SERIES)
    markets = [
        {
            "ticker": "FTTS-H",
            "title": "First team to score",
            "yes_sub_title": "Newcastle United",
            "rules_primary": REGULATION,
        },
        {
            "ticker": "FTTS-A",
            "title": "First team to score",
            "yes_sub_title": "Chelsea",
            "rules_primary": REGULATION,
        },
        {
            "ticker": "FTTS-N",
            "title": "First team to score",
            "yes_sub_title": "No Goal",
            "rules_primary": REGULATION,
        },
    ]
    assembled = normalizer.assemble_canonical_markets(event, markets, series=KALSHI_SERIES)
    assert assembled[0].family is MarketFamily.FIRST_TEAM_TO_SCORE
    ambiguous = [{**item, "rules_primary": "Winner of the match."} for item in markets]
    incomplete = normalizer.assemble_canonical_markets(event, ambiguous, series=KALSHI_SERIES)
    assert len(incomplete) == 1
    assert incomplete[0].family is MarketFamily.FIRST_TEAM_TO_SCORE
    assert incomplete[0].settlement.is_economically_complete() is False
    assert {runner.outcome for runner in incomplete[0].runners} == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    }


def test_ambiguous_settlement_is_visible_but_incomplete() -> None:
    normalizer = KalshiNormalizer()
    event = normalizer.normalize_event(KALSHI_EVENT, series=KALSHI_SERIES)
    market = normalizer.normalize_market(
        event,
        {
            "ticker": "BTTS-AMBIG",
            "title": "Both Teams To Score",
            "yes_sub_title": "Yes",
            "rules_primary": "See contract URL.",
        },
    )
    assert market.family is MarketFamily.BOTH_TEAMS_TO_SCORE
    assert market.settlement.scope is SettlementScope.UNKNOWN
    assert market.settlement.is_economically_complete() is False


def test_orderbook_yes_no_bids_convert_to_complement_buy_economics() -> None:
    observation = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _btts_market(),
        {_btts_market()["ticker"]: _btts_book()},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=50,
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    yes_book = observation.book_for(CanonicalOutcome.YES)
    no_book = observation.book_for(CanonicalOutcome.NO)
    assert yes_book is not None and no_book is not None
    assert yes_book.best_back is not None
    assert yes_book.best_back.decimal_odds == Decimal("1") / Decimal("0.51")
    assert yes_book.best_back.available_stake == Decimal("0.51") * Decimal("200.00")
    assert no_book.best_back is not None
    assert no_book.best_back.decimal_odds == Decimal("1") / Decimal("0.60")
    assert yes_book.raw_book["complement"] == "yes_ask = 1 - no_bid"
    assert no_book.raw_book["contract_side"] == "NO"
    assert observation.native_currency == "USD"
    assert observation.venue is VenueName.KALSHI


def test_known_quadratic_fee_changes_net_economics_unknown_formula_fails_closed() -> None:
    known = kalshi_cost_from_series(
        KALSHI_SERIES,
        captured_at=OBSERVED,
        source_market_id="KX-BTTS",
    )
    assert known.fee_basis is FeeBasis.FORMULA
    assert known.action is MarketAction.BUY
    with_fee = apply_venue_costs(
        known,
        gross_decimal_odds=Decimal("2"),
        stake=Decimal("1"),
        require_gbp=False,
    )
    none = apply_venue_costs(
        kalshi_cost_from_series(
            {"fee_type": "quadratic", "fee_multiplier": 0},
            captured_at=OBSERVED,
            order_role=OrderRole.MAKER,
        ),
        gross_decimal_odds=Decimal("2"),
        require_gbp=False,
    )
    assert with_fee.venue_fee > 0
    assert with_fee.net_decimal_equivalent < Decimal("2")
    assert none.venue_fee == 0
    unknown = kalshi_cost_from_series({"fee_type": "flat", "fee_multiplier": 1}, captured_at=OBSERVED)
    assert unknown.known_status.value == "unknown"
    with pytest.raises(CostRuleError):
        apply_venue_costs(unknown, gross_decimal_odds=Decimal("2"), require_gbp=False)


def test_event_fee_override_changes_net_economics_and_records_provenance() -> None:
    from sports_hedge.fees.kalshi import (
        FEE_PROVENANCE_EVENT_OVERRIDE,
        FEE_PROVENANCE_SERIES,
        resolve_kalshi_fee_metadata,
    )

    event = {
        **KALSHI_EVENT,
        "fee_type_override": "quadratic",
        "fee_multiplier_override": 2,
    }
    series_cost = kalshi_cost_from_series(KALSHI_SERIES, captured_at=OBSERVED)
    override_cost = kalshi_cost_from_series(KALSHI_SERIES, event=event, captured_at=OBSERVED)
    series_net = apply_venue_costs(
        series_cost, gross_decimal_odds=Decimal("2"), stake=Decimal("1"), require_gbp=False
    )
    override_net = apply_venue_costs(
        override_cost, gross_decimal_odds=Decimal("2"), stake=Decimal("1"), require_gbp=False
    )
    assert override_net.venue_fee > series_net.venue_fee
    assert FEE_PROVENANCE_SERIES in series_cost.source
    assert FEE_PROVENANCE_EVENT_OVERRIDE in override_cost.source
    meta = resolve_kalshi_fee_metadata(event=event, series=KALSHI_SERIES)
    assert meta["fee_provenance"] == FEE_PROVENANCE_EVENT_OVERRIDE
    assert Decimal(str(meta["fee_multiplier"])) == Decimal("2")
    assert "fee_resolution_error" not in meta


def test_partial_event_fee_override_fails_closed_without_series_fallback() -> None:
    from sports_hedge.fees.kalshi import FEE_PROVENANCE_EVENT_OVERRIDE, resolve_kalshi_fee_metadata

    event = {**KALSHI_EVENT, "fee_type_override": "quadratic"}
    meta = resolve_kalshi_fee_metadata(event=event, series=KALSHI_SERIES)
    assert meta["fee_resolution_error"] == "partial_event_fee_override"
    snapshot = kalshi_cost_from_series(KALSHI_SERIES, event=event, captured_at=OBSERVED)
    assert snapshot.known_status.value == "unknown"
    assert FEE_PROVENANCE_EVENT_OVERRIDE in snapshot.source
    assert "no series fallback" in (snapshot.detail or "")
    with pytest.raises(CostRuleError):
        apply_venue_costs(snapshot, gross_decimal_odds=Decimal("2"), require_gbp=False)


def test_unsupported_event_fee_override_rejects_scan_inventory_visible() -> None:
    from sports_hedge.application.fixture_inventory import InventoryMarket
    from sports_hedge.fees.kalshi import FEE_PROVENANCE_EVENT_OVERRIDE, resolve_kalshi_fee_metadata

    captured = datetime.now(UTC)
    event = {
        **KALSHI_EVENT,
        "fee_type_override": "flat",
        "fee_multiplier_override": 1,
    }
    fee_meta = resolve_kalshi_fee_metadata(event=event, series=KALSHI_SERIES)
    kalshi = KalshiObservationBuilder().build(
        event,
        _btts_market(),
        {_btts_market()["ticker"]: _btts_book()},
        series=KALSHI_SERIES,
        observed_at=captured,
        source_latency_ms=37,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot=fee_meta,
    )
    assert kalshi.source_latency_ms == 37
    unknown = kalshi_cost_from_series(fee_meta, captured_at=captured)
    assert unknown.known_status.value == "unknown"
    assert FEE_PROVENANCE_EVENT_OVERRIDE in unknown.source
    mb_event, mb_market = matchbook_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=captured, quote_age_ms=80
    )
    decision = _scan_service().scan_pair(
        matchbook,
        kalshi,
        venue_costs=[
            profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured),
            unknown,
        ],
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")],
        maximum_execution_risk=100,
    )
    assert decision.eligible_for_paper_simulation is False
    assert any("unknown" in reason or "unsupported" in reason for reason in decision.rejection_reasons)
    rows = assemble_fixture_inventory(
        [
            InventoryMarket(
                venue=VenueName.MATCHBOOK,
                source_event_id="1001",
                source_market_id="2001",
                raw_name="Both Teams To Score",
                canonical=matchbook.market,
                observation=matchbook,
            )
        ],
        [],
        kalshi_markets=[
            InventoryMarket(
                venue=VenueName.KALSHI,
                source_event_id=event["event_ticker"],
                source_market_id=_btts_market()["ticker"],
                raw_name="Both Teams To Score",
                canonical=kalshi.market,
                observation=kalshi,
            )
        ],
        venue_costs=[
            profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured),
            unknown,
        ],
    )
    btts = [row for row in rows if row.family == "both_teams_to_score"]
    assert len(btts) == 1
    assert btts[0].kalshi is not None
    assert btts[0].kalshi.fee_status == "unknown"
    assert btts[0].entered_solver is False


def _scan_service() -> PaperScanService:
    return PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))


def _kalshi_btts_observation() -> object:
    return KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _btts_market(),
        {_btts_market()["ticker"]: _btts_book()},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )


def test_matchbook_kalshi_scan_does_not_require_polymarket() -> None:
    mb_event, mb_market = matchbook_payloads()
    captured = datetime.now(UTC)
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=captured, quote_age_ms=80
    )
    kalshi = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _btts_market(),
        {_btts_market()["ticker"]: _btts_book()},
        series=KALSHI_SERIES,
        observed_at=captured,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    decision = _scan_service().scan_pair(
        matchbook,
        kalshi,
        venue_costs=[
            profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured),
            kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured),
        ],
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")],
        maximum_execution_risk=100,
    )
    assert {matchbook.venue, kalshi.venue} == {VenueName.MATCHBOOK, VenueName.KALSHI}
    assert decision.execution_modes[VenueName.KALSHI] == LegExecutionMode.INTERNAL
    assert decision.solver_model == "simple_complete_set"


def test_polymarket_kalshi_scan_does_not_require_matchbook() -> None:
    pm_event, pm_market, pm_books = polymarket_payloads()
    captured = datetime.now(UTC)
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=captured, quote_age_ms=90
    )
    kalshi = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _btts_market(),
        {_btts_market()["ticker"]: _btts_book()},
        series=KALSHI_SERIES,
        observed_at=captured,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    decision = _scan_service().scan_pair(
        polymarket,
        kalshi,
        venue_costs=[
            profit_commission_cost(VenueName.POLYMARKET, "0", captured_at=captured),
            kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured),
        ],
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")],
        maximum_execution_risk=100,
    )
    assert VenueName.MATCHBOOK not in decision.execution_modes
    assert decision.execution_modes[VenueName.KALSHI] == LegExecutionMode.INTERNAL
    assert decision.solver_model == "simple_complete_set"


def test_three_venue_inventory_does_not_duplicate_canonical_markets() -> None:
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook_obs = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=80
    )
    polymarket_obs = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=90
    )
    kalshi_obs = _kalshi_btts_observation()
    from sports_hedge.application.fixture_inventory import InventoryMarket

    rows = assemble_fixture_inventory(
        [
            InventoryMarket(
                venue=VenueName.MATCHBOOK,
                source_event_id="1001",
                source_market_id="2001",
                raw_name="Both Teams To Score",
                canonical=matchbook_obs.market,
                observation=matchbook_obs,
            )
        ],
        [
            InventoryMarket(
                venue=VenueName.POLYMARKET,
                source_event_id="pm-event-1",
                source_market_id="pm-market-1",
                raw_name="Both teams to score?",
                canonical=polymarket_obs.market,
                observation=polymarket_obs,
            )
        ],
        kalshi_markets=[
            InventoryMarket(
                venue=VenueName.KALSHI,
                source_event_id=KALSHI_EVENT["event_ticker"],
                source_market_id=_btts_market()["ticker"],
                raw_name="Both Teams To Score",
                canonical=kalshi_obs.market,
                observation=kalshi_obs,
            )
        ],
    )
    btts = [row for row in rows if row.family == "both_teams_to_score"]
    assert len(btts) == 1
    assert btts[0].matchbook is not None
    assert btts[0].polymarket is not None
    assert btts[0].kalshi is not None
    assert btts[0].comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT


def test_target_competition_aliases_match_without_fuzzy_penalty() -> None:
    from sports_hedge.domain.football import CanonicalEvent
    from sports_hedge.matching.events import EventMatcher

    matcher = EventMatcher()
    matchbook = CanonicalEvent(
        competition="Premier League",
        home_team="Newcastle United",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        source_venue=VenueName.MATCHBOOK,
        source_event_id="mb",
    )
    kalshi = CanonicalEvent(
        competition="English Premier League",
        home_team="Newcastle United",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        source_venue=VenueName.KALSHI,
        source_event_id="ks",
    )
    result = matcher.match(matchbook, kalshi)
    assert result.matched
    assert result.confidence >= 0.98
    assert "competition_fuzzy" not in result.reasons


@pytest.mark.asyncio
async def test_kalshi_client_paginates_events_and_has_no_trading_methods() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path.endswith("/events") and not request.url.params.get("cursor"):
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_ticker": "E1",
                            "title": "A vs B",
                            "strike_date": "2026-09-20T15:00:00Z",
                        }
                    ],
                    "cursor": "next",
                },
            )
        if request.url.path.endswith("/events") and request.url.params.get("cursor") == "next":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_ticker": "E2",
                            "title": "C vs D",
                            "strike_date": "2026-09-20T17:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    settings = Settings(
        kalshi_series_tickers=["KXEPLGAME"],
        kalshi_event_page_limit=1,
        kalshi_event_max_pages=5,
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        venue = KalshiClient(settings, client=http)
        payload = await venue.list_events()

    assert [item["event_ticker"] for item in payload["events"]] == ["E1", "E2"]
    assert venue.capabilities.execution_enabled is False
    assert not hasattr(venue, "place_order")
    assert not hasattr(venue, "cancel_order")
    assert all("/trade-api/v2/events" in str(url) for url in seen)
    assert all(url.path.endswith("/events") for url in seen)
    assert all(url.params.get("with_milestones") == "true" for url in seen)


@pytest.mark.asyncio
async def test_kalshi_get_market_reads_contract_rules_and_caches() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": "KXEPLGAME-BET",
                    "event_ticker": "KXEPLGAME-26SEP20BETGET",
                    "rules_primary": REGULATION,
                    "rules_secondary": "Secondary contract terms.",
                }
            },
        )

    settings = Settings()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        venue = KalshiClient(settings, client=http)
        first = await venue.get_market("KXEPLGAME-BET")
        second = await venue.get_market("KXEPLGAME-BET")

    assert first["rules_primary"] == REGULATION
    assert first["rules_secondary"] == "Secondary contract terms."
    assert second is first
    assert seen == ["/trade-api/v2/markets/KXEPLGAME-BET"]
    assert not hasattr(venue, "place_order")


@pytest.mark.asyncio
async def test_kalshi_orderbook_rejects_deprecated_cent_only_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orderbook": {"yes": [[7, 10]], "no": [[93, 10]]}})

    settings = Settings()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        venue = KalshiClient(settings, client=http)
        with pytest.raises(Exception, match="orderbook_fp"):
            await venue.get_order_book("event", "TICKER")
