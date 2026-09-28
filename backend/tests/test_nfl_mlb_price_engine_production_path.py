"""NFL/MLB production path: catalogue → Price Engine → PaperScan → inventory.

Captured/synthetic KCMIA and STL/PIT shapes. No venue writes. PAPER only.
These regressions fail when Matchbook reconstruction drops sport evidence,
when money_line is compared after normalize_text, when TOTAL_RUNS is outside
the Phase-1 work gate, or when a USD row only receives FX from a pair decision.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fx_test_helpers import fresh_usd_ecb_close
from test_issue344_price_engine import FakeKalshi, FakeMatchbook
from test_nfl_mlb_sibling_identity import (
    _mlb_game_payload,
    _mlb_milestone,
    _mlb_total_payload,
    _nfl_game_payload,
    _nfl_spread_payload,
    _nfl_total_payload,
)

from sports_hedge.application.approved_market_catalogue import derived_price_engine_working_set
from sports_hedge.application.catalogue_maintenance import (
    pair_identity_from_markets,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import InventoryMarket, assemble_fixture_inventory
from sports_hedge.application.market_observation import KalshiObservationBuilder, MatchbookObservationBuilder
from sports_hedge.application.opportunity_viability import reset_opportunity_viability_cache
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    PriceEnginePriority,
    _synthetic_matchbook_event,
)
from sports_hedge.application.provider_access import ProviderAccessLayer, reset_shared_provider_access
from sports_hedge.catalogue.admission import catalogue_allows_live_execution
from sports_hedge.catalogue.registry import family_is_phase1_expensive_work
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
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.scanner_context import bind_lane_fx
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.mlb.constants import CANONICAL_MLB_GAME_WINNER, CANONICAL_MLB_TOTAL_RUNS
from sports_hedge.mlb.normalize import kalshi_mlb_event, kalshi_mlb_markets
from sports_hedge.nfl.constants import (
    CANONICAL_NFL_GAME_WINNER,
    CANONICAL_NFL_POINT_SPREAD,
    CANONICAL_NFL_TOTAL_POINTS,
)
from sports_hedge.nfl.detect import is_nfl_payload
from sports_hedge.nfl.normalize import kalshi_nfl_event, kalshi_nfl_markets
from sports_hedge.normalization.venues import MatchbookNormalizer, VenueNormalizationError
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

USD_RATE = Decimal("0.78123456")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
KICKOFF = NOW + timedelta(days=2)


@pytest.fixture(autouse=True)
def _reset_providers() -> None:
    reset_shared_provider_access()
    reset_opportunity_viability_cache()
    yield
    reset_shared_provider_access()
    reset_opportunity_viability_cache()


def test_nfl_money_line_market_type_survives_text_normalization() -> None:
    """name=Moneyline used to hide this: normalize_text('money_line') is 'money line'."""

    event = _nfl_event()
    market = MatchbookNormalizer().normalize_market(event, _nfl_winner_payload())
    assert market.family is MarketFamily.GAME_WINNER
    assert market.event.sport == "american_football"
    assert registered_canonical_key(
        kalshi_nfl_markets(kalshi_nfl_event(_nfl_game_payload()), _nfl_game_payload()["markets"])[0],
        market,
    ) == CANONICAL_NFL_GAME_WINNER


def test_mlb_money_line_market_type_survives_text_normalization() -> None:
    event = _mlb_event()
    market = MatchbookNormalizer().normalize_market(event, _mlb_winner_payload())
    assert market.family is MarketFamily.GAME_WINNER
    assert market.event.sport == "baseball"
    assert registered_canonical_key(
        kalshi_mlb_markets(kalshi_mlb_event(_mlb_game_payload()), _mlb_game_payload()["markets"])[0],
        market,
    ) == CANONICAL_MLB_GAME_WINNER


def test_total_runs_is_phase1_expensive_work() -> None:
    assert family_is_phase1_expensive_work(MarketFamily.TOTAL_RUNS) is True


def test_price_engine_reconstructs_nfl_game_winner_not_match_result() -> None:
    """Sport-name alone is not NFL evidence. The synthetic event must carry it."""

    bare = {
        "id": "mb-kcmia",
        "name": "Kansas City Chiefs at Miami Dolphins",
        "sport-name": "American Football",
        "competition-name": "NFL",
    }
    assert is_nfl_payload(bare) is False
    _store, rows = _persist_sport(
        "evt-kcmia",
        "NFL",
        [_nfl_winner_pair()],
    )
    identity = derived_price_engine_working_set(rows)[0]
    event = _synthetic_matchbook_event(identity)
    assert str(event["sport-id"]) == "1"
    assert any(
        str(tag.get("name")) == "NFL" and str(tag.get("type")).upper() == "COMPETITION"
        for tag in event["meta-tags"]
    )
    assert event["name"].casefold().startswith("kansas city chiefs at ")
    observed = MatchbookObservationBuilder().build(event, _priced(_nfl_winner_payload()), observed_at=NOW)
    assert observed.market.family is MarketFamily.GAME_WINNER
    assert observed.market.family is not MarketFamily.MATCH_RESULT


def test_universal_fx_snapshot_covers_football_nfl_and_mlb_venue_only_rows() -> None:
    service = _fx_service()
    snapshots, reason = bind_lane_fx(explicit=None, fx_service=service, as_of=NOW)
    assert reason is None
    assert snapshots is not None
    usd = next(item for item in snapshots if item.currency == "USD")
    assert usd.gbp_per_unit == USD_RATE
    assert usd.source != "paper_demo_fx_snapshot"
    for sport, family in (
        ("football", MarketFamily.MATCH_RESULT),
        ("american_football", MarketFamily.GAME_WINNER),
        ("baseball", MarketFamily.TOTAL_RUNS),
    ):
        row = _venue_only_usd_row(sport, family, snapshots)
        assert row.comparison_status == "venue_only" or row.kalshi is not None
        assert row.kalshi is not None
        assert row.kalshi.fx_status == "known"
        assert row.kalshi.native_currency == "USD"
    missing = _venue_only_usd_row("american_football", MarketFamily.GAME_WINNER, None)
    assert missing.kalshi is not None
    assert missing.kalshi.fx_status == "missing"


def test_stale_fx_fails_closed_once_for_every_sport() -> None:
    service = _fx_service(age_days=30)
    snapshots, reason = bind_lane_fx(explicit=None, fx_service=service, as_of=NOW)
    assert snapshots is None
    assert reason == "stale_fx_rate:USD"
    again, same_reason = bind_lane_fx(explicit=None, fx_service=service, as_of=NOW)
    assert again is None
    assert same_reason == reason


@pytest.mark.asyncio
async def test_kcmia_and_mlb_production_path_stays_paper_only() -> None:
    fx = _fx_service()
    nfl = await _price_fixture(
        fx,
        canonical_event_id="evt-kcmia",
        competition="NFL",
        pairs=[_nfl_winner_pair(), _nfl_spread_pair(), _nfl_total_pair()],
        payloads={
            "mb-ml": _priced(_nfl_winner_payload()),
            "mb-spread-35": _priced(_nfl_spread_payload_market()),
            "mb-total-445": _priced(_nfl_total_payload_market()),
        },
    )
    mlb = await _price_fixture(
        fx,
        canonical_event_id="evt-stlpit",
        competition="MLB",
        pairs=[_mlb_winner_pair(), _mlb_total_pair()],
        payloads={
            "mb-mlb-ml": _priced(_mlb_winner_payload()),
            "mb-mlb-total": _priced(_mlb_total_payload_market()),
        },
    )
    _assert_shared_fx(nfl, mlb)
    _assert_nfl_economics(nfl)
    _assert_mlb_economics(mlb)
    _assert_unsupported_stay_out()
    assert Settings().sports_hedge_execution_enabled is False
    assert Settings().sports_hedge_mode == "paper"


def _assert_shared_fx(nfl: dict, mlb: dict) -> None:
    nfl_usd = next(item for item in nfl["engine"].fx_snapshots if item.currency == "USD")
    mlb_usd = next(item for item in mlb["engine"].fx_snapshots if item.currency == "USD")
    assert nfl_usd.gbp_per_unit == mlb_usd.gbp_per_unit == USD_RATE
    assert nfl_usd.source_date == mlb_usd.source_date
    football = _venue_only_usd_row("football", MarketFamily.MATCH_RESULT, nfl["engine"].fx_snapshots)
    assert football.kalshi is not None
    assert football.kalshi.fx_status == "known"


def _assert_nfl_economics(priced: dict) -> None:
    keys = {item.register_canonical_key for item in priced["working"]}
    assert CANONICAL_NFL_GAME_WINNER in keys
    assert f"{CANONICAL_NFL_POINT_SPREAD}:-3.5" in keys
    assert f"{CANONICAL_NFL_TOTAL_POINTS}:44.5" in keys
    assert priced["result"].evaluated
    assert not priced["result"].revalidation
    assert len(priced["result"].decisions) == 3
    for decision in priced["result"].decisions:
        assert decision.market_match.matched is True
        assert "incomplete_settlement" not in decision.rejection_reasons
        assert any(item.currency == "USD" for item in decision.fx_snapshots)
        assert all(item.is_economically_known() for item in decision.venue_costs)
        assert "unknown_required_venue_cost:kalshi" not in " ".join(decision.rejection_reasons)
        assert decision.depth_scan is not None or decision.payoff_scan is not None
        assert decision.solver_model
    detail = priced["detail"]
    assert detail is not None
    assert detail.fixture.sport == "american_football"
    labels = [row.display_label for row in detail.fixture.catalogue_coverage.rows]
    assert any(label.startswith("Game Winner") for label in labels)
    assert any("Point Spread" in label and "3.5" in label for label in labels)
    assert any("Total Points" in label and "44.5" in label for label in labels)
    assert all("Match Result" not in label and "1X2" not in label for label in labels)
    by_family = {_family_name(row) for row in detail.markets}
    markets = {_family_name(row): row for row in detail.markets}
    assert "game_winner" in by_family
    for family in ("game_winner", "point_spread", "total_points"):
        row = markets[family]
        assert str(row.comparison_status) == "paper_assumed_equivalent", (
            family,
            row.comparison_status,
            row.reason,
            row.rejection_reasons,
            row.match_reasons,
            row.matchbook is not None,
            row.kalshi is not None,
            row.solver_model,
        )
        assert row.entered_solver is True
        assert row.solver_model
        assert "incomplete_settlement" not in row.rejection_reasons
    for row in detail.markets:
        assert _family_name(row) != "match_result"
        if row.kalshi is not None:
            assert row.kalshi.fx_status == "known"
            assert row.kalshi.fee_status == "known"
        if row.matchbook is not None:
            assert row.matchbook.fee_status == "known"
    for kalshi, matchbook, _series_payload in (
        _nfl_winner_pair(),
        _nfl_spread_pair(),
        _nfl_total_pair(),
    ):
        assert catalogue_allows_live_execution(kalshi, matchbook) is False


def _assert_mlb_economics(priced: dict) -> None:
    keys = {item.register_canonical_key for item in priced["working"]}
    assert CANONICAL_MLB_GAME_WINNER in keys
    assert f"{CANONICAL_MLB_TOTAL_RUNS}:7.5" in keys
    assert priced["result"].evaluated
    assert not priced["result"].revalidation
    assert len(priced["result"].decisions) == 2, (
        [(item.market_match.reasons, item.rejection_reasons) for item in priced["result"].decisions],
        priced["result"].revalidation,
        priced["result"].issues,
    )
    for decision in priced["result"].decisions:
        assert decision.market_match.matched is True, (
            decision.market_match.reasons,
            decision.rejection_reasons,
            decision.canonical_market_id,
        )
        assert "incomplete_settlement" not in decision.rejection_reasons
        assert "match_result" not in (decision.canonical_market_id or "")
        assert any(item.currency == "USD" and item.gbp_per_unit == USD_RATE for item in decision.fx_snapshots)
        assert all(item.is_economically_known() for item in decision.venue_costs)
        assert decision.depth_scan is not None or decision.payoff_scan is not None
    detail = priced["detail"]
    assert detail is not None
    assert detail.fixture.sport == "baseball"
    labels = [row.display_label for row in detail.fixture.catalogue_coverage.rows]
    assert any(label.startswith("Game Winner") for label in labels)
    assert any("Total Runs" in label and "7.5" in label for label in labels)
    assert all("Match Result" not in label and "Both Teams" not in label for label in labels)
    markets = {_family_name(row): row for row in detail.markets}
    for family in ("game_winner", "total_runs"):
        row = markets[family]
        assert str(row.comparison_status) == "paper_assumed_equivalent", (
            family,
            row.comparison_status,
            row.reason,
            row.rejection_reasons,
        )
        assert row.entered_solver is True
        assert row.solver_model
        assert "incomplete_settlement" not in row.rejection_reasons
    for row in detail.markets:
        if row.kalshi is not None:
            assert row.kalshi.fx_status == "known"
            assert row.kalshi.fee_status == "known"
    assert catalogue_allows_live_execution(*_mlb_winner_pair()[:2]) is False
    assert catalogue_allows_live_execution(*_mlb_total_pair()[:2]) is False


def _assert_unsupported_stay_out() -> None:
    event = _nfl_event()
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": "mb-1h",
                "name": "First Half Money Line",
                "market-type": "money_line",
                "runners": [
                    {"id": "a", "name": "Kansas City Chiefs"},
                    {"id": "h", "name": "Miami Dolphins"},
                ],
            },
        )
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": "mb-player",
                "name": "Player passing yards",
                "market-type": "total",
                "runners": [
                    {"id": "o", "name": "Over 250.5", "handicap": "250.5"},
                    {"id": "u", "name": "Under 250.5", "handicap": "250.5"},
                ],
            },
        )
    with pytest.raises(VenueNormalizationError):
        kalshi_mlb_event(
            {
                "event_ticker": "KXMLBSPREAD-26SEP241235STLPIT",
                "series_ticker": "KXMLBSPREAD",
                "title": "STL Cardinals vs PIT Pirates run line",
                "milestone": _mlb_milestone(),
            }
        )


async def _price_fixture(
    fx: FxRateService,
    *,
    canonical_event_id: str,
    competition: str,
    pairs: list[tuple],
    payloads: dict[str, dict],
) -> dict:
    store, rows = _persist_sport(canonical_event_id, competition, pairs)
    working = derived_price_engine_working_set(rows)
    assert working
    assert all(item.kalshi_fee_snapshot_id for item in working)
    repository = SqliteMarketIntelligenceRepository()
    try:
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
        matchbook = FakeMatchbook()
        matchbook.payloads = payloads
        state = FixtureCurrentStateStore()
        engine = CataloguePriceEngine(
            catalogue_store=store,
            matchbook=matchbook,
            kalshi=FakeKalshi(),
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
        result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        await engine.observability.drain()
        detail = state.detail(canonical_event_id, now=NOW)
        return {
            "engine": engine,
            "result": result,
            "detail": detail,
            "working": working,
        }
    finally:
        repository.close()


def _family_name(row) -> str:
    family = getattr(row, "family", None)
    if family is None:
        return ""
    return family.value if isinstance(family, MarketFamily) else str(family)


def _persist_sport(canonical_event_id: str, competition: str, pairs: list[tuple]):
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    anchor = pairs[0][0]
    identities = []
    for kalshi, matchbook, series in pairs:
        identity = pair_identity_from_markets(
            kalshi,
            matchbook,
            kalshi_series_payload=series,
        )
        assert identity is not None
        identities.append(identity)
    rows = persist_universe_catalogue_pass(
        store,
        canonical_event_id=canonical_event_id,
        competition=competition,
        home_canonical=anchor.event.home_team,
        away_canonical=anchor.event.away_team,
        kickoff_utc=KICKOFF,
        pairs=identities,
        now=NOW,
        generation_id=f"g-{canonical_event_id}",
        family_discovery=None,
        terminal=False,
        allow_disappearance=False,
    )
    return store, rows


def _fx_service(age_days: int = 1) -> FxRateService:
    service = FxRateService(SqliteFxRateRepository())
    service.persist_ecb_closes([fresh_usd_ecb_close(USD_RATE, as_of=NOW, age_days=age_days)])
    return service


def _venue_only_usd_row(sport: str, family: MarketFamily, snapshots):
    source = f"{sport}-{family.value}"
    market = CanonicalMarket(
        event=CanonicalEvent(
            sport=sport,
            competition=sport,
            home_team="home",
            away_team="away",
            kickoff_utc=KICKOFF,
            source_venue=VenueName.KALSHI,
            source_event_id=source,
        ),
        source_venue=VenueName.KALSHI,
        source_market_id=source,
        family=family,
        period=FootballPeriod.FULL_TIME,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            push_possible=False,
            penalties_included=False,
            extra_time_included=False,
        ),
        runners=[
            CanonicalRunner(
                source_runner_id=f"{source}-H:YES",
                outcome=CanonicalOutcome.HOME,
                label="home",
            ),
            CanonicalRunner(
                source_runner_id=f"{source}-A:YES",
                outcome=CanonicalOutcome.AWAY,
                label="away",
            ),
        ],
    )
    books = {
        f"{source}-H": {"orderbook_fp": {"yes_dollars": [["0.45", "100"]], "no_dollars": [["0.50", "100"]]}},
        f"{source}-A": {"orderbook_fp": {"yes_dollars": [["0.45", "100"]], "no_dollars": [["0.50", "100"]]}},
    }
    observation = KalshiObservationBuilder().build_from_canonical(
        market,
        books,
        observed_at=NOW,
        quote_age_ms=20,
    )
    rows = assemble_fixture_inventory(
        [],
        [],
        kalshi_markets=[
            InventoryMarket(
                venue=VenueName.KALSHI,
                source_event_id=source,
                source_market_id=source,
                raw_name=family.value,
                canonical=market,
                observation=observation,
            )
        ],
        fx_snapshots=snapshots,
        cost_resolver=VenueCostResolver(),
    )
    assert len(rows) == 1
    return rows[0]


def _priced(payload: dict) -> dict:
    priced = copy.deepcopy(payload)
    for runner in priced.get("runners") or []:
        runner["prices"] = [
            {"side": "back", "odds": "2.10", "available-amount": "500"},
            {"side": "lay", "odds": "2.16", "available-amount": "500"},
        ]
    return priced


def _series(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": "1",
    }


def _nfl_event():
    return MatchbookNormalizer().normalize_event(
        {
            "id": "mb-kcmia",
            "name": "Kansas City Chiefs at Miami Dolphins",
            "start": KICKOFF.isoformat(),
            "sport-id": "1",
            "sport-name": "American Football",
            "meta-tags": [
                {"id": "1", "name": "American Football", "type": "SPORT"},
                {"id": "491503123380010", "name": "NFL", "type": "COMPETITION"},
            ],
            "status": "open",
        }
    )


def _mlb_event():
    return MatchbookNormalizer().normalize_event(
        {
            "id": "mb-stlpit",
            "name": "St. Louis Cardinals at Pittsburgh Pirates",
            "start": KICKOFF.isoformat(),
            "sport-id": 3,
            "sport-name": "Baseball",
            "meta-tags": [
                {"id": "3", "name": "Baseball", "type": "SPORT"},
                {"id": "1494669213760003", "name": "MLB", "type": "COMPETITION"},
            ],
            "status": "open",
        }
    )


def _nfl_winner_payload() -> dict:
    return {
        "id": "mb-ml",
        "name": "Money Line",
        "market-type": "money_line",
        "status": "open",
        "runners": [
            {"id": "away", "name": "Kansas City Chiefs"},
            {"id": "home", "name": "Miami Dolphins"},
        ],
    }


def _nfl_spread_payload_market() -> dict:
    return {
        "id": "mb-spread-35",
        "name": "Handicap",
        "market-type": "handicap",
        "status": "open",
        "runners": [
            {"id": "away-s", "name": "Kansas City Chiefs +3.5", "handicap": "3.5"},
            {"id": "home-s", "name": "Miami Dolphins -3.5", "handicap": "-3.5"},
        ],
    }


def _nfl_total_payload_market() -> dict:
    return {
        "id": "mb-total-445",
        "name": "Total",
        "market-type": "total",
        "status": "open",
        "runners": [
            {"id": "over", "name": "Over 44.5", "handicap": "44.5"},
            {"id": "under", "name": "Under 44.5", "handicap": "44.5"},
        ],
    }


def _mlb_winner_payload() -> dict:
    return {
        "id": "mb-mlb-ml",
        "name": "Money Line",
        "market-type": "money_line",
        "status": "open",
        "runners": [
            {"id": "stl", "name": "St. Louis Cardinals"},
            {"id": "pit", "name": "Pittsburgh Pirates"},
        ],
    }


def _mlb_total_payload_market() -> dict:
    return {
        "id": "mb-mlb-total",
        "name": "Total",
        "market-type": "total",
        "status": "open",
        "handicap": "7.5",
        "runners": [
            {"id": "over", "name": "Over 7.5"},
            {"id": "under", "name": "Under 7.5"},
        ],
    }


def _nfl_winner_pair():
    kalshi = kalshi_nfl_markets(kalshi_nfl_event(_nfl_game_payload()), _nfl_game_payload()["markets"])[0]
    matchbook = MatchbookNormalizer().normalize_market(_nfl_event(), _nfl_winner_payload())
    return kalshi, matchbook, _series("KXNFLGAME")


def _nfl_spread_pair():
    payload = _nfl_spread_payload()
    kalshi = kalshi_nfl_markets(kalshi_nfl_event(payload), payload["markets"])[0]
    matchbook = MatchbookNormalizer().normalize_market(_nfl_event(), _nfl_spread_payload_market())
    return kalshi, matchbook, _series("KXNFLSPREAD")


def _nfl_total_pair():
    payload = _nfl_total_payload()
    kalshi = kalshi_nfl_markets(kalshi_nfl_event(payload), payload["markets"])[0]
    matchbook = MatchbookNormalizer().normalize_market(_nfl_event(), _nfl_total_payload_market())
    return kalshi, matchbook, _series("KXNFLTOTAL")


def _mlb_winner_pair():
    kalshi = kalshi_mlb_markets(kalshi_mlb_event(_mlb_game_payload()), _mlb_game_payload()["markets"])[0]
    matchbook = MatchbookNormalizer().normalize_market(_mlb_event(), _mlb_winner_payload())
    return kalshi, matchbook, _series("KXMLBGAME")


def _mlb_total_pair():
    payload = _mlb_total_payload()
    kalshi = kalshi_mlb_markets(
        kalshi_mlb_event(payload),
        payload["markets"],
        event_payload=payload,
    )[0]
    matchbook = MatchbookNormalizer().normalize_market(_mlb_event(), _mlb_total_payload_market())
    return kalshi, matchbook, _series("KXMLBTOTAL")
