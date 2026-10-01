"""Issue #284: fail-closed Kalshi settlement blockers on #280.

Exclusion-only extra-time/penalty wording is not regulation evidence.
Captured Kalshi cancellation/reschedule-to-fair-price is material and cannot
silently disappear before APPROVED_EQUIVALENT. PAPER / execution off.
Data class: captured public payload shape plus deterministic fixtures.
Not live quotes, not owner-live books, not modelled probabilities.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.catalogue.admission import assess_catalogue_admission, catalogue_allows_solver
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import (
    CANCEL_RESCHEDULE_FAIR_PRICE,
    GAMEWIN_TEMPLATE,
    KALSHI_GAMEWIN_SERIES,
    NINETY_MINUTE_EXCLUSION,
    _kalshi,
    _kalshi_1x2,
    _mb,
    _mb_1x2,
)
from sports_hedge.catalogue.matrix import (
    MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_ASSUMPTION_ID,
    MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_COMPATIBLE,
)
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome, FootballPeriod, MarketFamily, SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON,
    KalshiNormalizer,
    classify_kalshi_rule_structure,
    classify_settlement_wording,
    kalshi_documented_result_scope_fingerprint,
    kalshi_text_has_cancel_reschedule_fair_price,
    matchbook_kalshi_cancel_reschedule_fair_price_compatible,
    resolve_kalshi_settlement_from_rule_fields,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CAPTURED_BAYERN = FIXTURES / "kalshi_trade_api_bundesliga_bayern_union_2026-09-18.json"
KICKOFF = datetime(2026, 9, 18, 18, 30, tzinfo=UTC)
CURRENT_PRIMARY = (
    "If Bayern Munich wins the Bayern Munich vs Union Berlin professional "
    "Bundesliga soccer game originally scheduled for Sep 18, 2026 after 90 "
    "minutes plus stoppage time (does not include extra time or penalties), "
    "then the market resolves to Yes."
)
CURRENT_SECONDARY = (
    "The following market refers to the Bayern Munich vs Union Berlin "
    "professional Bundesliga soccer game originally scheduled for Sep 18, 2026 "
    "after 90 minutes plus stoppage time (does not include extra time or "
    "penalties). If the game ends in a tie, the market called \"Tie\" resolves "
    "to Yes. If the game is cancelled or rescheduled to over 48 hours away, "
    "the market will resolve to a fair price in accordance with the rules."
)
EXCLUSION_ONLY = (
    "This does not include extra time or penalties.",
    "does not include extra time or penalties",
    "excluding extra time or penalties",
    "not including extra time and penalties",
    "extra time and penalties do not count",
)
INCLUDE_ET_PENALTIES = "Resolves including extra time and penalties."
GAMEWIN_SERIES = {
    "ticker": "KXBUNDESLIGAGAME",
    "title": "Bundesliga",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf",
    "contract_family": dict(KALSHI_GAMEWIN_SERIES["contract_family"]),
}


def _captured() -> dict[str, Any]:
    return json.loads(CAPTURED_BAYERN.read_text(encoding="utf-8"))


def _mb_event() -> dict[str, Any]:
    return {
        "id": 28401,
        "name": "Bayern Munich vs Union Berlin",
        "start": KICKOFF.isoformat(),
        "competition-name": "Bundesliga",
    }


def _mb_match_odds() -> dict[str, Any]:
    return {
        "id": 28410,
        "name": "Match Odds",
        "runners": [
            {"id": 1, "name": "Bayern Munich", "prices": [{"side": "back", "odds": "1.20", "available-amount": "80"}]},
            {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "7.50", "available-amount": "80"}]},
            {"id": 3, "name": "Union Berlin", "prices": [{"side": "back", "odds": "15.00", "available-amount": "80"}]},
        ],
    }


def _captured_kalshi_side() -> PayloadSide:
    captured = _captured()["payload"]
    return PayloadSide(
        venue=VenueName.KALSHI,
        event=captured,
        markets=list(captured["markets"]),
        series=GAMEWIN_SERIES,
    )


@pytest.mark.parametrize("text", EXCLUSION_ONLY)
def test_exclusion_only_et_penalty_wording_stays_unknown(text: str) -> None:
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert scope is SettlementScope.UNKNOWN
    assert extra_time is None
    assert penalties is None
    assert resolve_kalshi_settlement_from_rule_fields(text, "") == (
        SettlementScope.UNKNOWN,
        None,
        None,
    )


def test_single_subject_extra_time_exclusion_remains_regulation() -> None:
    assert classify_settlement_wording("Resolves not including extra time.") == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )
    assert classify_settlement_wording("Does not include extra time.") == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )


def test_explicit_90_minute_plus_exclusion_is_regulation() -> None:
    scope, extra_time, penalties = classify_settlement_wording(CURRENT_PRIMARY)
    assert scope is SettlementScope.REGULATION_TIME
    assert extra_time is False
    assert penalties is False
    structure = classify_kalshi_rule_structure(CURRENT_PRIMARY)
    assert structure["looks_like_generic_contract_template"] is False
    assert resolve_kalshi_settlement_from_rule_fields(CURRENT_PRIMARY, "") == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )
    assert classify_settlement_wording(NINETY_MINUTE_EXCLUSION) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )


def test_complete_regulation_and_complete_include_et_fields_fail_closed() -> None:
    fingerprint = resolve_kalshi_settlement_from_rule_fields(
        CURRENT_PRIMARY, INCLUDE_ET_PENALTIES
    )
    assert fingerprint == (SettlementScope.UNKNOWN, None, None)
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_captured()["payload"],
            markets=[
                {**item, "rules_primary": CURRENT_PRIMARY, "rules_secondary": INCLUDE_ET_PENALTIES}
                for item in _captured()["payload"]["markets"]
            ],
            series=GAMEWIN_SERIES,
        ),
    )
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.execution_eligible is assessment.paper_mode_admitted


def test_no_matchbook_kalshi_cancel_fair_price_assumption_exists() -> None:
    assert MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_COMPATIBLE is False
    assert MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_ASSUMPTION_ID is None
    assert matchbook_kalshi_cancel_reschedule_fair_price_compatible() is False
    assert kalshi_text_has_cancel_reschedule_fair_price(CURRENT_SECONDARY) is True
    assert kalshi_text_has_cancel_reschedule_fair_price(CURRENT_PRIMARY) is False
    assert kalshi_text_has_cancel_reschedule_fair_price(CANCEL_RESCHEDULE_FAIR_PRICE) is True


def test_captured_fair_price_payload_is_not_approved_equivalent() -> None:
    captured = _captured()["payload"]
    assert "fair price" in captured["markets"][0]["rules_secondary"].casefold()
    assert "cancelled or rescheduled" in captured["markets"][0]["rules_secondary"].casefold()
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()]),
        _captured_kalshi_side(),
    )
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.reason == "paper_assumed_equivalent"
    assert assessment.settlement_complete is False
    assert assessment.execution_eligible is assessment.paper_mode_admitted
    assert assessment.paper_mode_admitted is True
    assert assessment.matcher_matched is True
    mb = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()])
    )
    kalshi = normalize_payload_side(_captured_kalshi_side())
    assert kalshi.settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
    assert kalshi.settlement.is_economically_complete() is False
    assert kalshi.settlement.abandonment_rule is None
    assert kalshi.settlement.postponement_rule is None
    assert mb.settlement.abandonment_rule is None
    assert mb.settlement.postponement_rule is None
    match = MarketMatcher().match(mb, kalshi)
    assert match.matched is True
    assert "paper_assumed_equivalent" in match.reasons
    assert catalogue_allows_solver(mb, kalshi) is True
    assert scan_eligible_pair(mb, kalshi, match) is True
    admission = assess_catalogue_admission(mb, kalshi)
    assert admission.allowed is True
    assert admission.live_execution_eligible is True
    assert admission.catalogue_shared_by == ("hot", "universe")


def test_captured_payload_still_assembles_home_draw_away() -> None:
    captured = _captured()["payload"]
    event = KalshiNormalizer().normalize_event(captured)
    markets = KalshiNormalizer().assemble_canonical_markets(
        event, list(captured["markets"]), series=GAMEWIN_SERIES, event_payload=captured
    )
    assert len(markets) == 1
    market = markets[0]
    assert market.family is MarketFamily.MATCH_RESULT
    assert market.period is FootballPeriod.FULL_TIME
    assert {runner.outcome for runner in market.runners} == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    }
    assert market.settlement.is_economically_complete() is False
    assert market.settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
    assert kalshi_documented_result_scope_fingerprint(captured["markets"][0]) is None


def test_explicit_90_minute_without_fair_price_sibling_remains_approved() -> None:
    markets = [
        {**item, "rules_primary": CURRENT_PRIMARY, "rules_secondary": ""}
        for item in _captured()["payload"]["markets"]
    ]
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_captured()["payload"],
            markets=markets,
            series=GAMEWIN_SERIES,
        ),
    )
    assert assessment.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.reason == "approved_equivalent"
    assert assessment.settlement_complete is True
    assert assessment.execution_eligible is assessment.paper_mode_admitted


def test_generic_gamewin_is_paper_assumed_not_approved() -> None:
    assessment = classify_payload_pair(
        _mb([_mb_1x2()]),
        _kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES),
    )
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.reason == "paper_assumed_equivalent"
    assert assessment.matcher_admits_unknown_1x2 is True
    assert assessment.execution_eligible is assessment.paper_mode_admitted
    left = normalize_payload_side(_mb([_mb_1x2()]))
    right = normalize_payload_side(
        _kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES)
    )
    assert catalogue_allows_solver(left, right) is True
    assert scan_eligible_pair(left, right, MarketMatcher().match(left, right)) is True


def test_execution_remains_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


class _BayernMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_mb_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_mb_match_odds()]}


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class _BayernKalshi:
    def __init__(self) -> None:
        self.get_market_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_captured()["payload"]]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(ticker)
        raise AssertionError("material cancel/fair-price nested wording must not fetch Get Market")

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.90", "100.00"]],
                "no_dollars": [["0.09", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return {**GAMEWIN_SERIES, "ticker": series_ticker}


@pytest.mark.asyncio
async def test_collector_captured_fair_price_is_not_inventory_equivalent() -> None:
    repository = SqliteMarketIntelligenceRepository()
    kalshi = _BayernKalshi()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_BayernMatchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=[
                *matchbook_polymarket_costs(),
                kalshi_cost_from_series(GAMEWIN_SERIES, captured_at=datetime.now(UTC)),
            ],
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
            ],
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        )
        assert kalshi.get_market_calls == []
        fixture = report.discovered_fixtures[0]
        assert fixture.matchbook_matched is True
        assert fixture.kalshi_matched is True
        assert fixture.matched_equivalent_count == 1
        rows = report.fixture_markets[fixture.canonical_event_id]
        assert any(
            row.comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
            and row.family == "match_result"
            for row in rows
        )
        assert not any(
            row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
            for row in rows
        )
        coverage = report.scan_diagnostics["matching_coverage"]
        assert coverage["equivalent_markets"] == 1
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        repository.close()
