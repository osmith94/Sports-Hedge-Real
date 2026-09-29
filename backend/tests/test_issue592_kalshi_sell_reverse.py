"""Issue #592: Kalshi SELL reverse books and paper package-close safety.

Fixture/demo order books. PAPER only. No live venue order placement.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.position_management.quotes import (
    LatestObservationCatalog,
    reverse_quotes_for_position,
)
from sports_hedge.paper.trades import PaperLegFillKind
from sports_hedge.paper.unwind import PaperUnwindEngine
from sports_hedge.paper.unwind.adapter import _aggregate_same_market_close_legs
from sports_hedge.paper.unwind.models import (
    OpenPaperFillShare,
    OpenPaperLeg,
    OpenPaperPosition,
    UnwindEvaluationRequest,
    UnwindPolicy,
    UnwindRecommendation,
)
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_paper_scan_pipeline import (
    OBSERVED,
    kalshi_btts_observation,
    kalshi_btts_payloads,
    matchbook_payloads,
)


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
FX = [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="test", captured_at=NOW)]
FEE = {"fee_type": "quadratic", "fee_multiplier": "1"}


def _matchbook_observation(*, quote_age_ms: int = 80) -> VenueMarketObservation:
    event, market = matchbook_payloads()
    return MatchbookObservationBuilder().build(
        event, market, observed_at=OBSERVED, quote_age_ms=quote_age_ms, quote_age_basis="retrieval"
    )


def _kalshi_observation(
    books: dict[str, dict] | None = None,
    *,
    quote_age_ms: int = 80,
    fee_snapshot: dict | None = FEE,
) -> VenueMarketObservation:
    event, market, default_books, series = kalshi_btts_payloads()
    return KalshiObservationBuilder().build(
        event,
        market,
        books if books is not None else default_books,
        series=series,
        observed_at=OBSERVED,
        quote_age_ms=quote_age_ms,
        quote_age_basis="retrieval",
        fee_snapshot=fee_snapshot,
    )


def _leg_from_book(
    observation: VenueMarketObservation,
    outcome: CanonicalOutcome,
    *,
    action: MarketAction,
    size: str,
    fill_id: str,
) -> OpenPaperLeg:
    book = observation.book_for(outcome)
    assert book is not None and book.best_back is not None
    return OpenPaperLeg(
        venue=observation.venue,
        source_event_id=str(observation.market.event.source_event_id),
        source_market_id=observation.market.source_market_id,
        source_runner_id=book.source_runner_id,
        canonical_market_id="canon-btts",
        canonical_outcome=outcome.value,
        canonical_state=outcome.value,
        opening_action=action,
        filled_price=book.best_back.decimal_odds,
        filled_size=Decimal(size),
        native_currency=observation.native_currency,
        settlement_fingerprint_key=observation.market.settlement.deterministic_key(),
        fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
        fill_id=fill_id,
        opening_fills=[
            OpenPaperFillShare(
                fill_id=fill_id,
                filled_size=Decimal(size),
                filled_price=book.best_back.decimal_odds,
            )
        ],
    )


def _position(observations: list[VenueMarketObservation], legs: list[OpenPaperLeg]) -> OpenPaperPosition:
    fingerprint = observations[0].market.settlement.deterministic_key()
    return OpenPaperPosition(
        trade_id="ptrade-592",
        opportunity_id="opp-592",
        canonical_event_id="evt-592",
        canonical_market_id="canon-btts",
        settlement_fingerprint_key=fingerprint,
        solver_model="simple_complete_set",
        hold_pnl_gbp=Decimal("4"),
        capital_locked_native={"GBP": Decimal("10"), "USD": Decimal("10")},
        legs=legs,
    )


def _quotes(position: OpenPaperPosition, observations: list[VenueMarketObservation]):
    return reverse_quotes_for_position(
        position,
        observations,
        cost_resolver=VenueCostResolver(),
        evaluated_at=NOW,
    )


def _evaluate(position: OpenPaperPosition, observations: list[VenueMarketObservation], *, max_age: int = 2000):
    return PaperUnwindEngine().evaluate(
        UnwindEvaluationRequest(
            position=position,
            quotes=_quotes(position, observations),
            fx=FX,
            policy=UnwindPolicy(max_quote_age_ms=max_age, max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100),
            evaluated_at=NOW,
        )
    )


def test_kalshi_yes_buy_from_no_bid_and_sell_from_yes_bid() -> None:
    observation = _kalshi_observation()
    yes_book = observation.book_for(CanonicalOutcome.YES)
    assert yes_book is not None
    assert yes_book.best_back is not None
    assert yes_book.best_back.decimal_odds == Decimal("1") / Decimal("0.30")
    assert yes_book.best_back.available_stake == Decimal("0.30") * Decimal("500.00")
    assert yes_book.best_lay is not None
    assert yes_book.best_lay.decimal_odds == Decimal("1") / Decimal("0.20")
    assert yes_book.best_lay.available_stake == Decimal("0.20") * Decimal("500.00")
    assert yes_book.raw_book["sell_book"] == "yes_bid"


def test_kalshi_no_buy_from_yes_bid_and_sell_from_no_bid() -> None:
    observation = _kalshi_observation()
    no_book = observation.book_for(CanonicalOutcome.NO)
    assert no_book is not None
    assert no_book.best_back is not None
    assert no_book.best_back.decimal_odds == Decimal("1") / Decimal("0.80")
    assert no_book.best_back.available_stake == Decimal("0.80") * Decimal("500.00")
    assert no_book.best_lay is not None
    assert no_book.best_lay.decimal_odds == Decimal("1") / Decimal("0.70")
    assert no_book.best_lay.available_stake == Decimal("0.70") * Decimal("500.00")
    assert no_book.raw_book["sell_book"] == "no_bid"


def test_active_shaped_matchbook_kalshi_fresh_reverse_yields_validated_exit() -> None:
    matchbook = _matchbook_observation()
    kalshi = _kalshi_observation()
    catalog = LatestObservationCatalog()
    catalog.remember([matchbook, kalshi])
    position = _position(
        [matchbook, kalshi],
        [
            _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
            _leg_from_book(kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-1"),
        ],
    )
    quotes = reverse_quotes_for_position(
        position,
        catalog.observations(),
        cost_resolver=VenueCostResolver(),
        evaluated_at=NOW,
    )
    assert len(quotes) == 2
    by_venue = {quote.venue: quote for quote in quotes}
    assert by_venue[VenueName.KALSHI].levels
    assert by_venue[VenueName.MATCHBOOK].levels
    decision = PaperUnwindEngine().evaluate(
        UnwindEvaluationRequest(
            position=position,
            quotes=quotes,
            fx=FX,
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100),
            evaluated_at=NOW,
        )
    )
    assert decision.close_plan.fully_executable is True
    assert decision.validated_exit_pnl_gbp is not None
    assert decision.close_blocker is None
    assert "missing_reverse_quote" not in decision.close_plan.rejection_reasons


def test_missing_kalshi_same_side_bid_fails_closed_without_synthesizing() -> None:
    event, market, _books, series = kalshi_btts_payloads()
    books = {
        market["ticker"]: {
            "orderbook_fp": {
                "yes_dollars": [],
                "no_dollars": [["0.70", "500.00"]],
            }
        }
    }
    matchbook = _matchbook_observation()
    kalshi = KalshiObservationBuilder().build(
        event,
        market,
        books,
        series=series,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot=FEE,
    )
    yes_book = kalshi.book_for(CanonicalOutcome.YES)
    assert yes_book is not None
    assert yes_book.best_back is not None
    assert yes_book.lay_levels == []
    no_book = kalshi.book_for(CanonicalOutcome.NO)
    assert no_book is not None
    position = _position(
        [matchbook, kalshi],
        [
            _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
            OpenPaperLeg(
                venue=VenueName.KALSHI,
                source_event_id=str(kalshi.market.event.source_event_id),
                source_market_id=kalshi.market.source_market_id,
                source_runner_id=yes_book.source_runner_id,
                canonical_market_id="canon-btts",
                canonical_outcome=CanonicalOutcome.YES.value,
                canonical_state=CanonicalOutcome.YES.value,
                opening_action=MarketAction.BUY,
                filled_price=yes_book.best_back.decimal_odds,
                filled_size=Decimal("10"),
                native_currency="USD",
                settlement_fingerprint_key=kalshi.market.settlement.deterministic_key(),
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
                fill_id="kx-1",
            ),
        ],
    )
    decision = _evaluate(position, [matchbook, kalshi])
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert decision.close_plan.fully_executable is False
    assert decision.validated_exit_pnl_gbp is None
    assert decision.conditionally_releasable_by_venue_currency == {}
    reasons = decision.close_plan.rejection_reasons
    assert "missing_reverse_quote" in reasons or "insufficient_reverse_depth" in reasons
    assert no_book.lay_levels  # NO same-side bids still present; YES was not invented


def test_insufficient_reverse_depth_on_either_venue_blocks_entire_package() -> None:
    matchbook = _matchbook_observation()
    event, market, _books, series = kalshi_btts_payloads()
    thin = {
        market["ticker"]: {
            "orderbook_fp": {
                "yes_dollars": [["0.20", "1.00"]],
                "no_dollars": [["0.70", "1.00"]],
            }
        }
    }
    kalshi = KalshiObservationBuilder().build(
        event,
        market,
        thin,
        series=series,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot=FEE,
    )
    position = _position(
        [matchbook, kalshi],
        [
            _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
            _leg_from_book(kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-1"),
        ],
    )
    decision = _evaluate(position, [matchbook, kalshi])
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "insufficient_reverse_depth" in decision.close_plan.rejection_reasons
    assert decision.close_plan.fully_executable is False
    assert decision.validated_exit_pnl_gbp is None
    assert decision.conditionally_releasable_by_venue_currency == {}


def test_stale_reverse_quote_on_either_venue_blocks_entire_package() -> None:
    matchbook = _matchbook_observation(quote_age_ms=50)
    kalshi = _kalshi_observation(quote_age_ms=5000)
    position = _position(
        [matchbook, kalshi],
        [
            _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
            _leg_from_book(kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-1"),
        ],
    )
    decision = _evaluate(position, [matchbook, kalshi], max_age=2000)
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "stale_quote" in decision.close_plan.rejection_reasons
    assert decision.close_plan.fully_executable is False
    assert decision.validated_exit_pnl_gbp is None

    stale_matchbook = _matchbook_observation(quote_age_ms=9000)
    fresh_kalshi = _kalshi_observation(quote_age_ms=50)
    other = _position(
        [stale_matchbook, fresh_kalshi],
        [
            _leg_from_book(stale_matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-2"),
            _leg_from_book(fresh_kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-2"),
        ],
    )
    blocked = _evaluate(other, [stale_matchbook, fresh_kalshi], max_age=2000)
    assert blocked.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "stale_quote" in blocked.close_plan.rejection_reasons


def test_multi_tranche_same_runner_consumes_shared_kalshi_sell_book_once() -> None:
    matchbook = _matchbook_observation()
    event, market, _books, series = kalshi_btts_payloads()
    shared = {
        market["ticker"]: {
            "orderbook_fp": {
                "yes_dollars": [["0.20", "500.00"]],
                "no_dollars": [["0.50", "18.00"]],
            }
        }
    }
    kalshi = KalshiObservationBuilder().build(
        event,
        market,
        shared,
        series=series,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot=FEE,
    )
    first = _leg_from_book(kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-open")
    second = first.model_copy(
        update={
            "fill_id": "kx-topup",
            "opening_fills": [
                OpenPaperFillShare(
                    fill_id="kx-topup",
                    filled_size=first.filled_size,
                    filled_price=first.filled_price,
                )
            ],
        }
    )
    independent = PaperUnwindEngine().evaluate(
        UnwindEvaluationRequest(
            position=_position([matchbook, kalshi], [_leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"), first]),
            quotes=_quotes(
                _position([matchbook, kalshi], [_leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"), first]),
                [matchbook, kalshi],
            ),
            fx=FX,
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100),
            evaluated_at=NOW,
        )
    )
    assert independent.close_plan.fully_executable is True
    aggregated = _aggregate_same_market_close_legs([first, second])
    assert len(aggregated) == 1
    assert aggregated[0].filled_size == first.filled_size * 2
    package = _position(
        [matchbook, kalshi],
        [
            _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
            aggregated[0],
        ],
    )
    decision = _evaluate(package, [matchbook, kalshi])
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "insufficient_reverse_depth" in decision.close_plan.rejection_reasons
    assert decision.close_plan.fully_executable is False
    assert decision.conditionally_releasable_by_venue_currency == {}


def test_settlement_identity_mismatch_is_not_reported_as_missing_reverse_quote() -> None:
    matchbook = _matchbook_observation()
    kalshi = _kalshi_observation()
    mismatched = "ft:other:mismatch:v1"
    legs = [
        _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
        _leg_from_book(kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-1"),
    ]
    legs = [leg.model_copy(update={"settlement_fingerprint_key": mismatched}) for leg in legs]
    position = OpenPaperPosition(
        trade_id="ptrade-592-mismatch",
        opportunity_id="opp-592",
        canonical_event_id="evt-592",
        canonical_market_id="canon-btts",
        settlement_fingerprint_key=mismatched,
        solver_model="simple_complete_set",
        hold_pnl_gbp=Decimal("4"),
        capital_locked_native={"GBP": Decimal("10"), "USD": Decimal("10")},
        legs=legs,
    )
    decision = _evaluate(position, [matchbook, kalshi])
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "settlement_identity_mismatch" in decision.close_plan.rejection_reasons
    assert "missing_reverse_quote" not in decision.close_plan.rejection_reasons


def test_unknown_kalshi_closing_fee_is_distinguishable() -> None:
    matchbook = _matchbook_observation()
    kalshi = _kalshi_observation(fee_snapshot=None)
    position = _position(
        [matchbook, kalshi],
        [
            _leg_from_book(matchbook, CanonicalOutcome.YES, action=MarketAction.BACK, size="10", fill_id="mb-1"),
            _leg_from_book(kalshi, CanonicalOutcome.NO, action=MarketAction.BUY, size="10", fill_id="kx-1"),
        ],
    )
    decision = _evaluate(position, [matchbook, kalshi])
    assert decision.recommendation is UnwindRecommendation.UNWIND_NOT_SAFE
    assert "unknown_costs" in decision.close_plan.rejection_reasons
    assert "missing_reverse_quote" not in decision.close_plan.rejection_reasons


def test_active_exact_id_path_does_not_rediscover_or_remap() -> None:
    source = inspect.getsource(reverse_quotes_for_position)
    builder = inspect.getsource(KalshiObservationBuilder.build_from_canonical)
    assert "self.normalizer.assemble_canonical_markets" not in builder
    assert "list_events" not in source
    assert "fuzzy" not in source.casefold()


def test_paper_only_boundary_unchanged_by_kalshi_sell_books() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    for venue_cls in (MatchbookClient, PolymarketClient, KalshiClient):
        assert not hasattr(venue_cls, "place_order")
        assert not hasattr(venue_cls, "cancel_order")
    observation_src = Path(
        inspect.getsourcefile(KalshiObservationBuilder) or ""
    ).read_text(encoding="utf-8")
    quotes_src = inspect.getsource(reverse_quotes_for_position)
    for banned in ("place_order", "cancel_order", "sign_order", "submit_order"):
        assert banned not in observation_src
        assert banned not in quotes_src
    assert kalshi_btts_observation().metadata["price_model"] == "kalshi_binary_complement_buy"
