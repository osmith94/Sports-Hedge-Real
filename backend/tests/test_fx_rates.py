from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from sports_hedge.fx.models import FxCheckStatus, FxRateUnavailable, PublishedFxClose, variance_bps
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService, ecb_publication_window_open, london_calendar_date
from sports_hedge.fx.sources import parse_boe_xudluss_csv, parse_ecb_eurofxref_daily
from sports_hedge.fx.schedule import AccountingSchedule

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-09-11">
      <Cube currency="USD" rate="1.1470"/>
      <Cube currency="GBP" rate="0.86025"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""

LONDON = ZoneInfo("Europe/London")
BERLIN = ZoneInfo("Europe/Berlin")


def _service() -> FxRateService:
    return FxRateService(SqliteFxRateRepository(), check_tolerance_bps=Decimal("25"))


def test_ecb_derives_gbp_per_usd_and_persists_raw_provenance() -> None:
    retrieved = datetime(2026, 9, 11, 16, 5, tzinfo=ZoneInfo("Europe/Berlin"))
    closes = parse_ecb_eurofxref_daily(ECB_XML, retrieved_at=retrieved)
    usd = next(item for item in closes if item.currency == "USD")
    assert usd.source_date == date(2026, 9, 11)
    assert usd.gbp_per_unit == (Decimal("0.86025") / Decimal("1.1470")).quantize(Decimal("0.00000001"))
    assert usd.raw["gbp_per_eur"] == "0.86025"
    service = _service()
    stored = service.persist_ecb_closes(closes)
    row = next(item for item in stored if item.currency == "USD")
    assert row.status is FxCheckStatus.PENDING_CHECK
    assert row.primary_source == "ecb_eurofxref"
    assert row.valuation_date == date(2026, 9, 11)


def test_primary_rate_is_not_overwritten_on_reingest() -> None:
    retrieved = datetime(2026, 9, 11, 16, 5, tzinfo=UTC)
    service = _service()
    first = PublishedFxClose(
        currency="USD",
        gbp_per_unit=Decimal("0.75000000"),
        source_date=date(2026, 9, 11),
        retrieved_at=retrieved,
        source="ecb_eurofxref",
        source_id="ecb:2026-09-11:USD",
        raw={"usd_per_eur": "1.1470"},
    )
    second = first.model_copy(update={"gbp_per_unit": Decimal("0.80000000")})
    service.persist_ecb_closes([first])
    again = service.persist_ecb_closes([second])
    assert again[0].gbp_per_unit == Decimal("0.75000000")


def test_boe_agreed_and_exception_never_replace_primary() -> None:
    retrieved = datetime(2026, 9, 11, 16, 10, tzinfo=UTC)
    service = _service()
    primary = Decimal("0.75000000")
    service.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=primary,
                source_date=date(2026, 9, 11),
                retrieved_at=retrieved,
                source="ecb_eurofxref",
                source_id="ecb:2026-09-11:USD",
            )
        ]
    )
    agreed_usd_per_gbp = Decimal("1") / primary
    agreed_csv = f"DATE,XUDLUSS\n2026-09-11,{agreed_usd_per_gbp}\n"
    checks = parse_boe_xudluss_csv(agreed_csv, retrieved_at=retrieved)
    agreed = service.apply_boe_checks(checks)[0]
    assert agreed.status is FxCheckStatus.AGREED
    assert agreed.gbp_per_unit == primary
    assert agreed.variance_bps is not None
    assert agreed.variance_bps <= Decimal("25")

    exception_csv = "DATE,XUDLUSS\n2026-09-11,1.1000\n"
    exception = service.apply_boe_checks(parse_boe_xudluss_csv(exception_csv, retrieved_at=retrieved))[0]
    assert exception.status is FxCheckStatus.EXCEPTION
    assert exception.gbp_per_unit == primary
    assert exception.check_gbp_per_unit == (Decimal("1") / Decimal("1.1000")).quantize(
        Decimal("0.00000001")
    )
    assert variance_bps(primary, exception.check_gbp_per_unit) > Decimal("25")


def test_weekend_carry_forward_keeps_source_date() -> None:
    retrieved = datetime(2026, 9, 11, 16, 5, tzinfo=UTC)
    service = _service()
    service.persist_ecb_closes(parse_ecb_eurofxref_daily(ECB_XML, retrieved_at=retrieved))
    saturday = datetime(2026, 9, 12, 0, 5, tzinfo=LONDON)
    carried = service.ensure_valuation_rate("USD", date(2026, 9, 12), as_of=saturday)
    assert carried.status is FxCheckStatus.CARRIED_FORWARD
    assert carried.source_date == date(2026, 9, 11)
    assert carried.valuation_date == date(2026, 9, 12)
    again = service.ensure_valuation_rate("USD", date(2026, 9, 12), as_of=saturday)
    assert again.gbp_per_unit == carried.gbp_per_unit
    snapshot = service.resolve_for_scanner("USD", as_of=saturday)
    assert snapshot.gbp_per_unit == carried.gbp_per_unit
    assert snapshot.source_date == date(2026, 9, 11)
    assert snapshot.check_status == FxCheckStatus.CARRIED_FORWARD.value


def test_published_close_replaces_same_date_carry_forward_placeholder() -> None:
    service = _service()
    friday = datetime(2026, 9, 11, 16, 15, tzinfo=LONDON)
    service.persist_ecb_closes(parse_ecb_eurofxref_daily(ECB_XML, retrieved_at=friday))
    monday = datetime(2026, 9, 14, 0, 1, tzinfo=LONDON)
    carried = service.ensure_valuation_rate("USD", date(2026, 9, 14), as_of=monday)
    assert carried.status is FxCheckStatus.CARRIED_FORWARD
    assert carried.source_date == date(2026, 9, 11)
    monday_close = parse_ecb_eurofxref_daily(MONDAY_ECB_XML, retrieved_at=monday)
    stored = service.persist_ecb_closes(monday_close)
    usd = next(item for item in stored if item.currency == "USD")
    assert usd.status is FxCheckStatus.PENDING_CHECK
    assert usd.source_date == date(2026, 9, 14)
    assert usd.valuation_date == date(2026, 9, 14)
    row = service.repository.get("USD", date(2026, 9, 14))
    assert row is not None
    assert row.status is FxCheckStatus.PENDING_CHECK
    assert row.source_date == date(2026, 9, 14)


def test_missing_and_stale_fx_fail_closed() -> None:
    service = _service()
    as_of = datetime(2026, 9, 12, 12, 0, tzinfo=LONDON)
    with pytest.raises(FxRateUnavailable, match="missing_fx_rate:USD"):
        service.resolve_for_scanner("USD", as_of=as_of)
    old = PublishedFxClose(
        currency="USD",
        gbp_per_unit=Decimal("0.75"),
        source_date=date(2026, 8, 1),
        retrieved_at=datetime(2026, 8, 1, 16, tzinfo=UTC),
        source="ecb_eurofxref",
        source_id="ecb:2026-08-01:USD",
    )
    service.persist_ecb_closes([old])
    with pytest.raises(FxRateUnavailable, match="stale_fx_rate:USD"):
        service.resolve_for_scanner("USD", as_of=as_of)


def test_ecb_is_not_treated_as_midnight_publication() -> None:
    friday_midnight = datetime(2026, 9, 11, 0, 0, tzinfo=LONDON)
    assert ecb_publication_window_open(friday_midnight) is False
    friday_before_window = datetime(2026, 9, 11, 16, 14, tzinfo=LONDON)
    assert ecb_publication_window_open(friday_before_window) is False
    friday_berlin_afternoon = datetime(2026, 9, 11, 16, 5, tzinfo=BERLIN)
    assert ecb_publication_window_open(friday_berlin_afternoon) is False
    friday_uk_window = datetime(2026, 9, 11, 16, 15, tzinfo=LONDON)
    assert ecb_publication_window_open(friday_uk_window) is True
    saturday = datetime(2026, 9, 12, 17, 0, tzinfo=LONDON)
    assert ecb_publication_window_open(saturday) is False


def test_scheduler_midnight_does_not_fetch_and_is_idempotent() -> None:
    service = _service()
    friday = datetime(2026, 9, 11, 16, 5, tzinfo=UTC)
    service.persist_ecb_closes(parse_ecb_eurofxref_daily(ECB_XML, retrieved_at=friday))

    def boom_http() -> None:
        raise AssertionError("ECB must not be fetched at the London valuation cut-off")

    schedule = AccountingSchedule(
        service,
        clock=lambda: datetime(2026, 9, 12, 0, 1, tzinfo=LONDON),
        http_factory=boom_http,  # type: ignore[arg-type]
        enabled=True,
    )
    first = schedule.run_due_jobs()
    assert first["revaluation"] == "2026-09-12"
    assert "ingest" not in first
    carried = service.repository.get("USD", date(2026, 9, 12))
    assert carried is not None
    assert carried.status is FxCheckStatus.CARRIED_FORWARD
    second = schedule.run_due_jobs()
    assert second == {}
    assert london_calendar_date(datetime(2026, 9, 12, 0, 1, tzinfo=LONDON)) == date(2026, 9, 12)


MONDAY_ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-09-14">
      <Cube currency="USD" rate="1.1500"/>
      <Cube currency="GBP" rate="0.86100"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


class _FakeFxResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class FakeFxHttp:
    def __init__(self, *, ecb_xml: str, boe_csv: str | None = None, fail: bool = False) -> None:
        self.ecb_xml = ecb_xml
        self.boe_csv = boe_csv
        self.fail = fail
        self.ecb_gets = 0
        self.boe_gets = 0

    def __enter__(self) -> "FakeFxHttp":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, *, timeout: float = 30.0) -> _FakeFxResponse:
        if self.fail:
            raise RuntimeError("fx source unreachable")
        if "eurofxref" in url:
            self.ecb_gets += 1
            return _FakeFxResponse(self.ecb_xml)
        self.boe_gets += 1
        if self.boe_csv is None:
            raise RuntimeError("boe unavailable")
        return _FakeFxResponse(self.boe_csv)


def test_sunday_bootstrap_persists_friday_close_as_carried_forward() -> None:
    service = _service()
    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=LONDON)
    http = FakeFxHttp(ecb_xml=ECB_XML)
    schedule = AccountingSchedule(
        service,
        clock=lambda: sunday,
        http_factory=lambda: http,
        enabled=True,
    )
    actions = schedule.bootstrap_if_needed()
    assert actions["bootstrap"] == "2026-09-11"
    assert http.ecb_gets == 1
    snapshot = service.resolve_for_scanner("USD", as_of=sunday)
    assert snapshot.source == "ecb_eurofxref"
    assert snapshot.source_date == date(2026, 9, 11)
    assert snapshot.valuation_date == date(2026, 9, 13)
    assert snapshot.check_status == FxCheckStatus.CARRIED_FORWARD.value
    assert snapshot.gbp_per_unit != Decimal("0.80")
    again = schedule.bootstrap_if_needed()
    assert again == {}
    assert http.ecb_gets == 1
    ticks = schedule.run_due_jobs()
    assert "ingest" not in ticks
    assert http.ecb_gets == 1


def test_daily_window_ingest_is_once_per_london_working_day() -> None:
    service = _service()
    friday_window = datetime(2026, 9, 11, 16, 15, tzinfo=LONDON)
    http = FakeFxHttp(ecb_xml=ECB_XML)
    schedule = AccountingSchedule(
        service,
        clock=lambda: friday_window,
        http_factory=lambda: http,
        enabled=True,
    )
    first = schedule.run_due_jobs()
    assert first["ingest"] == "2026-09-11"
    assert http.ecb_gets == 1
    second = schedule.run_due_jobs()
    assert "ingest" not in second
    assert http.ecb_gets == 1

    monday_window = datetime(2026, 9, 14, 16, 15, tzinfo=LONDON)
    http.ecb_xml = MONDAY_ECB_XML
    schedule.clock = lambda: monday_window
    refreshed = schedule.run_due_jobs()
    assert refreshed["ingest"] == "2026-09-14"
    assert http.ecb_gets == 2
    snapshot = service.resolve_for_scanner("USD", as_of=monday_window)
    assert snapshot.source_date == date(2026, 9, 14)
    third = schedule.run_due_jobs()
    assert "ingest" not in third
    assert http.ecb_gets == 2


def test_bootstrap_failure_stays_fail_closed_without_demo_fx() -> None:
    service = _service()
    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=LONDON)
    http = FakeFxHttp(ecb_xml=ECB_XML, fail=True)
    schedule = AccountingSchedule(
        service,
        clock=lambda: sunday,
        http_factory=lambda: http,
        enabled=True,
    )
    actions = schedule.bootstrap_if_needed()
    assert "bootstrap_error" in actions
    with pytest.raises(FxRateUnavailable, match="missing_fx_rate:USD"):
        service.resolve_for_scanner("USD", as_of=sunday)
    status = schedule.operator_status(as_of=sunday)
    assert status["enabled"] is True
    assert status["last_error"]
    assert status["scanner_usd"]["available"] is False
    assert status["scanner_usd"]["reason"] == "missing_fx_rate:USD"


def test_sunday_bootstrap_unblocks_usd_matched_paper_scan() -> None:
    from sports_hedge.application.market_observation import (
        MatchbookObservationBuilder,
        PolymarketObservationBuilder,
    )
    from sports_hedge.application.paper_scan import PaperScanService
    from sports_hedge.fees.resolver import VenueCostResolver
    from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
    from sports_hedge.market_intelligence.service import MarketIntelligenceService
    from test_paper_scan_pipeline import matchbook_payloads, polymarket_payloads

    service = _service()
    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=LONDON)
    http = FakeFxHttp(ecb_xml=ECB_XML)
    schedule = AccountingSchedule(
        service,
        clock=lambda: sunday,
        http_factory=lambda: http,
        enabled=True,
    )
    schedule.bootstrap_if_needed()
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    scan = PaperScanService(
        intelligence,
        fx_service=service,
        cost_resolver=VenueCostResolver(),
    )
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=sunday, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=sunday, quote_age_ms=180
    )
    decision = scan.scan_pair(matchbook, polymarket, maximum_execution_risk=100)
    assert "missing_fx_rate:USD" not in decision.rejection_reasons
    usd = next(item for item in decision.fx_snapshots if item.currency == "USD")
    assert usd.source == "ecb_eurofxref"
    assert usd.source_date == date(2026, 9, 11)
    assert usd.gbp_per_unit != Decimal("0.80")

