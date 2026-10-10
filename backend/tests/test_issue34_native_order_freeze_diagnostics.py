"""Issue #34: per-leg Polymarket native-order freeze diagnostics.

Observability only. Rejection codes, thresholds, fees, FX, and dispatch stay
unchanged. Fixture books. No live venue reads or orders.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.execution_snapshot import ExecutionSnapshot, FrozenNativeOrder
from sports_hedge.application.venue_capital import (
    POLYMARKET_CONSTRAINTS_UNPROVEN,
    VENUE_CAPITAL_UNPROVEN,
    judge_real_package,
)
from sports_hedge.arbitrage.watchlist.price2_activity import project_audit_row
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.frozen import freeze_native_orders, freeze_native_package
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
TOKEN = "8" * 76


def _leg(
    venue: VenueName,
    *,
    runner: str,
    market: str,
    event: str = "evt",
    stake: str = "2.50",
    odds: str = "2",
    currency: str = "USD",
    outcome: str = "away",
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=market,
        source_runner_id=runner,
        source_event_id=event,
        currency=currency,
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
    )


def _pm(**overrides: str) -> PaperOpportunityLeg:
    fields = {
        "runner": TOKEN,
        "market": "condition-1",
        "event": "evt-pm",
        "stake": "2.50",
        "odds": "2",
        "currency": "USD",
        "outcome": "away",
    }
    fields.update(overrides)
    return _leg(VenueName.POLYMARKET, **fields)


def _mb() -> PaperOpportunityLeg:
    return _leg(
        VenueName.MATCHBOOK,
        runner="101",
        market="mb-mkt",
        event="evt-mb",
        stake="4",
        odds="2.09",
        currency="GBP",
        outcome="home",
    )


def _decision(*legs: PaperOpportunityLeg) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_market_id="mkt",
        canonical_event_id="evt",
        eligible_for_paper_simulation=True,
        market_match={"matched": True, "confidence": 1.0, "reasons": ["test"]},
        solver_model="simple_complete_set",
        fill_legs=list(legs),
        scanned_at=NOW,
    )


def _books(**payload: object) -> dict[str, dict[str, object]]:
    return {TOKEN: dict(payload)}


def test_default_execution_stays_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"


def test_successful_polymarket_freeze_records_frozen_diagnostic() -> None:
    package = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(tick_size="0.01", min_order_size="5"),
    )
    assert len(package.orders) == 1
    assert package.orders[0].native_runner_id == TOKEN
    assert len(package.diagnostics) == 1
    row = package.diagnostics[0]
    assert row.frozen is True
    assert row.reason == "frozen"
    assert row.venue == "polymarket"
    assert row.outcome == "away"
    assert row.native_market_id == "condition-1"
    assert row.native_runner_id == TOKEN
    assert row.observed_tick_size == "0.01"
    assert row.observed_minimum_shares == "5"
    assert row.intended_native_stake == "2.50"
    assert row.intended_limit_price == "0.5"
    assert Decimal(row.intended_native_shares or "0") == Decimal(5)
    assert freeze_native_orders(
        _decision(_pm()),
        polymarket_books=_books(tick_size="0.01", min_order_size="5"),
    ) == package.orders


def test_missing_tick_size_is_distinguished_from_missing_minimum() -> None:
    missing_tick = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(min_order_size="5", asks=[]),
    )
    assert missing_tick.orders == ()
    assert missing_tick.diagnostics[0].reason == "missing_tick_size"
    assert missing_tick.diagnostics[0].frozen is False
    assert missing_tick.diagnostics[0].observed_minimum_shares == "5"

    missing_min = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(tick_size="0.01", asks=[]),
    )
    assert missing_min.orders == ()
    assert missing_min.diagnostics[0].reason == "missing_minimum_order_size"
    assert missing_min.diagnostics[0].observed_tick_size == "0.01"


def test_invalid_constraint_metadata_does_not_invent_tick_or_minimum() -> None:
    garbage = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(tick_size="not-a-tick", min_order_size="5"),
    )
    assert garbage.orders == ()
    assert garbage.diagnostics[0].reason == "invalid_constraint_metadata"
    assert garbage.diagnostics[0].observed_tick_size is None

    zero_min = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(tick_size="0.01", min_order_size="0"),
    )
    assert zero_min.diagnostics[0].reason == "invalid_constraint_metadata"


def test_unsupported_tick_size() -> None:
    package = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(tick_size="0.03", min_order_size="5"),
    )
    assert package.orders == ()
    assert package.diagnostics[0].reason == "unsupported_tick_size"
    assert package.diagnostics[0].observed_tick_size == "0.03"


def test_below_native_minimum_shares_records_intended_size() -> None:
    package = freeze_native_package(
        _decision(_pm(stake="1.00")),
        polymarket_books=_books(tick_size="0.01", min_order_size="5"),
    )
    assert package.orders == ()
    row = package.diagnostics[0]
    assert row.reason == "below_native_minimum_shares"
    assert row.observed_minimum_shares == "5"
    assert row.intended_native_stake == "1.00"
    assert Decimal(row.intended_native_shares or "0") == Decimal(2)
    assert row.intended_limit_price == "0.5"


def test_malformed_token_and_missing_market() -> None:
    bad_token = freeze_native_package(
        _decision(_pm(runner="yes")),
        polymarket_books={"yes": {"tick_size": "0.01", "min_order_size": "1"}},
    )
    assert bad_token.orders == ()
    assert bad_token.diagnostics[0].reason == "missing_or_invalid_native_market_or_token"

    missing_market = freeze_native_package(
        _decision(_pm(market="")),
        polymarket_books=_books(tick_size="0.01", min_order_size="1"),
    )
    assert missing_market.orders == ()
    assert missing_market.diagnostics[0].reason == "missing_or_invalid_native_market_or_token"


def test_price_rounding_incompatible_for_untradeable_limit() -> None:
    package = freeze_native_package(
        _decision(_pm(odds="1000")),
        polymarket_books=_books(tick_size="0.1", min_order_size="1"),
    )
    assert package.orders == ()
    assert package.diagnostics[0].reason == "price_rounding_incompatible"


def test_stake_rounding_or_size_incompatible() -> None:
    package = freeze_native_package(
        _decision(_pm(stake="0.015")),
        polymarket_books=_books(tick_size="0.01", min_order_size="0.001"),
    )
    assert package.orders == ()
    assert package.diagnostics[0].reason == "stake_rounding_or_size_incompatible"


def test_mixed_matchbook_and_polymarket_legs_keep_authoritative_reject_code() -> None:
    package = freeze_native_package(
        _decision(_mb(), _pm()),
        polymarket_books=_books(asks=[{"price": "0.5", "size": "10"}]),
    )
    assert len(package.orders) == 1
    assert package.orders[0].venue == "matchbook"
    reasons = {item.venue: item.reason for item in package.diagnostics}
    assert reasons["matchbook"] == "frozen"
    assert reasons["polymarket"] == "missing_tick_size"
    block, _ = judge_real_package(
        mode="real",
        legs=[_mb(), _pm()],
        frozen_orders=package.orders,
        readiness=(),
    )
    assert block == POLYMARKET_CONSTRAINTS_UNPROVEN
    proven, _ = judge_real_package(
        mode="real",
        legs=[_pm()],
        frozen_orders=freeze_native_orders(
            _decision(_pm()),
            polymarket_books=_books(tick_size="0.01", min_order_size="1"),
        ),
        readiness=(),
    )
    assert proven == VENUE_CAPITAL_UNPROVEN


def test_diagnostics_never_include_exception_text_or_change_accept_semantics() -> None:
    package = freeze_native_package(
        _decision(_pm()),
        polymarket_books=_books(tick_size="nope", min_order_size="x"),
    )
    snapshot = ExecutionSnapshot(
        catalogue_row_id="cat-1",
        started_at=NOW,
        retrievals=(),
        accepted=False,
        rejection_reason=POLYMARKET_CONSTRAINTS_UNPROVEN,
        net_edge="0.0022",
        minimum_net_edge="0.01",
        frozen_orders=package.orders,
        native_order_freeze_diagnostics=package.diagnostics,
    )
    payload = json.loads(snapshot.to_json())
    blob = json.dumps(payload)
    assert "TranslationError" not in blob
    assert "Traceback" not in blob
    assert "secret" not in blob.lower()
    assert payload["rejection_reason"] == POLYMARKET_CONSTRAINTS_UNPROVEN
    assert payload["accepted"] is False
    assert payload["frozen_orders"] == []
    row = payload["native_order_freeze_diagnostics"][0]
    assert row["reason"] == "invalid_constraint_metadata"
    assert "exception" not in row


def test_old_stored_audit_shows_details_not_recorded() -> None:
    historical = {
        "snapshot_id": "exec:old-alaves",
        "started_at": "2026-10-10T12:00:00+00:00",
        "evaluated_at": "2026-10-10T12:00:01+00:00",
        "net_edge": "0.0022",
        "guaranteed_profit": "0.01",
        "accepted": False,
        "rejection_reason": POLYMARKET_CONSTRAINTS_UNPROVEN,
        "frozen_orders": [],
        "legs": [
            {
                "venue": "polymarket",
                "outcome": "home",
                "displayed_odds": "2.10",
                "requested_stake": "1",
                "native_market_id": "pm-1",
                "native_runner_id": TOKEN,
            }
        ],
    }
    assert "native_order_freeze_diagnostics" not in historical
    observation = project_audit_row(
        {
            "snapshot_id": "exec:old-alaves",
            "opportunity_id": "watch:alaves",
            "occurred_at": NOW,
            "accepted": False,
            "rejection_reason": POLYMARKET_CONSTRAINTS_UNPROVEN,
            "snapshot_json": json.dumps(historical),
            "diagnostics_json": "{}",
        }
    )
    assert observation.rejection_reason == POLYMARKET_CONSTRAINTS_UNPROVEN
    assert observation.native_order_freeze_recorded is False
    assert observation.economics_vs_threshold == "threshold_not_recorded"
    assert observation.legs[0].freeze_status == "details_not_recorded"
    assert observation.legs[0].freeze_reason == "details_not_recorded"
    assert observation.legs[0].native_frozen is None


def test_activity_projection_exposes_recorded_subreason_and_threshold() -> None:
    package = freeze_native_package(
        _decision(_mb(), _pm(stake="1.00")),
        polymarket_books=_books(tick_size="0.01", min_order_size="5"),
    )
    snapshot = ExecutionSnapshot(
        catalogue_row_id="cat-1",
        started_at=NOW,
        retrievals=(),
        accepted=False,
        rejection_reason=POLYMARKET_CONSTRAINTS_UNPROVEN,
        net_edge="0.0022",
        guaranteed_profit="0.01",
        minimum_net_edge="0.01",
        minimum_net_edge_source="operator_settings",
        frozen_orders=package.orders,
        native_order_freeze_diagnostics=package.diagnostics,
        legs=(),
    )
    payload = json.loads(snapshot.to_json())
    payload["legs"] = [
        {
            "venue": "matchbook",
            "outcome": "home",
            "displayed_odds": "2.09",
            "requested_stake": "4",
            "native_market_id": "mb-mkt",
            "native_runner_id": "101",
        },
        {
            "venue": "polymarket",
            "outcome": "away",
            "displayed_odds": "2",
            "requested_stake": "1.00",
            "native_market_id": "condition-1",
            "native_runner_id": TOKEN,
        },
    ]
    observation = project_audit_row(
        {
            "snapshot_id": snapshot.snapshot_id,
            "opportunity_id": "watch:demo",
            "occurred_at": NOW,
            "accepted": False,
            "rejection_reason": POLYMARKET_CONSTRAINTS_UNPROVEN,
            "snapshot_json": json.dumps(payload),
            "diagnostics_json": "{}",
        }
    )
    assert observation.native_order_freeze_recorded is True
    assert observation.minimum_net_edge == "0.01"
    assert observation.economics_vs_threshold == "below_configured_threshold"
    by_venue = {leg.venue: leg for leg in observation.legs}
    assert by_venue["matchbook"].freeze_status == "frozen"
    assert by_venue["matchbook"].native_frozen is True
    assert by_venue["polymarket"].freeze_status == "not_frozen"
    assert by_venue["polymarket"].freeze_reason == "below_native_minimum_shares"
    assert by_venue["polymarket"].observed_minimum_shares == "5"
    assert observation.rejection_reason == POLYMARKET_CONSTRAINTS_UNPROVEN


def test_frozen_native_order_dataclass_round_trip_still_deserializes_old_orders() -> None:
    order = FrozenNativeOrder(
        venue="polymarket",
        native_event_id="evt",
        native_market_id="mkt",
        native_runner_id=TOKEN,
        side="back",
        currency="USD",
        approved_decimal_odds="2",
        approved_stake="2.50",
    )
    snapshot = ExecutionSnapshot(
        catalogue_row_id="cat",
        started_at=NOW,
        retrievals=(),
        frozen_orders=(order,),
    )
    payload = json.loads(snapshot.to_json())
    assert "native_order_freeze_diagnostics" in payload
    assert payload["native_order_freeze_diagnostics"] == []
    assert payload["frozen_orders"][0]["native_runner_id"] == TOKEN
