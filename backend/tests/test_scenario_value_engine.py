from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sports_hedge.research.value.engine as value_engine_module
from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.research.value import PAPER_RESEARCH_ONLY, ScenarioValueEngine
from sports_hedge.research.value.contracts import (
    CanonicalProposition,
    DataQuality,
    ScenarioEvidence,
    ValueEnginePolicy,
    ValueStatus,
    VenueQuote,
)
from sports_hedge.research.value.economics import expected_profit_per_unit, net_back_odds


AS_OF = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def _settlement(*, extra_time: bool = False, complete: bool = True) -> SettlementFingerprint:
    if not complete:
        return SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.UNKNOWN,
        )
    return SettlementFingerprint(
        scope=SettlementScope.INCLUDING_EXTRA_TIME
        if extra_time
        else SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        extra_time_included=extra_time,
        penalties_included=False,
        push_possible=False,
    )


def _proposition(**overrides: object) -> CanonicalProposition:
    payload: dict[str, object] = {
        "event_id": "football|premier league|arsenal|tottenham|2026-09-20T15:00:00+00:00",
        "team": "arsenal",
        "opponent": "tottenham",
        "scenario_id": "favourite_concedes_first",
        "metric": "corners",
        "response_window": "0-15",
        "market_family": MarketFamily.CORNERS,
        "period": FootballPeriod.FULL_TIME,
        "settlement": _settlement(),
        "outcome": "over",
        "line": Decimal("10.5"),
    }
    payload.update(overrides)
    return CanonicalProposition.model_validate(payload)


def _evidence(**overrides: object) -> ScenarioEvidence:
    payload: dict[str, object] = {
        "src": Decimal("1.40"),
        "sample_size": 48,
        "confidence": Decimal("0.82"),
        "stability": Decimal("0.75"),
        "data_quality": DataQuality.HIGH,
        "regime_relevance": Decimal("0.90"),
        "model_probability": Decimal("0.58"),
    }
    payload.update(overrides)
    return ScenarioEvidence.model_validate(payload)


def _quote(
    venue: VenueName = VenueName.MATCHBOOK,
    *,
    odds: str = "2.20",
    commission: str | None = "0.02",
    age_seconds: int = 5,
    depth: str | None = "50",
    extra_time: bool = False,
    complete_settlement: bool = True,
    source_market_id: str | None = None,
) -> VenueQuote:
    return VenueQuote(
        venue=venue,
        source_market_id=source_market_id or f"{venue}-corners-over",
        displayed_decimal_odds=Decimal(odds),
        quoted_at=AS_OF - timedelta(seconds=age_seconds),
        settlement=_settlement(extra_time=extra_time, complete=complete_settlement),
        market_family=MarketFamily.CORNERS,
        period=FootballPeriod.FULL_TIME,
        line=Decimal("10.5"),
        available_depth=None if depth is None else Decimal(depth),
        commission_rate=None if commission is None else Decimal(commission),
    )


def test_package_is_paper_research_only_and_isolated_from_arbitrage_solver() -> None:
    assert PAPER_RESEARCH_ONLY is True
    source = inspect.getsource(value_engine_module)
    assert "sports_hedge.arbitrage" not in source
    assert "place_order" not in source


def test_positive_value_uses_matchbook_as_reference() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote()],
        as_of=AS_OF,
    )

    net = net_back_odds(Decimal("2.20"), Decimal("0.02"))
    assert result.status is ValueStatus.VALUE
    assert result.paper_research_only is True
    assert result.best_venue is VenueName.MATCHBOOK
    assert result.reference_venue is VenueName.MATCHBOOK
    assert result.best_price == Decimal("2.20")
    assert result.reference_price == Decimal("2.20")
    assert result.raw_market_implied_probability == Decimal("1") / Decimal("2.20")
    assert result.cost_adjusted_market_probability == Decimal("1") / net
    assert result.expected_profit_per_unit == expected_profit_per_unit(Decimal("0.58"), net)
    assert result.expected_roi == result.expected_profit_per_unit
    assert result.probability_edge_pp is not None and result.probability_edge_pp > 0
    assert result.value_signal_score is not None and result.value_signal_score > 0
    assert result.score_components is not None
    assert result.quote_age_seconds == Decimal("5")
    assert result.src == Decimal("1.40")


def test_no_value_when_model_probability_is_below_cost_adjusted_price() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(model_probability=Decimal("0.40")),
        [_quote(odds="2.05")],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.NO_VALUE
    assert result.rejection_reason == "no_positive_edge"
    assert result.expected_profit_per_unit is not None
    assert result.expected_profit_per_unit < 0
    assert result.score_components is not None


def test_same_model_probability_better_venue_price_wins() -> None:
    matchbook = _quote(VenueName.MATCHBOOK, odds="2.10")
    smarkets = _quote(VenueName.SMARKETS, odds="2.40")
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [matchbook, smarkets],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.VALUE
    assert result.best_venue is VenueName.SMARKETS
    assert result.best_price == Decimal("2.40")
    assert result.reference_venue is VenueName.MATCHBOOK
    assert result.reference_price == Decimal("2.10")

    matchbook_only = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [matchbook],
        as_of=AS_OF,
    )
    assert result.expected_profit_per_unit > matchbook_only.expected_profit_per_unit
    assert result.value_signal_score > matchbook_only.value_signal_score


def test_commission_can_remove_apparent_value() -> None:
    evidence = _evidence(model_probability=Decimal("0.505"))
    zero_fee = ScenarioValueEngine().evaluate(
        _proposition(),
        evidence,
        [_quote(odds="2.02", commission="0")],
        as_of=AS_OF,
    )
    with_fee = ScenarioValueEngine().evaluate(
        _proposition(),
        evidence,
        [_quote(odds="2.02", commission="0.05")],
        as_of=AS_OF,
    )

    assert zero_fee.status is ValueStatus.VALUE
    assert with_fee.status is ValueStatus.NO_VALUE
    assert with_fee.best_net_price < zero_fee.best_net_price


def test_stale_quote_is_rejected() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(age_seconds=120)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.STALE_QUOTE
    assert result.rejection_reason == "stale_quote"
    assert result.expected_profit_per_unit is None


def test_settlement_mismatch_is_rejected() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(extra_time=True)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.SEMANTICS_MISMATCH
    assert result.rejection_reason == "settlement_mismatch"


def test_incomplete_settlement_fails_closed() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(settlement=_settlement(complete=False)),
        _evidence(),
        [_quote(complete_settlement=False)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.SEMANTICS_MISMATCH
    assert result.rejection_reason == "incomplete_settlement"


def test_low_sample_and_confidence_are_insufficient_evidence() -> None:
    low_sample = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(sample_size=4, src=Decimal("3.50")),
        [_quote()],
        as_of=AS_OF,
    )
    low_confidence = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(confidence=Decimal("0.10"), src=Decimal("3.50")),
        [_quote()],
        as_of=AS_OF,
    )

    assert low_sample.status is ValueStatus.INSUFFICIENT_EVIDENCE
    assert low_sample.rejection_reason == "low_sample_size"
    assert low_confidence.status is ValueStatus.INSUFFICIENT_EVIDENCE
    assert low_confidence.rejection_reason == "low_confidence"
    assert low_sample.src == Decimal("3.50")


def test_wide_uncertainty_is_insufficient_evidence_rather_than_value() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(
            model_probability=Decimal("0.58"),
            model_probability_low=Decimal("0.20"),
            model_probability_high=Decimal("0.90"),
        ),
        [_quote()],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.INSUFFICIENT_EVIDENCE
    assert result.rejection_reason == "uncertainty_too_wide"
    assert result.conservative_model_probability == Decimal("0.20")
    assert result.expected_profit_per_unit is not None


def test_multi_venue_quote_selection_prefers_best_equivalent_net_odds() -> None:
    quotes = [
        _quote(VenueName.MATCHBOOK, odds="2.15", commission="0.02"),
        _quote(VenueName.SMARKETS, odds="2.18", commission="0.02"),
        _quote(VenueName.POLYMARKET, odds="2.50", extra_time=True),
        _quote(
            VenueName.POLYMARKET,
            odds="2.30",
            commission="0.00",
            source_market_id="poly-equivalent",
        ),
    ]
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        quotes,
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.VALUE
    assert result.best_venue is VenueName.POLYMARKET
    assert result.best_price == Decimal("2.30")
    assert result.reference_venue is VenueName.MATCHBOOK
    assert result.reference_price == Decimal("2.15")


def test_missing_costs_fail_closed_without_inventing_fees() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(commission=None)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.MISSING_COSTS
    assert result.rejection_reason == "missing_costs"


def test_insufficient_liquidity_is_rejected() -> None:
    policy = ValueEnginePolicy(min_available_depth=Decimal("25"))
    result = ScenarioValueEngine(policy).evaluate(
        _proposition(),
        _evidence(),
        [_quote(depth="5")],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.INSUFFICIENT_LIQUIDITY
    assert result.rejection_reason == "insufficient_liquidity"


def test_stale_matchbook_does_not_block_fresh_equivalent_venue() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [
            _quote(VenueName.MATCHBOOK, odds="2.50", age_seconds=180),
            _quote(VenueName.SMARKETS, odds="2.20", age_seconds=3),
        ],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.VALUE
    assert result.best_venue is VenueName.SMARKETS
    assert result.reference_venue is VenueName.MATCHBOOK
