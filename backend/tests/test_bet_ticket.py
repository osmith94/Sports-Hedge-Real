from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger, get_paper_operations_service
from sports_hedge.api.watchlist import get_bet_ticket_operations, get_watchlist_service
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.adapters import (
    balances_from_treasury,
    request_from_paper_decision,
)
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.arbitrage.watchlist.service import _opportunity_id
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.bet_ticket import (
    LIVE_BLOCKED_REASON,
    PAPER_CONFIRM_CTA,
    RecommendedPaperDeployment,
    current_bet_deployability,
)
from sports_hedge.treasury.models import TreasuryLockRequest
from test_lane2_fixed_paper_preparation import TEN, _persist_qualified
from test_step8f_automatic_paper_entry import (
    OBSERVED,
    _matchbook_btts,
    _ops_bundle,
    _polymarket_btts,
)
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from test_step8b_first_team_to_score import _ftts_books, _ftts_mb_payload, _ftts_pm_payload
from test_step7_safe_market_expansion import MB_EVENT, PM_EVENT


def test_current_bet_deployability_requires_accepted_positive_size() -> None:
    assert current_bet_deployability(
        semantically_qualified=False,
        semantic_blocked_reason="below_trigger",
        allocation_accepted=True,
        recommended_size=Decimal("40"),
    ) == (False, "below_trigger")
    assert current_bet_deployability(
        semantically_qualified=True,
        semantic_blocked_reason=None,
        allocation_accepted=False,
        recommended_size=Decimal("50"),
        allocation_rejection_reason="reserve matchbook GBP",
    ) == (False, "reserve matchbook GBP")
    assert current_bet_deployability(
        semantically_qualified=True,
        semantic_blocked_reason=None,
        allocation_accepted=True,
        recommended_size=Decimal("0"),
        limiting_constraint_detail="native available matchbook GBP",
    ) == (False, "native available matchbook GBP")
    assert current_bet_deployability(
        semantically_qualified=True,
        semantic_blocked_reason=None,
        allocation_accepted=True,
        recommended_size=Decimal("25"),
    ) == (True, None)


def test_recommended_deployment_cannot_be_actionable_when_rejected() -> None:
    with pytest.raises(ValidationError):
        RecommendedPaperDeployment(
            opportunity_id="watch:blocked",
            accepted=False,
            bet_actionable=True,
        )


def test_recommended_size_is_allocator_not_hardcoded_ten(tmp_path: Path) -> None:
    _scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_polymarket_btts()
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        watch = watchlist.repository.get(opportunity_id)
        assert watch is not None
        assert watch.status is OpportunityStatus.TRIGGERED
        rec = ops.recommend_paper_deployment(opportunity_id)
        assert rec.accepted is True
        assert rec.bet_actionable is True
        assert rec.bet_blocked_reason is None
        assert rec.locks_treasury is False
        assert rec.opens_trade is False
        assert rec.places_orders is False
        assert rec.execution_seam.execution_enabled is False
        assert rec.execution_seam.live_cta_available is False
        assert rec.execution_seam.paper_confirm_cta == PAPER_CONFIRM_CTA
        assert rec.recommended_size_gbp != TEN
        assert rec.recommended_size_gbp > TEN
        fx = {item.currency.upper(): item.gbp_per_unit for item in decision.fx_snapshots}
        fx.setdefault("GBP", Decimal("1"))
        request = request_from_paper_decision(
            decision,
            policy=policy_from_settings(get_settings()),
            balances=balances_from_treasury(ledger.treasury.snapshot(), gbp_per_unit=fx),
        )
        assert request is not None
        baseline = allocate(request)
        assert rec.recommended_size_gbp == baseline.recommended_size
        assert rec.maximum_validated_size_gbp == baseline.maximum_validated_size
        before = ledger.treasury.snapshot()
        preview = ops.prepare_fixed_deployment(opportunity_id, rec.recommended_size_gbp)
        assert preview.accepted is True
        assert preview.applied_size_gbp == rec.recommended_size_gbp
        assert preview.recommended_size_gbp == rec.recommended_size_gbp
        assert preview.operator_entered_size_gbp == rec.recommended_size_gbp
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").available_cash == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).available_cash
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == before.pool(
            VenueName.POLYMARKET, "USD"
        ).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_bet_ticket_shows_legs_fees_fx_depth_risk_and_treasury(tmp_path: Path) -> None:
    _scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_polymarket_btts()
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        preview = ops.prepare_fixed_deployment(opportunity_id, TEN)
        assert preview.accepted is True
        assert preview.execution_seam.live_cta_available is False
        assert preview.execution_seam.live_blocked_reason == LIVE_BLOCKED_REASON
        assert preview.places_orders is False
        assert preview.locks_treasury is False
        assert preview.quote_age_ms is not None
        assert preview.net_edge is not None
        assert preview.venue_pair
        assert preview.market_label
        assert preview.fx_assumptions
        assert preview.treasury_remaining
        assert preview.survivability.estimate_not_guarantee is True
        for leg in preview.legs:
            assert leg.displayed_odds is not None
            assert leg.stake_native > 0
            assert leg.capital_reporting > 0
            assert leg.fx_gbp_per_unit is not None
            assert leg.venue_fee is not None
            assert leg.action in {None, "back", "buy"}
        preparable = ops.list_preparable(decision.canonical_event_id)
        qualified = [item for item in preparable if item.opportunity_id == opportunity_id]
        assert qualified
        assert qualified[0].bet_actionable is True
        assert qualified[0].recommended_size_gbp == preview.recommended_size_gbp
        annotated = ops.annotate_bet_ticket_actions([watchlist.repository.get(opportunity_id)])
        assert annotated[0].bet_actionable is True
        assert annotated[0].bet_blocked_reason is None
    finally:
        repository.close()
        ledger.close()


def test_rejected_and_unevaluated_rows_are_not_bet_actionable(tmp_path: Path) -> None:
    _scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    try:
        assert ops.annotate_bet_ticket_actions([]) == []
        try:
            ops.recommend_paper_deployment("watch:unknown")
            raise AssertionError("expected missing plan")
        except PaperOperationsError as exc:
            assert str(exc) == "missing_paper_fill_plan"

        from datetime import UTC, datetime

        from sports_hedge.arbitrage.watchlist.models import WatchObservation

        rejected = watchlist.observe(
            WatchObservation(
                observed_at=datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
                canonical_event_id="evt-rejected",
                canonical_market_id="mkt-rejected-totals",
                home_team="Leeds United",
                away_team="Newcastle United",
                trigger_net_edge=Decimal("0.01"),
                current_net_edge=Decimal("0.40"),
                implied_probability_sum=Decimal("0.60"),
                solver_is_arbitrage=False,
                eligible_for_paper_simulation=False,
                rejection_reasons=["market_not_equivalent"],
                quote_age_ms=80,
            )
        )
        assert rejected.status is OpportunityStatus.REJECTED
        labelled = ops.annotate_bet_ticket_actions([rejected])
        assert labelled[0].bet_actionable is False
        assert labelled[0].bet_blocked_reason == "market_not_equivalent"
        assert labelled[0].current_net_edge == Decimal("0.40")
    finally:
        repository.close()
        ledger.close()


def test_qualified_row_loses_bet_when_authoritative_treasury_is_consumed(
    tmp_path: Path,
) -> None:
    """Semantic qualification can remain TRIGGERED after capital changes.

    Active BET must follow the current allocator recommendation, not the
    qualifying scan. Recommend/prepare must not lock or OPEN.
    """

    _scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_polymarket_btts()
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        watch = watchlist.repository.get(opportunity_id)
        assert watch is not None
        assert watch.status is OpportunityStatus.TRIGGERED
        rec = ops.recommend_paper_deployment(opportunity_id)
        assert rec.accepted is True
        assert rec.bet_actionable is True
        annotated = ops.annotate_bet_ticket_actions([watch])
        assert annotated[0].bet_actionable is True
        preparable = ops.list_preparable(decision.canonical_event_id)
        qualified = [item for item in preparable if item.opportunity_id == opportunity_id]
        assert qualified and qualified[0].bet_actionable is True

        before = ledger.treasury.snapshot()
        matchbook = before.pool(VenueName.MATCHBOOK, "GBP")
        available = matchbook.available_cash
        assert available > 0
        locked_at = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)
        ledger.treasury.lock_capital(
            [
                TreasuryLockRequest(
                    venue=VenueName.MATCHBOOK,
                    native_currency="GBP",
                    amount_native=available,
                    lock_id="lock-other-open-exposure",
                    trade_id="ptrade-other-open",
                    opportunity_id="watch:other-open-trade",
                    fx_rate_gbp_per_unit=Decimal("1"),
                )
            ],
            occurred_at=locked_at,
        )
        drained = ledger.treasury.snapshot()
        assert drained.pool(VenueName.MATCHBOOK, "GBP").available_cash == Decimal("0")
        assert drained.pool(VenueName.MATCHBOOK, "GBP").locked_capital == available

        watch_after = watchlist.repository.get(opportunity_id)
        assert watch_after is not None
        assert watch_after.status is OpportunityStatus.TRIGGERED
        assert watch_after.current_net_edge == watch.current_net_edge

        rec_after = ops.recommend_paper_deployment(opportunity_id)
        assert rec_after.accepted is False
        assert rec_after.bet_actionable is False
        assert rec_after.bet_blocked_reason
        assert rec_after.locks_treasury is False
        assert rec_after.opens_trade is False
        assert rec_after.places_orders is False

        annotated_after = ops.annotate_bet_ticket_actions([watch_after])
        assert annotated_after[0].status is OpportunityStatus.TRIGGERED
        assert annotated_after[0].bet_actionable is False
        assert annotated_after[0].bet_blocked_reason == rec_after.bet_blocked_reason

        listed = [
            item
            for item in ops.list_preparable(decision.canonical_event_id)
            if item.opportunity_id == opportunity_id
        ]
        assert listed
        assert listed[0].bet_actionable is False
        assert listed[0].bet_blocked_reason == rec_after.bet_blocked_reason

        preview = ops.prepare_fixed_deployment(opportunity_id, TEN)
        assert preview.accepted is False
        assert preview.opens_trade is False
        assert preview.locks_treasury is False
        assert ops.list_active_trades() == []

        still = ledger.treasury.snapshot()
        assert still.pool(VenueName.MATCHBOOK, "GBP").available_cash == Decimal("0")
        assert still.pool(VenueName.MATCHBOOK, "GBP").locked_capital == available
    finally:
        repository.close()
        ledger.close()


def test_recommend_and_prepare_api_keep_paper_boundary(tmp_path: Path) -> None:
    _scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_polymarket_btts()
    )
    try:
        app.dependency_overrides[get_paper_operations_service] = lambda: ops
        app.dependency_overrides[get_paper_ledger] = lambda: ledger
        app.dependency_overrides[get_watchlist_service] = lambda: watchlist
        app.dependency_overrides[get_bet_ticket_operations] = lambda: ops
        client = TestClient(app)
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        recommended = client.post(
            "/paper/recommend-deployment",
            json={"opportunity_id": opportunity_id},
        )
        assert recommended.status_code == 200, recommended.text
        body = recommended.json()
        assert body["accepted"] is True
        assert body["places_orders"] is False
        assert body["locks_treasury"] is False
        assert Decimal(body["recommended_size_gbp"]) != TEN
        prepared = client.post(
            "/paper/prepare-deployment",
            json={
                "opportunity_id": opportunity_id,
                "requested_size_gbp": str(body["recommended_size_gbp"]),
            },
        )
        assert prepared.status_code == 200, prepared.text
        ticket = prepared.json()
        assert ticket["accepted"] is True
        assert ticket["execution_seam"]["live_cta_available"] is False
        assert ticket["execution_seam"]["execution_enabled"] is False
        assert Decimal(ticket["applied_size_gbp"]) == Decimal(body["recommended_size_gbp"])
        too_big = client.post(
            "/paper/prepare-deployment",
            json={
                "opportunity_id": opportunity_id,
                "requested_size_gbp": "1000000",
            },
        )
        assert too_big.status_code == 200
        rejected = too_big.json()
        assert rejected["accepted"] is False
        assert rejected["rejection_reason"] == "requested_size_exceeds_validated_maximum"
        assert Decimal(rejected["maximum_validated_size_gbp"]) == Decimal(
            body["maximum_validated_size_gbp"]
        )
        tracked = client.get("/paper/watchlist/tracked")
        assert tracked.status_code == 200
        rows = {item["opportunity_id"]: item for item in tracked.json()}
        assert rows[opportunity_id]["bet_actionable"] is True
    finally:
        app.dependency_overrides.clear()
        repository.close()
        ledger.close()


def test_generalized_ticket_still_paper_only(tmp_path: Path) -> None:
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT, _ftts_mb_payload(), observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        PM_EVENT, _ftts_pm_payload(), _ftts_books(), observed_at=OBSERVED, quote_age_ms=150
    )
    _scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=matchbook, right=polymarket
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        rec = ops.recommend_paper_deployment(opportunity_id)
        preview = ops.prepare_fixed_deployment(opportunity_id, rec.recommended_size_gbp)
        assert preview.accepted is True
        assert preview.places_orders is False
        assert preview.solver_model
        assert preview.legs
    finally:
        repository.close()
        ledger.close()
