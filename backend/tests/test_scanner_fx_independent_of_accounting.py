"""Operational scanner FX runs when accounting revaluation is off.

Fixture ECB XML only. No live network and no order submission.
SPORTS_HEDGE_EXECUTION_ENABLED stays false. Treasury demo FX is rejected.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_duplicate_economic_authorities import (
    PM_FORMULA,
    _assert_solver_economics,
    _grouped_formula_payload,
)
from test_fx_rates import FakeFxHttp, _service
from test_mb_polymarket_price_engine_parity import (
    ENABLED,
    TOKEN_AWAY,
    TOKEN_DRAW,
    TOKEN_HOME,
    RecordingKalshi,
    RecordingMatchbook,
    RecordingPolymarket,
    _football_markets,
    _football_match_odds,
    _persist,
    _pm_books,
)
from test_nfl_stage1b_paper_markets import (
    _mb_indkc,
    _mb_market,
    _normalize_mb_family,
    _normalize_pm_family,
)

from sports_hedge.accounting.revaluation import DailyFxRevaluationService
from sports_hedge.application.catalogue_maintenance import pair_identity_from_markets
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.application.provider_access import (
    ProviderAccessLayer,
    reset_shared_provider_access,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.models import FxRateUnavailable, PublishedFxClose
from sports_hedge.fx.scanner_context import resolve_scanner_economic_fx
from sports_hedge.fx.schedule import AccountingSchedule
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

LONDON = ZoneInfo("Europe/London")
SUNDAY = datetime(2026, 10, 4, 12, 0, tzinfo=LONDON)
FRIDAY = date(2026, 10, 2)
MONDAY_WINDOW = datetime(2026, 10, 5, 16, 15, tzinfo=LONDON)
REPO_ROOT = Path(__file__).resolve().parents[2]

FRIDAY_ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-10-02">
      <Cube currency="USD" rate="1.1700"/>
      <Cube currency="GBP" rate="0.87000"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""

MONDAY_ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-10-05">
      <Cube currency="USD" rate="1.1710"/>
      <Cube currency="GBP" rate="0.87100"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


@pytest.fixture(autouse=True)
def _reset_shared_provider() -> None:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


def _real_settings() -> Settings:
    return Settings(
        sports_hedge_mode="real",
        accounting_schedule_enabled=False,
        sports_hedge_execution_enabled=False,
        paper_live_refresh_enabled=True,
    )


def _schedule(
    service,
    http: FakeFxHttp,
    *,
    when: datetime,
    enabled: bool,
) -> AccountingSchedule:
    return AccountingSchedule(
        service,
        clock=lambda: when,
        http_factory=lambda: http,
        enabled=enabled,
    )


def _spy_revaluation(schedule: AccountingSchedule) -> list[date]:
    calls: list[date] = []
    original = schedule.revaluation.run

    def _run(*, valuation_date: date, as_of: datetime):
        calls.append(valuation_date)
        return original(valuation_date=valuation_date, as_of=as_of)

    schedule.revaluation.run = _run  # type: ignore[method-assign]
    return calls


def _friday_close() -> PublishedFxClose:
    return PublishedFxClose(
        currency="USD",
        gbp_per_unit=(Decimal("0.87000") / Decimal("1.1700")).quantize(Decimal("0.00000001")),
        source_date=FRIDAY,
        retrieved_at=datetime(2026, 10, 2, 16, 15, tzinfo=LONDON),
        source="ecb_eurofxref",
        source_id="ecb:2026-10-02:USD",
    )


async def _price_with_bootstrapped_fx(
    store: SqliteApprovedMarketCatalogueStore,
    fx,
    *,
    canonical_event_id: str,
    matchbook_payloads: dict,
    polymarket_books: dict,
) -> dict:
    repository = SqliteMarketIntelligenceRepository()
    scan = PaperScanService(
        MarketIntelligenceService(repository),
        settings=Settings(
            sports_hedge_mode="real",
            sports_hedge_execution_enabled=False,
            accounting_schedule_enabled=False,
            min_net_edge=0,
            max_slippage_bps=0,
            fx_spread_bps=0,
            max_execution_risk=100,
        ),
        fx_service=fx,
        cost_resolver=VenueCostResolver(),
        clock=lambda: SUNDAY.astimezone(UTC),
    )
    state = FixtureCurrentStateStore()
    engine = CataloguePriceEngine(
        catalogue_store=store,
        matchbook=RecordingMatchbook(matchbook_payloads),
        kalshi=RecordingKalshi(),
        polymarket=RecordingPolymarket(polymarket_books),
        paper_scan=scan,
        fixture_state=state,
        provider_access=ProviderAccessLayer(
            {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
        ),
        clock=lambda: SUNDAY.astimezone(UTC),
        provider_timeout_seconds=2,
        hot_interval_seconds=0,
        background_interval_seconds=0,
    )
    engine.set_enabled_venues(ENABLED)
    try:
        hot = await engine.run_slice(PriceEnginePriority.HOT, now=SUNDAY.astimezone(UTC))
        background = await engine.run_slice(
            PriceEnginePriority.BACKGROUND, now=SUNDAY.astimezone(UTC)
        )
        await engine.observability.drain()
        return {
            "result": background,
            "hot": hot,
            "detail": state.detail(canonical_event_id, now=SUNDAY.astimezone(UTC)),
            "engine": engine,
        }
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_real_startup_bootstraps_friday_close_while_accounting_stays_off() -> None:
    settings = _real_settings()
    assert settings.sports_hedge_mode == "real"
    assert settings.accounting_schedule_enabled is False
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_live_refresh_enabled is True
    assert Settings().sports_hedge_execution_enabled is False

    service = _service()
    http = FakeFxHttp(ecb_xml=FRIDAY_ECB_XML)
    schedule = _schedule(service, http, when=SUNDAY, enabled=settings.accounting_schedule_enabled)
    revaluation_calls = _spy_revaluation(schedule)
    await schedule.start()
    try:
        assert revaluation_calls == []
        assert schedule.enabled is False
        assert isinstance(schedule.revaluation, DailyFxRevaluationService)
        snapshot = service.resolve_for_scanner("USD", as_of=SUNDAY)
    finally:
        await schedule.stop()

    assert http.ecb_gets == 1
    assert http.boe_gets == 0
    assert snapshot.source == "ecb_eurofxref"
    assert snapshot.source != "paper_demo_fx_snapshot"
    assert snapshot.source_date == FRIDAY
    assert snapshot.valuation_date == date(2026, 10, 4)
    assert snapshot.gbp_per_unit != Decimal("0.80")
    economic = resolve_scanner_economic_fx(service, as_of=SUNDAY)
    assert any(item.currency == "USD" and item.source == "ecb_eurofxref" for item in economic)
    status = schedule.operator_status(as_of=SUNDAY)
    assert status["enabled"] is False
    assert status["accounting_enabled"] is False
    assert status["fx_ingestion_enabled"] is True
    scanner_usd = status["scanner_usd"]
    assert scanner_usd["available"] is True
    assert scanner_usd["source"] == "ecb_eurofxref"
    assert scanner_usd["source_date"] == "2026-10-02"
    assert scanner_usd["valuation_date"] == "2026-10-04"
    assert revaluation_calls == []


def test_usable_persisted_usd_is_reused_without_another_ecb_fetch() -> None:
    service = _service()
    service.persist_ecb_closes([_friday_close()])
    http = FakeFxHttp(ecb_xml=FRIDAY_ECB_XML, fail=True)
    schedule = _schedule(service, http, when=SUNDAY, enabled=False)
    actions = schedule.run_due_jobs(startup=True)
    assert "bootstrap" not in actions
    assert "ingest" not in actions
    assert "bootstrap_error" not in actions
    assert http.ecb_gets == 0
    snapshot = service.resolve_for_scanner("USD", as_of=SUNDAY)
    assert snapshot.source == "ecb_eurofxref"
    assert snapshot.source_date == FRIDAY


def test_failed_bootstrap_stays_fail_closed_without_inventing_a_rate() -> None:
    service = _service()
    http = FakeFxHttp(ecb_xml=FRIDAY_ECB_XML, fail=True)
    schedule = _schedule(service, http, when=SUNDAY, enabled=False)
    actions = schedule.bootstrap_if_needed()
    assert "bootstrap_error" in actions
    with pytest.raises(FxRateUnavailable, match="missing_fx_rate:USD"):
        service.resolve_for_scanner("USD", as_of=SUNDAY)
    status = schedule.operator_status(as_of=SUNDAY)
    assert status["scanner_usd"]["available"] is False
    assert status["scanner_usd"]["reason"] == "missing_fx_rate:USD"

    stale = _service()
    stale.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=Decimal("0.75000000"),
                source_date=date(2026, 9, 18),
                retrieved_at=datetime(2026, 9, 18, 16, 15, tzinfo=LONDON),
                source="ecb_eurofxref",
                source_id="ecb:2026-09-18:USD",
            )
        ]
    )
    stale_http = FakeFxHttp(ecb_xml=FRIDAY_ECB_XML, fail=True)
    stale_schedule = _schedule(stale, stale_http, when=SUNDAY, enabled=False)
    failed = stale_schedule.bootstrap_if_needed()
    assert "bootstrap_error" in failed
    with pytest.raises(FxRateUnavailable, match="stale_fx_rate:USD"):
        stale.resolve_for_scanner("USD", as_of=SUNDAY)
    stored = stale.repository.latest_published("USD", on_or_before=date(2026, 10, 4))
    assert stored is not None
    assert stored.source_date == date(2026, 9, 18)
    assert stored.gbp_per_unit == Decimal("0.75000000")


def test_accounting_off_still_refreshes_the_publication_window_without_journals() -> None:
    service = _service()
    http = FakeFxHttp(ecb_xml=MONDAY_ECB_XML)
    schedule = _schedule(service, http, when=MONDAY_WINDOW, enabled=False)
    calls = _spy_revaluation(schedule)
    first = schedule.run_due_jobs()
    assert first["ingest"] == "2026-10-05"
    assert "revaluation" not in first
    assert calls == []
    assert http.ecb_gets == 1
    second = schedule.run_due_jobs()
    assert "ingest" not in second
    assert "revaluation" not in second
    assert calls == []
    assert http.ecb_gets == 1
    snapshot = service.resolve_for_scanner("USD", as_of=MONDAY_WINDOW)
    assert snapshot.source == "ecb_eurofxref"
    assert snapshot.source_date == date(2026, 10, 5)


def test_demo_treasury_fx_is_still_rejected_for_scanner_economics() -> None:
    class _DemoFx:
        def paper_snapshots(self, _currencies: set[str], *, as_of: datetime) -> list[FxRateSnapshot]:
            return [
                FxRateSnapshot(
                    currency="USD",
                    gbp_per_unit=Decimal("0.8"),
                    source="paper_demo_fx_snapshot",
                    captured_at=as_of,
                )
            ]

    with pytest.raises(FxRateUnavailable, match="missing_fx_rate:USD"):
        resolve_scanner_economic_fx(_DemoFx(), as_of=SUNDAY)


@pytest.mark.asyncio
async def test_bootstrapped_usd_fx_prices_football_and_nfl_without_another_fetch() -> None:
    service = _service()
    http = FakeFxHttp(ecb_xml=FRIDAY_ECB_XML)
    schedule = _schedule(service, http, when=SUNDAY, enabled=False)
    calls = _spy_revaluation(schedule)
    await schedule.start()
    await schedule.stop()
    assert calls == []
    assert http.ecb_gets == 1

    kickoff = SUNDAY.astimezone(UTC) + timedelta(hours=6)
    matchbook, polymarket = _football_markets()
    matchbook = matchbook.model_copy(
        update={"event": matchbook.event.model_copy(update={"kickoff_utc": kickoff})}
    )
    polymarket = polymarket.model_copy(
        update={"event": polymarket.event.model_copy(update={"kickoff_utc": kickoff})}
    )
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
            home=matchbook.event.home_team,
            away=matchbook.event.away_team,
            kickoff=matchbook.event.kickoff_utc,
        )
        priced = await _price_with_bootstrapped_fx(
            store,
            service,
            canonical_event_id="evt-brentford",
            matchbook_payloads={"41001": _football_match_odds()},
            polymarket_books=_pm_books([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]),
        )
    finally:
        store.close()
    _assert_solver_economics(priced)
    assert http.ecb_gets == 1
    assert "FX missing" not in " ".join(
        priced["result"].decisions[0].rejection_reasons
    )

    _, nfl_polymarket, raw = _normalize_pm_family("moneyline")
    market_payload = _mb_market(_mb_indkc(), name="Moneyline")
    for runner in market_payload.get("runners") or []:
        runner["prices"] = [
            {"side": "back", "odds": "2.20", "available-amount": "200"},
            {"side": "lay", "odds": "2.30", "available-amount": "200"},
        ]
    _, nfl_matchbook = _normalize_mb_family(market_payload)
    nfl_matchbook = nfl_matchbook.model_copy(
        update={"event": nfl_matchbook.event.model_copy(update={"kickoff_utc": kickoff})}
    )
    nfl_polymarket = nfl_polymarket.model_copy(
        update={"event": nfl_polymarket.event.model_copy(update={"kickoff_utc": kickoff})}
    )
    nfl_identity = pair_identity_from_markets(
        nfl_matchbook,
        nfl_polymarket,
        polymarket_market_payload={**raw, **PM_FORMULA},
    )
    assert nfl_identity is not None
    nfl_store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(
            nfl_store,
            [nfl_identity],
            canonical_event_id="evt-indkc",
            competition="NFL",
            home=nfl_matchbook.event.home_team,
            away=nfl_matchbook.event.away_team,
            kickoff=nfl_matchbook.event.kickoff_utc,
        )
        row = nfl_store.list_active()[0]
        tokens = [item.native_id for item in row.polymarket_token_ids]
        nfl_priced = await _price_with_bootstrapped_fx(
            nfl_store,
            service,
            canonical_event_id="evt-indkc",
            matchbook_payloads={str(market_payload["id"]): market_payload},
            polymarket_books=_pm_books(tokens),
        )
    finally:
        nfl_store.close()
    market = nfl_priced["detail"].markets[0]
    assert market.polymarket.fx_status == "known"
    assert market.current_net_edge is not None
    assert market.entered_solver is True
    assert http.ecb_gets == 1
    assert Settings().sports_hedge_execution_enabled is False


def test_real_launcher_keeps_accounting_and_execution_off() -> None:
    launcher = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Real.ps1").read_text(encoding="utf-8")
    assert '$env:ACCOUNTING_SCHEDULE_ENABLED = "false"' in launcher
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in launcher
    assert "ACCOUNTING_SCHEDULE_ENABLED=true" not in launcher.replace(" ", "")
