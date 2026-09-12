from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from pydantic import ValidationError
import pytest

import sports_hedge.research.value.engine as value_engine_module
from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)
from sports_hedge.fees.effective import apply_venue_costs
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
from sports_hedge.research.value.quotes import quote_age_seconds


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


def _cost(
    venue: VenueName,
    source_market_id: str,
    quoted_at: datetime,
    *,
    rate: str | None = "0.02",
    fee_basis: FeeBasis = FeeBasis.PROFIT_COMMISSION,
    known: bool = True,
    action: MarketAction = MarketAction.BACK,
    order_role: OrderRole = OrderRole.TAKER,
    fee_scope: FeeScope = FeeScope.PER_QUOTE,
    fixed_amount: str | None = None,
    snapshot_id: str | None = None,
    captured_at: datetime | None = None,
    effective_from: datetime | None = None,
) -> VenueCostSnapshot:
    captured = quoted_at if captured_at is None else captured_at
    effective = quoted_at if effective_from is None else effective_from
    if not known:
        return VenueCostSnapshot(
            venue=venue,
            action=action,
            fee_basis=FeeBasis.UNKNOWN,
            known_status=CostKnownStatus.UNKNOWN,
            captured_at=captured,
            source="test_unknown",
            source_market_id=source_market_id,
            order_role=order_role,
            fee_scope=fee_scope,
            snapshot_id=snapshot_id,
            effective_from=effective,
        )
    return VenueCostSnapshot(
        venue=venue,
        action=action,
        fee_basis=fee_basis,
        known_status=CostKnownStatus.KNOWN,
        captured_at=captured,
        source="test_paper_assumption",
        source_market_id=source_market_id,
        order_role=order_role,
        fee_scope=fee_scope,
        account_or_fee_tier="standard",
        rate=None if rate is None else Decimal(rate),
        fixed_amount=None if fixed_amount is None else Decimal(fixed_amount),
        formula_name="unregistered" if fee_basis is FeeBasis.FORMULA else None,
        currency="GBP",
        effective_from=effective,
        snapshot_id=snapshot_id,
        detail="Deterministic paper cost snapshot",
    )


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
    fee_basis: FeeBasis = FeeBasis.PROFIT_COMMISSION,
    action: MarketAction = MarketAction.BACK,
    order_role: OrderRole = OrderRole.TAKER,
    fee_scope: FeeScope = FeeScope.PER_QUOTE,
    fixed_amount: str | None = None,
    snapshot_id: str | None = None,
    captured_at: datetime | None = None,
    effective_from: datetime | None = None,
) -> VenueQuote:
    market_id = source_market_id or f"{venue}-corners-over"
    quoted_at = AS_OF - timedelta(seconds=age_seconds)
    known = commission is not None or fee_basis in {FeeBasis.NONE_CONFIRMED, FeeBasis.FIXED, FeeBasis.FORMULA}
    if commission is None and fee_basis is FeeBasis.PROFIT_COMMISSION:
        known = False
    rate: str | None
    if fee_basis is FeeBasis.NONE_CONFIRMED:
        rate = None
        known = True
    elif fee_basis is FeeBasis.FORMULA:
        rate = None
        known = True
    elif fee_basis is FeeBasis.FIXED:
        rate = None
        known = True
    else:
        rate = commission
    return VenueQuote(
        venue=venue,
        source_market_id=market_id,
        action=action,
        displayed_decimal_odds=Decimal(odds),
        quoted_at=quoted_at,
        settlement=_settlement(extra_time=extra_time, complete=complete_settlement),
        market_family=MarketFamily.CORNERS,
        period=FootballPeriod.FULL_TIME,
        line=Decimal("10.5"),
        available_depth=None if depth is None else Decimal(depth),
        cost=_cost(
            venue,
            market_id,
            quoted_at,
            rate=rate,
            fee_basis=fee_basis if known else FeeBasis.UNKNOWN,
            known=known,
            action=action,
            order_role=order_role,
            fee_scope=fee_scope,
            fixed_amount=fixed_amount,
            snapshot_id=snapshot_id,
            captured_at=captured_at,
            effective_from=effective_from,
        ),
    )


def test_package_is_paper_research_only_and_isolated_from_arbitrage_solver() -> None:
    assert PAPER_RESEARCH_ONLY is True
    source = inspect.getsource(value_engine_module)
    assert "sports_hedge.arbitrage" not in source
    assert "place_order" not in source
    assert "commission_rate" not in source


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
    assert result.fee_basis == FeeBasis.PROFIT_COMMISSION.value
    assert result.fee_scope == FeeScope.PER_QUOTE.value
    assert result.cost_known_status == CostKnownStatus.KNOWN.value
    assert result.cost_source == "test_paper_assumption"
    assert result.cost_currency == "GBP"
    assert result.cost_captured_at is not None
    assert result.cost_effective_from is not None
    assert result.order_role == OrderRole.TAKER.value
    assert result.action == MarketAction.BACK.value
    assert result.evaluation_kind == "directional_expected_value"
    assert result.claims_guaranteed_settlement_profit is False
    assert result.paper_research_only is True


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
    assert result.rejection_reason == "unknown_costs"


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


def test_fee_basis_not_coerced_to_profit_commission() -> None:
    matchbook = _quote(VenueName.MATCHBOOK, odds="2.20", commission="0.05")
    smarkets = _quote(
        VenueName.SMARKETS,
        odds="2.20",
        commission=None,
        fee_basis=FeeBasis.NONE_CONFIRMED,
    )
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [matchbook, smarkets],
        as_of=AS_OF,
    )

    matchbook_net = apply_venue_costs(
        matchbook.cost,
        gross_decimal_odds=matchbook.displayed_decimal_odds,
    )
    smarkets_net = apply_venue_costs(
        smarkets.cost,
        gross_decimal_odds=smarkets.displayed_decimal_odds,
    )
    assert smarkets_net.net_decimal_equivalent > matchbook_net.net_decimal_equivalent
    assert result.best_venue is VenueName.SMARKETS
    assert result.fee_basis == FeeBasis.NONE_CONFIRMED.value
    assert result.reference_venue is VenueName.MATCHBOOK


def test_same_headline_payout_fee_loses_to_profit_commission() -> None:
    payout = _quote(
        VenueName.MATCHBOOK,
        odds="2.10",
        commission="0.05",
        fee_basis=FeeBasis.PAYOUT,
    )
    profit = _quote(
        VenueName.SMARKETS,
        odds="2.10",
        commission="0.05",
        fee_basis=FeeBasis.PROFIT_COMMISSION,
    )
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [payout, profit],
        as_of=AS_OF,
    )

    assert result.best_venue is VenueName.SMARKETS
    assert result.fee_basis == FeeBasis.PROFIT_COMMISSION.value
    assert result.best_net_price == apply_venue_costs(
        profit.cost,
        gross_decimal_odds=profit.displayed_decimal_odds,
    ).net_decimal_equivalent


def test_unsupported_formula_basis_fails_closed() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(fee_basis=FeeBasis.FORMULA, commission=None)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.MISSING_COSTS
    assert result.rejection_reason == "unsupported_fee_basis"


def test_lay_action_fails_closed_instead_of_back_haircut() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(action=MarketAction.LAY)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.MISSING_COSTS
    assert result.rejection_reason == "unsupported_action"


def test_stateful_fee_scopes_fail_closed_rather_than_approximating() -> None:
    market_net = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(fee_scope=FeeScope.MARKET_NET_PNL)],
        as_of=AS_OF,
    )
    period = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(fee_scope=FeeScope.ACCOUNT_PERIOD)],
        as_of=AS_OF,
    )
    netted = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(fee_scope=FeeScope.NETTED_COMMISSION)],
        as_of=AS_OF,
    )

    assert market_net.status is ValueStatus.MISSING_COSTS
    assert market_net.rejection_reason == "unsupported_fee_scope"
    assert market_net.fee_scope == FeeScope.MARKET_NET_PNL.value
    assert period.rejection_reason == "unsupported_fee_scope"
    assert netted.rejection_reason == "unsupported_fee_scope"


def test_unknown_order_role_fails_closed() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(order_role=OrderRole.UNKNOWN)],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.MISSING_COSTS
    assert result.rejection_reason == "unknown_order_role"


def test_value_is_not_an_arbitrage_claim() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote()],
        as_of=AS_OF,
    )

    assert result.status is ValueStatus.VALUE
    assert result.expected_profit_per_unit is not None
    assert result.expected_profit_per_unit > 0
    assert result.claims_guaranteed_settlement_profit is False
    assert result.evaluation_kind == "directional_expected_value"
    assert not hasattr(result, "is_arbitrage")
    assert not hasattr(result, "guaranteed_profit")


def test_future_quote_fails_closed_instead_of_ranking_fresh() -> None:
    future = _quote(age_seconds=-15)
    with pytest.raises(ValueError, match="future_quote"):
        quote_age_seconds(future, AS_OF)
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [future],
        as_of=AS_OF,
    )
    assert result.status is ValueStatus.STALE_QUOTE
    assert result.rejection_reason == "future_quote"


def test_naive_and_non_utc_timestamps_are_rejected() -> None:
    naive = datetime(2026, 9, 11, 21, 0)
    plus_one = timezone(timedelta(hours=1))
    payload = _quote().model_dump()
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        VenueQuote.model_validate({**payload, "quoted_at": naive})
    with pytest.raises(ValidationError, match="must be UTC"):
        VenueQuote.model_validate(
            {**payload, "quoted_at": datetime(2026, 9, 11, 21, 0, tzinfo=plus_one)}
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        ScenarioValueEngine().evaluate(_proposition(), _evidence(), [_quote()], as_of=naive)


def test_future_effective_fee_snapshot_fails_closed() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(effective_from=AS_OF + timedelta(minutes=5))],
        as_of=AS_OF,
    )
    assert result.status is ValueStatus.MISSING_COSTS
    assert result.rejection_reason == "future_effective_cost"
    assert result.cost_effective_from == AS_OF + timedelta(minutes=5)


def test_future_captured_fee_snapshot_fails_closed() -> None:
    result = ScenarioValueEngine().evaluate(
        _proposition(),
        _evidence(),
        [_quote(captured_at=AS_OF + timedelta(seconds=30))],
        as_of=AS_OF,
    )
    assert result.status is ValueStatus.MISSING_COSTS
    assert result.rejection_reason == "future_captured_cost"
    assert result.cost_captured_at == AS_OF + timedelta(seconds=30)


