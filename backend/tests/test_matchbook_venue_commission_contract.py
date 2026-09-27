"""Matchbook costs follow the venue/account commission, not the sport.

Sports own market semantics. The fee layer owns the Matchbook commission
schedule, the saved account override, and fail-closed behaviour when a
required cost is actually unknown.

Quotes in the paper scans are fixture/demo books shaped like captured
provider payloads. They are not owner-live prices. The game-winner identity
is the owner acceptance case: Miami Dolphins at Kansas City Chiefs,
Game Winner, full time.
"""

from __future__ import annotations

import ast
import copy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fx_test_helpers import fresh_usd_ecb_close
from test_fixture_inventory import _inventory, _market
from test_nfl_stage1b_paper_markets import (
    _clone_mb_spread,
    _clone_mb_total,
    _kalshi_game_event,
    _mb_indkc,
    _mb_market,
    _normalize_kalshi_spread,
    _normalize_kalshi_total,
    _pm_indkc,
)
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads

from sports_hedge.application.fixture_inventory import assemble_fixture_inventory
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees import resolver as fee_resolver
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, MarketAction, OrderRole
from sports_hedge.fees.effective import apply_venue_costs
from sports_hedge.fees.polymarket import resolve_polymarket_fee_metadata
from sports_hedge.fees.resolver import (
    MATCHBOOK_OVERRIDE_SOURCE,
    MATCHBOOK_PROVIDER_DEFAULT_COMMISSION,
    MATCHBOOK_REGISTRY_SOURCE,
    MATCHBOOK_STANDARD_FOOTBALL_COMMISSION,
    MATCHBOOK_VENUE_COMMISSION_CLASS,
    UnknownRequiredCostError,
    VenueCostResolver,
    VenueCostRule,
    phase1_seed_rules,
)
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.audit import build_paper_scan_record
from sports_hedge.persistence.matchbook_account_fee import SqliteMatchbookAccountFeeStore

AS_OF = OBSERVED + timedelta(hours=1)
SOCCER_FAMILIES = (
    MarketFamily.MATCH_RESULT,
    MarketFamily.BOTH_TEAMS_TO_SCORE,
    MarketFamily.TOTAL_GOALS,
    MarketFamily.CORRECT_SCORE,
)
NFL_FAMILIES = (
    MarketFamily.GAME_WINNER,
    MarketFamily.POINT_SPREAD,
    MarketFamily.TOTAL_POINTS,
)
FUTURE_FAMILY = "future_sport_match_winner"
KALSHI_FEE = {
    "fee_type": "quadratic_with_maker_fees",
    "fee_multiplier": "1",
    "fee_provenance": "series",
}
SPORT_BRANCH_TOKENS = (
    "nfl",
    "nba",
    "ncaab",
    "ncaaf",
    "soccer",
    "football",
    "basketball",
    "nfl_paper_market_families",
)


def test_seed_is_one_matchbook_venue_schedule() -> None:
    rules = phase1_seed_rules()
    assert len(rules) == 2
    assert {rule.venue for rule in rules} == {VenueName.MATCHBOOK}
    assert {rule.market_class for rule in rules} == {MATCHBOOK_VENUE_COMMISSION_CLASS}
    assert {rule.action for rule in rules} == {MarketAction.BACK, MarketAction.LAY}
    assert {rule.order_role for rule in rules} == {OrderRole.TAKER}
    assert {rule.fee_basis for rule in rules} == {FeeBasis.PROFIT_COMMISSION}
    assert {rule.rate for rule in rules} == {MATCHBOOK_PROVIDER_DEFAULT_COMMISSION}
    assert MATCHBOOK_STANDARD_FOOTBALL_COMMISSION == MATCHBOOK_PROVIDER_DEFAULT_COMMISSION
    assert all(rule.known_status is CostKnownStatus.KNOWN for rule in rules)
    assert VenueName.POLYMARKET not in {rule.venue for rule in rules}
    assert VenueName.KALSHI not in {rule.venue for rule in rules}


def test_fee_resolution_does_not_branch_on_sport_to_select_a_schedule() -> None:
    source_path = Path(fee_resolver.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "nfl_paper_market_families" not in source.casefold()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            segment = (ast.get_source_segment(source, node.test) or "").casefold()
            for token in SPORT_BRANCH_TOKENS:
                assert token not in segment
        if isinstance(node, ast.For):
            iterated = ast.get_source_segment(source, node.iter) or ""
            assert "MarketFamily" not in iterated
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in {"phase1_seed_rules", "_matchbook_commission_rule", "_select_rule"}:
            continue
        body = (ast.get_source_segment(source, node) or "").casefold()
        for token in SPORT_BRANCH_TOKENS:
            assert token not in body
        assert "marketfamily" not in body


def test_every_current_and_future_family_uses_the_same_matchbook_schedule() -> None:
    resolver = VenueCostResolver()
    reference = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.MATCH_RESULT,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    families: list[str | MarketFamily] = [
        *MarketFamily,
        FUTURE_FAMILY,
    ]
    for family in families:
        if family is MarketFamily.UNKNOWN:
            continue
        for action in (MarketAction.BACK, MarketAction.LAY):
            snapshot = resolver.resolve(
                venue=VenueName.MATCHBOOK,
                market_class=family,
                action=action,
                as_of=AS_OF,
                order_role=OrderRole.TAKER,
            )
            assert snapshot.rate == reference.rate == Decimal("0.02")
            assert snapshot.fee_basis is FeeBasis.PROFIT_COMMISSION
            assert snapshot.source == MATCHBOOK_REGISTRY_SOURCE
            assert snapshot.known_status is CostKnownStatus.KNOWN
            expected_class = family.value if isinstance(family, MarketFamily) else family
            assert snapshot.market_class == expected_class
            if action is MarketAction.BACK:
                assert (
                    apply_venue_costs(
                        snapshot, gross_decimal_odds=Decimal("2.20")
                    ).net_decimal_equivalent
                    == apply_venue_costs(
                        reference, gross_decimal_odds=Decimal("2.20")
                    ).net_decimal_equivalent
                )
    assert FUTURE_FAMILY not in {rule.market_class for rule in phase1_seed_rules()}


def test_soccer_matchbook_economics_stay_on_the_provider_default() -> None:
    resolver = VenueCostResolver()
    odds = Decimal("2.20")
    snapshots = [
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=family,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
        for family in SOCCER_FAMILIES
    ]
    nets = {
        apply_venue_costs(snapshot, gross_decimal_odds=odds).net_decimal_equivalent
        for snapshot in snapshots
    }
    assert len(nets) == 1
    assert snapshots[0].rate == Decimal("0.02")
    assert snapshots[0].source == MATCHBOOK_REGISTRY_SOURCE
    nfl = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.GAME_WINNER,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert nfl.rate == snapshots[0].rate
    assert nfl.fee_basis is snapshots[0].fee_basis
    assert nfl.source == snapshots[0].source


def test_explicit_market_class_exception_beats_the_venue_schedule() -> None:
    exception = VenueCostRule(
        venue=VenueName.MATCHBOOK,
        market_class="player_props",
        action=MarketAction.BACK,
        order_role=OrderRole.TAKER,
        fee_basis=FeeBasis.PROFIT_COMMISSION,
        known_status=CostKnownStatus.KNOWN,
        rate=Decimal("0.05"),
        currency="GBP",
        source="venue_cost_registry:authoritative_exception",
        effective_from=AS_OF - timedelta(days=1),
        catalog_version="test-exception",
        detail="Fixture exception. Not a seeded sport table.",
    )
    resolver = VenueCostResolver(phase1_seed_rules() + [exception])
    props = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.PLAYER_PROPS,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    winner = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.GAME_WINNER,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert props.rate == Decimal("0.05")
    assert props.source == "venue_cost_registry:authoritative_exception"
    assert winner.rate == Decimal("0.02")
    assert winner.source == MATCHBOOK_REGISTRY_SOURCE


def test_operator_override_wins_for_every_applicable_family() -> None:
    store = SqliteMatchbookAccountFeeStore()
    store.set_override(Decimal("0.035"))
    resolver = VenueCostResolver(matchbook_fee_store=store)
    families: list[str | MarketFamily] = [*NFL_FAMILIES, *SOCCER_FAMILIES, FUTURE_FAMILY]
    for family in families:
        snapshot = resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=family,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
        assert snapshot.rate == Decimal("0.035")
        assert snapshot.source == MATCHBOOK_OVERRIDE_SOURCE
        assert snapshot.account_or_fee_tier == "operator_account_override"


def test_missing_or_ambiguous_costs_still_fail_closed() -> None:
    resolver = VenueCostResolver()
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:matchbook"):
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=MarketFamily.UNKNOWN,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:matchbook"):
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class="  ",
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:smarkets"):
        resolver.resolve(
            venue=VenueName.SMARKETS,
            market_class=MarketFamily.GAME_WINNER,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:kalshi"):
        resolver.resolve(
            venue=VenueName.KALSHI,
            market_class=MarketFamily.GAME_WINNER,
            action=MarketAction.BUY,
            as_of=AS_OF,
        )
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:polymarket"):
        resolver.resolve(
            venue=VenueName.POLYMARKET,
            market_class=MarketFamily.GAME_WINNER,
            action=MarketAction.BUY,
            as_of=AS_OF,
        )
    poisoned = VenueCostResolver(
        phase1_seed_rules()
        + [
            VenueCostRule(
                venue=VenueName.POLYMARKET,
                market_class=MATCHBOOK_VENUE_COMMISSION_CLASS,
                action=MarketAction.BUY,
                order_role=OrderRole.TAKER,
                fee_basis=FeeBasis.NONE_CONFIRMED,
                known_status=CostKnownStatus.KNOWN,
                currency="USD",
                source="venue_cost_registry:must_not_apply",
                effective_from=AS_OF - timedelta(days=1),
                catalog_version="test",
                detail="A registry zero must not become Polymarket's fee.",
            )
        ]
    )
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:polymarket"):
        poisoned.resolve(
            venue=VenueName.POLYMARKET,
            market_class=FUTURE_FAMILY,
            action=MarketAction.BUY,
            as_of=AS_OF,
        )
    with pytest.raises(
        UnknownRequiredCostError, match="unknown_required_venue_cost:matchbook:game_winner"
    ):
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=MarketFamily.GAME_WINNER,
            action=MarketAction.BACK,
            as_of=AS_OF,
            order_role=OrderRole.MAKER,
        )


def test_inventory_nfl_game_winner_is_not_fee_missing() -> None:
    market = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.GAME_WINNER,
        source_id="mb-mia-kc-winner",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
    )
    rows = assemble_fixture_inventory(
        [_inventory(market, name="Game Winner")],
        [],
        cost_resolver=VenueCostResolver(),
    )
    facts = rows[0].matchbook
    assert facts is not None
    assert facts.fee_status == "known"
    assert facts.fee_label == "2.00% net-profit commission"
    assert facts.fee_source == MATCHBOOK_REGISTRY_SOURCE
    assert facts.fee_rate == Decimal("0.02")


def _scan_service(
    *,
    fee_store: SqliteMatchbookAccountFeeStore | None = None,
) -> tuple[PaperScanService, SqliteMarketIntelligenceRepository]:
    fx = FxRateService(SqliteFxRateRepository())
    # Scanner FX age is measured from wall-clock now, not the fixture kickoff.
    fx.persist_ecb_closes([fresh_usd_ecb_close(Decimal("0.75000000"))])
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(
        MarketIntelligenceService(repository),
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        fx_service=fx,
        cost_resolver=VenueCostResolver(matchbook_fee_store=fee_store),
    )
    return service, repository


def _relabel_mia_kc(value: object) -> object:
    if isinstance(value, str):
        return (
            value.replace("Indianapolis Colts", "Miami Dolphins")
            .replace("Indianapolis", "Miami")
            .replace("IND Colts", "MIA Dolphins")
            .replace("Colts", "Dolphins")
        )
    if isinstance(value, list):
        return [_relabel_mia_kc(item) for item in value]
    if isinstance(value, dict):
        return {key: _relabel_mia_kc(item) for key, item in value.items()}
    return value


def _priced_matchbook_market(market: dict, odds: str) -> dict:
    priced = copy.deepcopy(market)
    for runner in priced["runners"]:
        runner["prices"] = [{"side": "back", "odds": odds, "available-amount": "250"}]
    return priced


def _matchbook_observation(event: dict, market: dict):
    return MatchbookObservationBuilder().build(
        event,
        market,
        observed_at=OBSERVED,
        quote_age_ms=120,
    )


def _kalshi_game_observation(event: dict, *, no_bid: str):
    from sports_hedge.normalization.venues import KalshiNormalizer

    normalized_event = KalshiNormalizer().normalize_event(event)
    markets = KalshiNormalizer().assemble_canonical_markets(
        normalized_event, event["markets"], event_payload=event
    )
    market = markets[0]
    tickers = [runner.source_runner_id.rsplit(":", 1)[0] for runner in market.runners]
    books = {
        ticker: {
            "orderbook_fp": {
                "yes_dollars": [["0.10", "400.00"]],
                "no_dollars": [[no_bid, "400.00"]],
            }
        }
        for ticker in tickers
    }
    return KalshiObservationBuilder().build_from_canonical(
        market,
        books,
        observed_at=OBSERVED,
        quote_age_ms=90,
        fee_snapshot=KALSHI_FEE,
    )


def _polymarket_game_observation(payload: dict, *, ask: str):
    from sports_hedge.normalization.venues import PolymarketNormalizer

    event = PolymarketNormalizer().normalize_event(payload)
    market_payload = next(
        item for item in payload["markets"] if item["sportsMarketType"] == "moneyline"
    )
    market_payload = {
        **market_payload,
        "clobTokenIds": [
            "101mon000111222333444555666777888999000111222333",
            "202mon000111222333444555666777888999000111222333",
        ],
        "feesEnabled": False,
    }
    market = PolymarketNormalizer().normalize_market(event, market_payload)
    books = {
        runner.source_runner_id: {
            "asset_id": runner.source_runner_id,
            "bids": [{"price": "0.10", "size": "200"}],
            "asks": [{"price": ask, "size": "200"}],
        }
        for runner in market.runners
    }
    return PolymarketObservationBuilder().build(
        {"id": event.source_event_id, "title": "Dolphins at Chiefs"},
        market_payload,
        books,
        canonical=market,
        observed_at=OBSERVED,
        quote_age_ms=110,
        fee_snapshot=resolve_polymarket_fee_metadata(market_payload),
    )


def _mia_kc_books() -> tuple[dict, dict, dict]:
    matchbook_event = _relabel_mia_kc(_mb_indkc())
    assert isinstance(matchbook_event, dict)
    moneyline = _mb_market(matchbook_event, name="Moneyline")
    kalshi_event = _relabel_mia_kc(_kalshi_game_event())
    assert isinstance(kalshi_event, dict)
    polymarket_event = _relabel_mia_kc(_pm_indkc())
    assert isinstance(polymarket_event, dict)
    return matchbook_event, moneyline, kalshi_event | {"polymarket": polymarket_event}


def _assert_acceptance_identity(observation) -> None:
    assert observation.market.event.home_team == "kansas city chiefs"
    assert observation.market.event.away_team == "miami dolphins"
    assert observation.market.family is MarketFamily.GAME_WINNER
    assert observation.market.period.value == "full_time"


def _edges(service: PaperScanService, repository, decision):
    assert decision.market_match.matched is True
    assert "unknown_required_venue_cost" not in " ".join(decision.rejection_reasons)
    assert decision.depth_scan is not None
    record = build_paper_scan_record(decision, repository.list_snapshots())
    assert record.gross_edge is not None
    assert record.net_edge is not None
    return record


def test_mia_kc_game_winner_matchbook_kalshi_computes_economics() -> None:
    matchbook_event, moneyline, bundle = _mia_kc_books()
    kalshi_event = {key: value for key, value in bundle.items() if key != "polymarket"}
    matchbook = _matchbook_observation(matchbook_event, _priced_matchbook_market(moneyline, "4.50"))
    kalshi = _kalshi_game_observation(kalshi_event, no_bid="0.80")
    _assert_acceptance_identity(matchbook)
    _assert_acceptance_identity(kalshi)
    service, repository = _scan_service()
    try:
        decision = service.scan_pair(
            matchbook,
            kalshi,
            maximum_execution_risk=100,
            capital_limit_gbp=Decimal(1000),
        )
        record = _edges(service, repository, decision)
        matchbook_cost = next(
            item for item in decision.venue_costs if item.venue is VenueName.MATCHBOOK
        )
        assert matchbook_cost.rate == Decimal("0.02")
        assert matchbook_cost.fee_basis is FeeBasis.PROFIT_COMMISSION
        assert matchbook_cost.source == MATCHBOOK_REGISTRY_SOURCE
        assert matchbook_cost.market_class == "game_winner"
        assert record.gross_edge > 0
        assert record.net_edge > 0
        assert record.executable_stake_gbp is not None
        assert record.executable_stake_gbp > 0
        assert record.home_team == "kansas city chiefs"
        assert record.away_team == "miami dolphins"
    finally:
        repository.close()


def test_mia_kc_game_winner_matchbook_polymarket_computes_economics() -> None:
    matchbook_event, moneyline, bundle = _mia_kc_books()
    polymarket_event = bundle["polymarket"]
    matchbook = _matchbook_observation(matchbook_event, _priced_matchbook_market(moneyline, "4.50"))
    polymarket = _polymarket_game_observation(polymarket_event, ask="0.20")
    _assert_acceptance_identity(matchbook)
    _assert_acceptance_identity(polymarket)
    service, repository = _scan_service()
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            maximum_execution_risk=100,
            capital_limit_gbp=Decimal(1000),
        )
        record = _edges(service, repository, decision)
        costs = {item.venue: item for item in decision.venue_costs}
        assert costs[VenueName.MATCHBOOK].is_economically_known()
        assert costs[VenueName.POLYMARKET].is_economically_known()
        assert costs[VenueName.POLYMARKET].fee_basis is FeeBasis.NONE_CONFIRMED
        assert record.gross_edge is not None
        assert record.net_edge is not None
        assert record.executable_stake_gbp is not None
        assert record.executable_stake_gbp > 0
    finally:
        repository.close()


def test_below_break_even_mia_kc_shows_negative_economics() -> None:
    matchbook_event, moneyline, bundle = _mia_kc_books()
    kalshi_event = {key: value for key, value in bundle.items() if key != "polymarket"}
    matchbook = _matchbook_observation(matchbook_event, _priced_matchbook_market(moneyline, "1.85"))
    kalshi = _kalshi_game_observation(kalshi_event, no_bid="0.40")
    service, repository = _scan_service()
    try:
        decision = service.scan_pair(matchbook, kalshi, maximum_execution_risk=100)
        record = _edges(service, repository, decision)
        assert record.gross_edge < 0
        assert record.net_edge < 0
        assert decision.depth_scan is not None
        assert decision.depth_scan.selected_quotes
        assert all(quote.cumulative_depth > 0 for quote in decision.depth_scan.selected_quotes)
    finally:
        repository.close()


def test_nfl_half_point_spread_and_total_use_the_venue_commission() -> None:
    event = _mb_indkc()
    _, spread = _normalize_kalshi_spread()
    _, total = _normalize_kalshi_total()
    assert spread.line is not None and total.line is not None
    spread_market = _priced_matchbook_market(
        _clone_mb_spread(event, home_line=spread.line),
        "4.20",
    )
    total_market = _priced_matchbook_market(
        _clone_mb_total(event, line=total.line),
        "4.20",
    )
    resolver = VenueCostResolver()
    reference = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.GAME_WINNER,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    service, repository = _scan_service()
    try:
        for market_payload, family in (
            (spread_market, MarketFamily.POINT_SPREAD),
            (total_market, MarketFamily.TOTAL_POINTS),
        ):
            observation = _matchbook_observation(event, market_payload)
            assert observation.market.family is family
            assert observation.market.line == (
                spread.line if family is MarketFamily.POINT_SPREAD else total.line
            )
            snapshot = service.cost_resolver.resolve(
                venue=VenueName.MATCHBOOK,
                market_class=observation.market.family,
                action=MarketAction.BACK,
                as_of=AS_OF,
            )
            assert snapshot.rate == reference.rate
            assert snapshot.fee_basis is reference.fee_basis
            assert snapshot.source == reference.source
            assert snapshot.market_class == family.value
    finally:
        repository.close()


def test_soccer_paper_scan_still_prices_matchbook_at_the_provider_default() -> None:
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    pm_market = {**pm_market, "feesEnabled": False}
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
        quote_age_ms=180,
        fee_snapshot=resolve_polymarket_fee_metadata(pm_market),
    )
    service, repository = _scan_service()
    try:
        decision = service.scan_pair(matchbook, polymarket, maximum_execution_risk=100)
        assert "unknown_required_venue_cost:matchbook" not in " ".join(decision.rejection_reasons)
        matchbook_cost = next(
            item for item in decision.venue_costs if item.venue is VenueName.MATCHBOOK
        )
        assert matchbook_cost.rate == Decimal("0.02")
        assert matchbook_cost.fee_basis is FeeBasis.PROFIT_COMMISSION
        assert matchbook_cost.source == MATCHBOOK_REGISTRY_SOURCE
        assert matchbook_cost.market_class == "both_teams_to_score"
    finally:
        repository.close()


def test_operator_override_reaches_the_nfl_game_winner_scan() -> None:
    store = SqliteMatchbookAccountFeeStore()
    store.set_override(Decimal("0.015"))
    matchbook_event, moneyline, bundle = _mia_kc_books()
    kalshi_event = {key: value for key, value in bundle.items() if key != "polymarket"}
    matchbook = _matchbook_observation(matchbook_event, _priced_matchbook_market(moneyline, "4.50"))
    kalshi = _kalshi_game_observation(kalshi_event, no_bid="0.80")
    service, repository = _scan_service(fee_store=store)
    try:
        decision = service.scan_pair(matchbook, kalshi, maximum_execution_risk=100)
        matchbook_cost = next(
            item for item in decision.venue_costs if item.venue is VenueName.MATCHBOOK
        )
        assert matchbook_cost.rate == Decimal("0.015")
        assert matchbook_cost.source == MATCHBOOK_OVERRIDE_SOURCE
        assert "unknown_required_venue_cost" not in " ".join(decision.rejection_reasons)
    finally:
        repository.close()
