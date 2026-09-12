from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import (
    TreasuryLockRequest,
    UnwindReleaseLeg,
    ValidatedUnwindResult,
)
from sports_hedge.treasury.service import PaperTreasuryError


FX = Decimal("0.80")
SEED = Decimal("1000")
NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _ledger(path: Path | None = None) -> SqlitePaperLedger:
    return SqlitePaperLedger(
        path or ":memory:",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )


def _lock(
    venue: VenueName,
    currency: str,
    amount: Decimal,
    lock_id: str,
    *,
    trade_id: str = "ptrade-demo",
    rate: Decimal | None = None,
) -> TreasuryLockRequest:
    return TreasuryLockRequest(
        venue=venue,
        native_currency=currency,
        amount_native=amount,
        lock_id=lock_id,
        trade_id=trade_id,
        opportunity_id="opp-demo",
        fx_rate_gbp_per_unit=rate if rate is not None else (Decimal("1") if currency == "GBP" else FX),
    )


def test_seed_matchbook_and_polymarket_with_fx_provenance() -> None:
    ledger = _ledger()
    snap = ledger.treasury.snapshot()
    assert snap.session is not None
    assert snap.session.seed_gbp == SEED
    assert snap.session.fx_source == "paper_demo_fx_snapshot"
    assert snap.session.fx_rate_usd_gbp == FX
    matchbook = snap.pool(VenueName.MATCHBOOK, "GBP")
    polymarket = snap.pool(VenueName.POLYMARKET, "USD")
    assert matchbook.available_cash == SEED
    assert matchbook.locked_capital == 0
    assert matchbook.seed_native == SEED
    assert matchbook.gbp_carrying_status == "identity"
    usd_native = (SEED / FX).quantize(Decimal("0.00000001"))
    assert polymarket.available_cash == usd_native
    assert polymarket.fx_source == "paper_demo_fx_snapshot"
    assert polymarket.gbp_carrying_value == pytest.approx(SEED)
    seeds = [item for item in snap.events if item.event_type.value == "seed"]
    assert {item.venue for item in seeds} >= {VenueName.MATCHBOOK, VenueName.POLYMARKET}
    ledger.close()


def test_two_usd_venues_are_independent_pools() -> None:
    ledger = _ledger()
    ledger.treasury.lock_capital(
        [_lock(VenueName.POLYMARKET, "USD", Decimal("100"), "lock-pm-1")],
        occurred_at=NOW,
    )
    snap = ledger.treasury.snapshot()
    polymarket = snap.pool(VenueName.POLYMARKET, "USD")
    kalshi = snap.pool(VenueName.KALSHI, "USD")
    assert polymarket.available_cash < polymarket.seed_native
    assert kalshi.available_cash == kalshi.seed_native
    assert kalshi.identity == "kalshi/USD"
    assert polymarket.identity == "polymarket/USD"
    with pytest.raises(ValueError, match="must not be summed"):
        snap.combined_cash_gbp()
    ledger.close()


def test_lock_reduces_available_and_increases_locked() -> None:
    ledger = _ledger()
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-mb-250")],
        occurred_at=NOW,
    )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.available_cash == Decimal("750")
    assert pool.locked_capital == Decimal("250")
    ledger.close()


def test_second_lock_respects_remaining_and_cannot_go_negative() -> None:
    ledger = _ledger()
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-mb-a")],
        occurred_at=NOW,
    )
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("750"), "lock-mb-b")],
        occurred_at=NOW,
    )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.available_cash == Decimal("0")
    assert pool.locked_capital == Decimal("1000")
    with pytest.raises(PaperTreasuryError, match="insufficient_available_cash"):
        ledger.treasury.lock_capital(
            [_lock(VenueName.MATCHBOOK, "GBP", Decimal("0.01"), "lock-mb-c")],
            occurred_at=NOW,
        )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.available_cash == Decimal("0")
    ledger.close()


def test_multi_venue_lock_is_atomic() -> None:
    ledger = _ledger()
    before = ledger.treasury.snapshot()
    usd = before.pool(VenueName.POLYMARKET, "USD").available_cash
    with pytest.raises(PaperTreasuryError, match="insufficient_available_cash"):
        ledger.treasury.lock_capital(
            [
                _lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-atomic-mb"),
                _lock(VenueName.POLYMARKET, "USD", usd + Decimal("1"), "lock-atomic-pm"),
            ],
            occurred_at=NOW,
        )
    after = ledger.treasury.snapshot()
    assert after.pool(VenueName.MATCHBOOK, "GBP").available_cash == SEED
    assert after.pool(VenueName.POLYMARKET, "USD").available_cash == usd
    assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
    journals = [
        entry
        for entry in ledger.journal.list_entries()
        if entry.source == "paper_fill_simulator"
    ]
    assert journals == []
    ledger.close()


def test_duplicate_lock_is_idempotent_only_for_same_facts() -> None:
    ledger = _ledger()
    request = _lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-dup")
    first = ledger.treasury.lock_capital([request], occurred_at=NOW)
    second = ledger.treasury.lock_capital([request], occurred_at=NOW)
    assert first[0].journal_id == second[0].journal_id
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("250")
    with pytest.raises(PaperTreasuryError, match="conflicting_lock_facts"):
        ledger.treasury.lock_capital(
            [_lock(VenueName.MATCHBOOK, "GBP", Decimal("100"), "lock-dup")],
            occurred_at=NOW,
        )
    with pytest.raises(PaperTreasuryError, match="conflicting_lock_facts"):
        ledger.treasury.lock_capital(
            [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-dup", trade_id="ptrade-other")],
            occurred_at=NOW,
        )
    conflicting_source = _lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-dup")
    conflicting_source.source = "other_source"
    with pytest.raises(PaperTreasuryError, match="conflicting_lock_facts"):
        ledger.treasury.lock_capital([conflicting_source], occurred_at=NOW)
    conflicting_capital = _lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-dup")
    conflicting_capital.capital_source = "MANUAL_EXTERNAL"
    with pytest.raises(PaperTreasuryError, match="conflicting_lock_facts"):
        ledger.treasury.lock_capital([conflicting_capital], occurred_at=NOW)
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("250")
    ledger.close()


def test_demo_reset_fails_closed_while_locks_open_then_preserves_history(tmp_path: Path) -> None:
    path = tmp_path / "treasury.sqlite"
    ledger = _ledger(path)
    first_id = ledger.treasury.snapshot().session.session_id
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-before-reset")],
        occurred_at=NOW,
    )
    with pytest.raises(PaperTreasuryError, match="active_treasury_locks"):
        ledger.treasury.reset_demo_session(
            seed_gbp=SEED,
            usd_gbp_per_unit=FX,
            fx_source="paper_demo_fx_snapshot",
            reason="operator reset",
            now=NOW,
        )
    assert ledger.treasury.snapshot().session.session_id == first_id
    assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == Decimal("250")
    ledger.treasury.post_unwind(
        ValidatedUnwindResult(
            trade_id="ptrade-demo",
            close_completed=True,
            source_id="unwind-before-reset",
            releases=[
                UnwindReleaseLeg(
                    venue=VenueName.MATCHBOOK,
                    native_currency="GBP",
                    lock_id="lock-before-reset",
                    amount_native=Decimal("250"),
                    fx_rate_gbp_per_unit=Decimal("1"),
                )
            ],
        ),
        now=NOW,
    )
    ledger.treasury.reset_demo_session(
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
        reason="operator reset",
        now=NOW,
    )
    ledger.close()
    reopened = SqlitePaperLedger(
        path,
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        auto_seed=True,
    )
    snap = reopened.treasury.snapshot()
    assert snap.session is not None
    assert snap.session.session_id != first_id
    assert snap.pool(VenueName.MATCHBOOK, "GBP").available_cash == SEED
    historical = reopened.treasury.list_events(limit=200)
    assert any(item.lock_id == "lock-before-reset" for item in historical)
    assert any(item.event_type.value == "session_close" for item in historical)
    reopened.close()


def test_demo_reset_fails_closed_while_open_paper_positions() -> None:
    from sports_hedge.paper.trades import PaperTrade, PaperTradeState

    ledger = _ledger()
    ledger.trades.save(
        PaperTrade(
            trade_id="ptrade-open",
            opportunity_id="opp-open",
            state=PaperTradeState.OPEN,
            opened_at=NOW,
            last_updated_at=NOW,
        )
    )
    with pytest.raises(PaperTreasuryError, match="open_paper_positions"):
        ledger.treasury.reset_demo_session(
            seed_gbp=SEED,
            usd_gbp_per_unit=FX,
            fx_source="paper_demo_fx_snapshot",
            reason="operator reset",
            now=NOW,
        )
    assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").available_cash == SEED
    ledger.close()


def test_fx_snapshot_updates_carrying_value_not_native() -> None:
    ledger = _ledger()
    native_before = ledger.treasury.snapshot().pool(VenueName.POLYMARKET, "USD").available_cash
    ledger.treasury.apply_fx_snapshot(
        usd_gbp_per_unit=Decimal("0.50"),
        fx_source="ecb_eurofxref",
        fx_as_of=NOW,
    )
    pool = ledger.treasury.snapshot().pool(VenueName.POLYMARKET, "USD")
    assert pool.available_cash == native_before
    assert pool.fx_source == "ecb_eurofxref"
    assert pool.gbp_carrying_value == native_before * Decimal("0.50")
    matchbook = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert matchbook.available_cash == SEED
    ledger.close()


def test_unwind_posts_only_after_completed_close() -> None:
    ledger = _ledger()
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-unwind")],
        occurred_at=NOW,
    )
    ignored = ledger.treasury.post_unwind(
        ValidatedUnwindResult(
            trade_id="ptrade-demo",
            close_completed=False,
            conditionally_releasable_native={"GBP": Decimal("250")},
            releases=[
                UnwindReleaseLeg(
                    venue=VenueName.MATCHBOOK,
                    native_currency="GBP",
                    lock_id="lock-unwind",
                    amount_native=Decimal("250"),
                    realised_pnl_native=Decimal("10"),
                    fx_rate_gbp_per_unit=Decimal("1"),
                )
            ],
        )
    )
    assert ignored == []
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("250")
    posted = ledger.treasury.post_unwind(
        ValidatedUnwindResult(
            trade_id="ptrade-demo",
            close_completed=True,
            source_id="unwind:ptrade-demo",
            releases=[
                UnwindReleaseLeg(
                    venue=VenueName.MATCHBOOK,
                    native_currency="GBP",
                    lock_id="lock-unwind",
                    amount_native=Decimal("250"),
                    realised_pnl_native=Decimal("10"),
                    fee_native=Decimal("2"),
                    fx_rate_gbp_per_unit=Decimal("1"),
                )
            ],
        ),
        now=NOW,
    )
    assert posted
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("0")
    assert pool.available_cash == Decimal("1010")
    assert pool.realised_pnl_native == Decimal("10")
    assert pool.cumulative_fees_native == Decimal("2")
    with pytest.raises(PaperTreasuryError, match="release_exceeds_lock"):
        ledger.treasury.post_unwind(
            ValidatedUnwindResult(
                trade_id="ptrade-demo",
                close_completed=True,
                source_id="unwind:ptrade-demo:dup",
                releases=[
                    UnwindReleaseLeg(
                        venue=VenueName.MATCHBOOK,
                        native_currency="GBP",
                        lock_id="lock-unwind",
                        amount_native=Decimal("250"),
                        fx_rate_gbp_per_unit=Decimal("1"),
                    )
                ],
            )
        )
    ledger.close()


def test_settlement_releases_lock_and_records_realised_pnl() -> None:
    from sports_hedge.paper.settlement import LegSettlement, PaperSettlementComputation
    from sports_hedge.paper.trades import PaperSettlementRequest, PaperTrade, PaperTradeState

    ledger = _ledger()
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-settle", trade_id="ptrade-settle")],
        occurred_at=NOW,
    )
    trade = PaperTrade(
        trade_id="ptrade-settle",
        opportunity_id="opp-demo",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    computation = PaperSettlementComputation(
        winning_outcome="home",
        realised_pnl_gbp=Decimal("40"),
        legs=[
            LegSettlement(
                outcome="home",
                venue="matchbook",
                currency="GBP",
                filled_stake=Decimal("250"),
                won=True,
                venue_fee=Decimal("10"),
                net_payoff=Decimal("290"),
                native_pnl=Decimal("40"),
                gbp_pnl=Decimal("40"),
                fx_rate_gbp_per_unit=Decimal("1"),
                capital_source="AUTO_POOL",
            )
        ],
    )
    ledger.treasury.apply_settlement(
        trade,
        computation,
        PaperSettlementRequest(winning_outcome="home", source="operator", source_id="evt-1"),
        settled_at=NOW,
    )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("0")
    assert pool.available_cash == Decimal("1040")
    assert pool.realised_pnl_native == Decimal("40")
    assert pool.cumulative_fees_native == Decimal("10")
    ledger.treasury.apply_settlement(
        trade,
        computation,
        PaperSettlementRequest(winning_outcome="home", source="operator", source_id="evt-1"),
        settled_at=NOW,
    )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.available_cash == Decimal("1040")
    ledger.close()


def test_settlement_cannot_release_another_trades_lock() -> None:
    from sports_hedge.paper.settlement import LegSettlement, PaperSettlementComputation
    from sports_hedge.paper.trades import PaperSettlementRequest, PaperTrade, PaperTradeState

    ledger = _ledger()
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-a", trade_id="ptrade-a")],
        occurred_at=NOW,
    )
    ledger.treasury.lock_capital(
        [_lock(VenueName.MATCHBOOK, "GBP", Decimal("250"), "lock-b", trade_id="ptrade-b")],
        occurred_at=NOW,
    )
    stranger = PaperTrade(
        trade_id="ptrade-stranger",
        opportunity_id="opp-stranger",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    computation = PaperSettlementComputation(
        winning_outcome="home",
        realised_pnl_gbp=Decimal("0"),
        legs=[
            LegSettlement(
                outcome="home",
                venue="matchbook",
                currency="GBP",
                filled_stake=Decimal("250"),
                won=False,
                net_payoff=Decimal("0"),
                native_pnl=Decimal("-250"),
                gbp_pnl=Decimal("-250"),
                fx_rate_gbp_per_unit=Decimal("1"),
                capital_source="AUTO_POOL",
            )
        ],
    )
    with pytest.raises(PaperTreasuryError, match="unknown_trade_lock"):
        ledger.treasury.apply_settlement(
            stranger,
            computation,
            PaperSettlementRequest(winning_outcome="home", source="operator", source_id="stranger"),
            settled_at=NOW,
        )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("500")
    assert pool.available_cash == Decimal("500")
    owned = PaperTrade(
        trade_id="ptrade-a",
        opportunity_id="opp-demo",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    ledger.treasury.apply_settlement(
        owned,
        computation,
        PaperSettlementRequest(winning_outcome="home", source="operator", source_id="a"),
        settled_at=NOW,
    )
    pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
    assert pool.locked_capital == Decimal("250")
    assert pool.available_cash == Decimal("500")
    ledger.close()


def test_lock_rejects_stale_fx_and_journals_pool_snapshot() -> None:
    ledger = _ledger()
    with pytest.raises(PaperTreasuryError, match="fx_rate_mismatch"):
        ledger.treasury.lock_capital(
            [_lock(VenueName.POLYMARKET, "USD", Decimal("100"), "lock-stale-fx", rate=Decimal("0.50"))],
            occurred_at=NOW,
        )
    assert ledger.treasury.snapshot().pool(VenueName.POLYMARKET, "USD").locked_capital == 0
    posted = ledger.treasury.lock_capital(
        [_lock(VenueName.POLYMARKET, "USD", Decimal("100"), "lock-pool-fx")],
        occurred_at=NOW,
    )
    lock_posting = next(
        item for item in posted[0].postings if item.fx_rate_gbp_per_unit is not None
    )
    assert lock_posting.fx_rate_gbp_per_unit == FX
    event = next(item for item in ledger.treasury.list_events() if item.lock_id == "lock-pool-fx")
    assert event.fx_source == "paper_demo_fx_snapshot"
    assert event.fx_rate_gbp_per_unit == FX
    ledger.close()


def test_rejects_currency_and_venue_mismatch() -> None:
    ledger = _ledger()
    with pytest.raises(PaperTreasuryError, match="stale_unknown_pool"):
        ledger.treasury.lock_capital(
            [_lock(VenueName.MATCHBOOK, "USD", Decimal("10"), "lock-bad-ccy", rate=FX)],
            occurred_at=NOW,
        )
    ledger.close()


def test_treasury_api_seed_lock_and_fx(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(
        tmp_path / "api-treasury.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        include_kalshi=True,
    )
    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    client = TestClient(app)
    try:
        listed = client.get("/paper/treasury")
        assert listed.status_code == 200
        body = listed.json()
        assert body["mode"] == "paper"
        assert body["execution_enabled"] is False
        assert body["capital_kind"] == "paper_hypothetical"
        venues = {pool["venue"]: pool for pool in body["pools"]}
        assert Decimal(venues["matchbook"]["available_cash"]) == SEED
        assert Decimal(venues["matchbook"]["seed_native"]) == SEED
        assert venues["polymarket"]["native_currency"] == "USD"
        assert venues["kalshi"]["native_currency"] == "USD"
        assert "combined_cash" not in str(body).lower() or "never summed" in body["note"].lower()

        locked = client.post(
            "/paper/treasury/locks",
            json=[
                {
                    "venue": "matchbook",
                    "native_currency": "GBP",
                    "amount_native": "250",
                    "lock_id": "api-lock-mb",
                    "trade_id": "ptrade-api",
                    "fx_rate_gbp_per_unit": "1",
                }
            ],
        )
        assert locked.status_code == 200
        after = {pool["venue"]: pool for pool in locked.json()["pools"]}
        assert Decimal(after["matchbook"]["available_cash"]) == Decimal("750")
        assert Decimal(after["matchbook"]["locked_capital"]) == Decimal("250")

        blocked = client.post("/paper/treasury/reset", json={"reason": "api reset while locked"})
        assert blocked.status_code == 409
        assert "active_treasury_locks" in str(blocked.json()["detail"])

        fx = client.post(
            "/paper/treasury/fx-snapshot",
            json={"usd_gbp_per_unit": "0.5", "fx_source": "test_fx"},
        )
        assert fx.status_code == 200
        pm = next(pool for pool in fx.json()["pools"] if pool["venue"] == "polymarket")
        assert Decimal(pm["available_cash"]) == Decimal(venues["polymarket"]["available_cash"])
        assert pm["fx_source"] == "test_fx"

        released = client.post(
            "/paper/treasury/unwind",
            json={
                "trade_id": "ptrade-api",
                "close_completed": True,
                "source_id": "unwind-api-lock",
                "releases": [
                    {
                        "venue": "matchbook",
                        "native_currency": "GBP",
                        "lock_id": "api-lock-mb",
                        "amount_native": "250",
                        "fx_rate_gbp_per_unit": "1",
                    }
                ],
            },
        )
        assert released.status_code == 200
        assert Decimal(
            next(pool for pool in released.json()["pools"] if pool["venue"] == "matchbook")["locked_capital"]
        ) == Decimal("0")

        reset = client.post("/paper/treasury/reset", json={"reason": "api reset"})
        assert reset.status_code == 200
        reset_id = reset.json()["session"]["session_id"]
        assert reset_id != body["session"]["session_id"]
        reset_mb = next(pool for pool in reset.json()["pools"] if pool["venue"] == "matchbook")
        assert Decimal(reset_mb["available_cash"]) == SEED
        assert Decimal(reset_mb["locked_capital"]) == 0

        noop = client.post(
            "/paper/treasury/unwind",
            json={
                "trade_id": "ptrade-api",
                "close_completed": False,
                "conditionally_releasable_native": {"GBP": "250"},
                "releases": [],
            },
        )
        assert noop.status_code == 200
    finally:
        app.dependency_overrides.clear()
        ledger.close()
