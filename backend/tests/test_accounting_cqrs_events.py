"""CQRS accounting event store, idempotent replay, and on-demand projections.

Paper modelled data only. Not live venue cash. Scanner path must not rebuild GL.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.adapters import from_treasury_event, hydrate_event_store
from sports_hedge.accounting.dimensions import AttributionScope, CapitalSource, StrategyBook
from sports_hedge.accounting.emission import PROVIDER_WRITES_FORBIDDEN, AccountingEventEmitter
from sports_hedge.accounting.event_store import InMemoryAccountingEventStore
from sports_hedge.accounting.events import (
    EVENT_SCHEMA_VERSION,
    AccountingEventPayload,
    AccountingEventType,
    DuplicateAccountingEventError,
    domain_event,
)
from sports_hedge.accounting.projections import (
    PROJECTION_VERSION,
    AccountingProjectionService,
    project_postings,
)
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import (
    PaperTreasuryEvent,
    PaperTreasuryEventType,
    TreasuryLockRequest,
)


NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)
SEED = Decimal("1000")
FX = Decimal("0.80")


def _ledger(path: Path | None = None) -> SqlitePaperLedger:
    return SqlitePaperLedger(
        path or ":memory:",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )


def _event(
    event_type: AccountingEventType,
    source_id: str,
    *,
    venue: VenueName = VenueName.MATCHBOOK,
    currency: str = "GBP",
    amount_native: Decimal = Decimal("10"),
    amount_gbp: Decimal | None = None,
    rate: Decimal | None = Decimal("1"),
    fx_source: str | None = "functional_currency",
    source: str = "test",
    **payload_extra: object,
) -> object:
    gbp = amount_gbp if amount_gbp is not None else amount_native * (rate or Decimal("1"))
    payload = AccountingEventPayload(
        venue=venue,
        currency=currency,
        amount_native=amount_native,
        amount_gbp=gbp,
        fx_rate_gbp_per_unit=rate if rate and rate > 0 else None,
        fx_source=fx_source,
        reason=str(payload_extra.pop("reason", "test fact")),
        **payload_extra,  # type: ignore[arg-type]
    )
    return domain_event(
        event_type=event_type,
        source=source,
        source_id=source_id,
        occurred_at=NOW,
        payload=payload,
    )


def test_duplicate_event_does_not_double_post() -> None:
    store = InMemoryAccountingEventStore()
    event = _event(AccountingEventType.CAPITAL_INTRODUCED, "seed-1", amount_native=SEED)
    first, created = store.append_idempotent(event)
    second, created_again = store.append_idempotent(event)
    assert created is True
    assert created_again is False
    assert first.event_id == second.event_id
    assert store.last_sequence() == 1
    projector = AccountingProjectionService(store)
    bundle = projector.rebuild()
    assert bundle.event_count == 1
    cash = next(
        item for item in bundle.general_ledger.accounts if item.account_code.startswith("ASSET:CASH:AVAILABLE")
    )
    assert cash.native_by_currency["GBP"] == SEED


def test_conflicting_duplicate_fails_closed() -> None:
    store = InMemoryAccountingEventStore()
    event = _event(AccountingEventType.CAPITAL_INTRODUCED, "seed-1", amount_native=SEED)
    store.append_idempotent(event)
    conflict = _event(AccountingEventType.CAPITAL_INTRODUCED, "seed-1", amount_native=Decimal("5"))
    with pytest.raises(DuplicateAccountingEventError, match="conflicting"):
        store.append_idempotent(conflict)


def test_replay_produces_identical_projection() -> None:
    store = InMemoryAccountingEventStore()
    events = [
        _event(AccountingEventType.CAPITAL_INTRODUCED, "mb", amount_native=SEED),
        _event(
            AccountingEventType.PAPER_CAPITAL_LOCK,
            "lock-1",
            amount_native=Decimal("100"),
            attribution=AttributionScope.STRATEGY,
            strategy_book=StrategyBook.ARBITRAGE,
            capital_source=CapitalSource.AUTO_POOL,
        ),
        _event(
            AccountingEventType.SETTLEMENT_PNL,
            "pnl-1",
            amount_native=Decimal("8"),
            attribution=AttributionScope.STRATEGY,
            strategy_book=StrategyBook.ARBITRAGE,
            capital_source=CapitalSource.AUTO_POOL,
        ),
        _event(
            AccountingEventType.FEE_POSTED,
            "fee-1",
            amount_native=Decimal("2"),
            attribution=AttributionScope.STRATEGY,
            strategy_book=StrategyBook.ARBITRAGE,
            capital_source=CapitalSource.AUTO_POOL,
        ),
    ]
    for item in events:
        store.append_idempotent(item)
    projector = AccountingProjectionService(store)
    first = projector.rebuild()
    second = projector.rebuild()
    assert first.canonical_dump() == second.canonical_dump()
    replayed = project_postings(store.list_in_order())
    again = project_postings(store.list_in_order())
    assert [item.model_dump(mode="json") for item in replayed] == [
        item.model_dump(mode="json") for item in again
    ]
    assert first.general_ledger.gbp_balanced is True
    assert first.balance_sheet.balanced is True
    assert first.management_reporting.data_kind == "management_reporting_read_model"
    assert first.general_ledger.data_kind == "statutory_accounting_projection"
    assert first.management_reporting.data_kind != first.general_ledger.data_kind


def test_capital_withdrawn_and_manual_adjustment_with_provenance() -> None:
    store = InMemoryAccountingEventStore()
    store.append_idempotent(_event(AccountingEventType.CAPITAL_INTRODUCED, "seed", amount_native=SEED))
    store.append_idempotent(
        _event(
            AccountingEventType.CAPITAL_WITHDRAWN,
            "withdraw",
            amount_native=Decimal("100"),
            reason="operator capital withdrawal",
        )
    )
    store.append_idempotent(
        _event(
            AccountingEventType.MANUAL_ADJUSTMENT,
            "adj",
            amount_native=Decimal("25"),
            reason="operator correction after bank rec",
            operator_reference="rec-2026-09-21",
            adjustment_kind="introduced",
        )
    )
    bundle = AccountingProjectionService(store).rebuild()
    available = next(
        item for item in bundle.general_ledger.accounts if "AVAILABLE:matchbook:GBP" in item.account_code
    )
    assert available.native_by_currency["GBP"] == Decimal("925")
    events = [item.event_type for item in store.list_in_order()]
    assert AccountingEventType.MANUAL_ADJUSTMENT in events
    manual = store.get("test", "adj")
    assert manual is not None
    assert manual.payload.operator_reference == "rec-2026-09-21"
    assert "operator correction" in manual.payload.reason


def test_treasury_transfer_and_fx_conversion_contract() -> None:
    store = InMemoryAccountingEventStore()
    store.append_idempotent(
        _event(
            AccountingEventType.CAPITAL_INTRODUCED,
            "poly-seed",
            venue=VenueName.POLYMARKET,
            currency="USD",
            amount_native=Decimal("1250"),
            rate=FX,
            fx_source="paper_demo_fx_snapshot",
        )
    )
    store.append_idempotent(
        _event(
            AccountingEventType.TREASURY_TRANSFER,
            "xfer-1",
            venue=VenueName.POLYMARKET,
            currency="USD",
            amount_native=Decimal("100"),
            rate=FX,
            fx_source="paper_demo_fx_snapshot",
            counterparty_venue=VenueName.KALSHI,
            counterparty_currency="USD",
            counterparty_amount_native=Decimal("100"),
            counterparty_amount_gbp=Decimal("80"),
        )
    )
    store.append_idempotent(
        _event(
            AccountingEventType.FX_CONVERSION,
            "fx-1",
            venue=VenueName.POLYMARKET,
            currency="USD",
            amount_native=Decimal("100"),
            amount_gbp=Decimal("80"),
            rate=FX,
            fx_source="executed_conversion",
            counterparty_venue=VenueName.MATCHBOOK,
            counterparty_currency="GBP",
            counterparty_amount_native=Decimal("79"),
            counterparty_amount_gbp=Decimal("79"),
        )
    )
    bundle = AccountingProjectionService(store).rebuild()
    assert bundle.general_ledger.gbp_balanced is True
    types = {item.event_type for item in store.list_in_order()}
    assert AccountingEventType.TREASURY_TRANSFER in types
    assert AccountingEventType.FX_CONVERSION in types
    fx_accounts = [item.account_code for item in bundle.general_ledger.accounts]
    assert any(code == "PNL:FX:REALISED" for code in fx_accounts)


def test_fx_revaluation_source_fact_and_gbp_delta() -> None:
    store = InMemoryAccountingEventStore()
    store.append_idempotent(
        _event(
            AccountingEventType.CAPITAL_INTRODUCED,
            "poly-seed",
            venue=VenueName.POLYMARKET,
            currency="USD",
            amount_native=Decimal("1250"),
            rate=FX,
            fx_source="ecb_eurofxref",
        )
    )
    source_fact = _event(
        AccountingEventType.FX_REVALUATION,
        "fx-fact",
        venue=VenueName.POLYMARKET,
        currency="USD",
        amount_native=Decimal("0"),
        amount_gbp=Decimal("0"),
        rate=FX,
        fx_source="ecb_eurofxref",
        fx_status="source_fact",
    )
    delta = _event(
        AccountingEventType.FX_REVALUATION,
        "fx-delta",
        venue=VenueName.POLYMARKET,
        currency="USD",
        amount_native=Decimal("0"),
        amount_gbp=Decimal("10"),
        rate=Decimal("0.808"),
        fx_source="ecb_eurofxref",
        fx_status="valuation",
        fx_source_date=date(2026, 9, 18),
    )
    store.append_idempotent(source_fact)
    store.append_idempotent(delta)
    bundle = AccountingProjectionService(store).rebuild()
    assert bundle.event_count == 3
    assert bundle.general_ledger.gbp_balanced is True
    assert any(item.account_code == "PNL:FX:UNREALISED" for item in bundle.general_ledger.accounts)


def test_operational_treasury_emits_and_projection_reconciles() -> None:
    ledger = _ledger()
    events = ledger.accounting_events.list_in_order()
    kinds = {item.event_type for item in events}
    assert AccountingEventType.CAPITAL_INTRODUCED in kinds
    snap = ledger.treasury.snapshot()
    ledger.treasury.lock_capital(
        [
            TreasuryLockRequest(
                venue=VenueName.MATCHBOOK,
                native_currency="GBP",
                amount_native=Decimal("150"),
                lock_id="lock-cqrs-1",
                trade_id="ptrade-cqrs",
                opportunity_id="opp-cqrs",
                fx_rate_gbp_per_unit=Decimal("1"),
            )
        ],
        occurred_at=NOW,
    )
    bundle = ledger.refresh_accounting_projections(rebuild=True)
    assert bundle.stale is False
    assert bundle.paper_only is True
    assert bundle.places_orders is False
    assert bundle.execution_enabled is False
    assert bundle.projection_version == PROJECTION_VERSION
    assert bundle.event_schema_version == EVENT_SCHEMA_VERSION
    assert bundle.reconciliation.ok is True
    matchbook = snap.pool(VenueName.MATCHBOOK, "GBP")
    after = ledger.treasury.snapshot()
    mb = after.pool(VenueName.MATCHBOOK, "GBP")
    assert mb.locked_capital == Decimal("150")
    assert mb.available_cash == matchbook.available_cash - Decimal("150")
    assert bundle.reconciliation.native_locked["matchbook/GBP"] == "150"
    assert bundle.management_reporting.data_kind != bundle.general_ledger.data_kind
    replay = AccountingProjectionService(ledger.accounting_events).rebuild()
    assert replay.general_ledger.model_dump(mode="json") == bundle.general_ledger.model_dump(mode="json")
    assert replay.balance_sheet.model_dump(mode="json") == bundle.balance_sheet.model_dump(mode="json")
    first = ledger.refresh_accounting_projections(rebuild=True)
    second = ledger.refresh_accounting_projections(rebuild=True)
    assert first.canonical_dump() == second.canonical_dump()
    ledger.close()


def test_hydrate_from_existing_facts_is_idempotent() -> None:
    ledger = _ledger()
    first = hydrate_event_store(ledger)
    second = hydrate_event_store(ledger)
    assert [item.event_id for item in first] == [item.event_id for item in second]
    counts = {}
    for item in second:
        counts[item.event_id] = counts.get(item.event_id, 0) + 1
    assert all(value == 1 for value in counts.values())
    ledger.close()


def test_reporting_failure_does_not_block_treasury_lock() -> None:
    ledger = _ledger()

    class BrokenStore:
        def append_idempotent(self, event):  # noqa: ANN001
            raise RuntimeError("projection store down")

    ledger.accounting_emitter.store = BrokenStore()
    entries = ledger.treasury.lock_capital(
        [
            TreasuryLockRequest(
                venue=VenueName.MATCHBOOK,
                native_currency="GBP",
                amount_native=Decimal("40"),
                lock_id="lock-stale-1",
                trade_id="ptrade-stale",
                opportunity_id="opp-stale",
                fx_rate_gbp_per_unit=Decimal("1"),
            )
        ],
        occurred_at=NOW,
    )
    assert entries
    snap = ledger.treasury.snapshot()
    assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == Decimal("40")
    assert ledger.accounting_emitter.last_emit_ok is False
    stale = AccountingProjectionService().rebuild()
    stale.stale = True
    assert stale.general_ledger.posting_count == 0
    ledger.close()


def test_unavailable_projection_is_explicitly_stale() -> None:
    from sports_hedge.accounting.projections import AccountingProjectionBundle

    bundle = AccountingProjectionBundle.unavailable("event store offline")
    assert bundle.stale is True
    assert bundle.places_orders is False
    assert bundle.reconciliation.ok is False


def test_scan_path_does_not_rebuild_accounting_projections() -> None:
    from sports_hedge.api import paper as paper_api
    from sports_hedge.application import live_refresh
    from sports_hedge.application import paper_scan

    tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
    refresh_src = inspect.getsource(live_refresh)
    scan_src = inspect.getsource(paper_scan)
    combined = tick_src + refresh_src + scan_src
    assert "refresh_accounting_projections" not in combined
    assert "AccountingProjectionService" not in combined
    assert "rebuild_projections" not in combined


def test_accounting_modules_forbid_provider_writes() -> None:
    from sports_hedge.accounting import adapters, emission, event_store, events, projections

    assert PROVIDER_WRITES_FORBIDDEN is True
    forbidden = (
        "place_order",
        "cancel_order",
        "sign_order",
        "submit_order",
        "wallet_sign",
        "execution_enabled=True",
    )
    for module in (adapters, emission, event_store, events, projections):
        src = inspect.getsource(module)
        for token in forbidden:
            assert token not in src


def test_adapter_maps_treasury_seed() -> None:
    treasury_event = PaperTreasuryEvent(
        event_id="seed-x",
        session_id="pts-1",
        pool_id="pts-1:matchbook/GBP",
        venue=VenueName.MATCHBOOK,
        native_currency="GBP",
        event_type=PaperTreasuryEventType.SEED,
        native_amount=SEED,
        occurred_at=NOW,
        source="paper_treasury_seed",
        source_id="pts-1:matchbook:GBP",
        reason="demo paper treasury seed",
        fx_rate_gbp_per_unit=Decimal("1"),
        fx_source="functional_currency",
    )
    mapped = from_treasury_event(treasury_event)
    assert mapped is not None
    assert mapped.event_type is AccountingEventType.CAPITAL_INTRODUCED
    assert mapped.event_id == "capital_introduced:paper_treasury_seed:pts-1:matchbook:GBP"


def test_emitter_swallows_store_errors() -> None:
    class Boom:
        def append_idempotent(self, event):  # noqa: ANN001
            raise RuntimeError("disk full")

    emitter = AccountingEventEmitter(Boom())
    event = _event(AccountingEventType.CAPITAL_INTRODUCED, "x", amount_native=Decimal("1"))
    assert emitter.emit(event) is False
    assert emitter.last_emit_ok is False


def test_accounting_api_is_on_demand_read_model(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(
        tmp_path / "cqrs.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
    )
    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    try:
        client = TestClient(app)
        events = client.get("/paper/accounting/events")
        assert events.status_code == 200
        body = events.json()
        assert body["paper_only"] is True
        assert body["places_orders"] is False
        assert body["count"] >= 3
        types = {item["event_type"] for item in body["events"]}
        assert "capital_introduced" in types
        projections = client.get("/paper/accounting/projections")
        assert projections.status_code == 200
        payload = projections.json()
        assert payload["paper_only"] is True
        assert payload["execution_enabled"] is False
        assert payload["general_ledger"]["data_kind"] == "statutory_accounting_projection"
        assert payload["management_reporting"]["data_kind"] == "management_reporting_read_model"
        assert payload["general_ledger"]["gbp_balanced"] is True
        assert payload["balance_sheet"]["balanced"] is True
    finally:
        app.dependency_overrides.clear()
        ledger.close()
