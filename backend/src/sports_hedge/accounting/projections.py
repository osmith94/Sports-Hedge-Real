"""On-demand GL, balance-sheet, reconciliation and management-reporting projections.

Rebuilt from the accounting event stream off the scanner critical path. Statutory
GL/balance-sheet is a separate read model from management strategy-book reporting.
Both consume the same append-only events. Never writes to venue providers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    EconomicAccount,
    PostingDimensions,
    PostingSide,
    cash_account,
)
from sports_hedge.accounting.event_store import AccountingEventStore, InMemoryAccountingEventStore
from sports_hedge.accounting.events import (
    EVENT_SCHEMA_VERSION,
    AccountingDomainEvent,
    AccountingEventPayload,
    AccountingEventType,
)
from sports_hedge.accounting.paper_journal import PaperJournalPosting
from sports_hedge.accounting.strategy_books import (
    DimensionedPosting,
    StrategyBookReport,
    StrategyBookReporter,
    missing_gbp_presentation_error,
    parse_cash_account,
)

PROJECTION_VERSION = 1
PROJECTION_BUNDLE_NAME = "accounting_bundle"
_ZERO = Decimal("0")


class AccountBalance(BaseModel):
    account_code: str
    amount_gbp: Decimal
    native_by_currency: dict[str, Decimal] = Field(default_factory=dict)


class GeneralLedgerProjection(BaseModel):
    projection_version: int = PROJECTION_VERSION
    accounts: list[AccountBalance] = Field(default_factory=list)
    posting_count: int = 0
    gbp_balanced: bool = True
    data_kind: str = "statutory_accounting_projection"


class BalanceSheetProjection(BaseModel):
    projection_version: int = PROJECTION_VERSION
    assets_gbp: Decimal = _ZERO
    equity_gbp: Decimal = _ZERO
    net_pnl_gbp: Decimal = _ZERO
    native_assets: dict[str, str] = Field(default_factory=dict)
    balanced: bool = True
    data_kind: str = "statutory_balance_sheet_projection"


class ProjectionReconciliation(BaseModel):
    ok: bool = True
    stale: bool = False
    mismatches: list[str] = Field(default_factory=list)
    native_available: dict[str, str] = Field(default_factory=dict)
    native_locked: dict[str, str] = Field(default_factory=dict)
    native_transit: dict[str, str] = Field(default_factory=dict)
    data_kind: str = "accounting_to_treasury_reconciliation"


class ManagementReportingProjection(BaseModel):
    projection_version: int = PROJECTION_VERSION
    by_strategy: dict[str, dict[str, str]] = Field(default_factory=dict)
    shared_treasury_pnl_gbp: str = "0"
    open_paper_trades: int = 0
    paper_fills: int = 0
    native_pool_balances: list[dict[str, str]] = Field(default_factory=list)
    data_kind: str = "management_reporting_read_model"


class AccountingProjectionBundle(BaseModel):
    projection_version: int = PROJECTION_VERSION
    event_schema_version: int = EVENT_SCHEMA_VERSION
    rebuilt_at: datetime
    last_event_id: str | None = None
    last_event_sequence: int = 0
    event_count: int = 0
    stale: bool = False
    unavailable_reason: str | None = None
    paper_only: bool = True
    places_orders: bool = False
    execution_enabled: bool = False
    data_kind: str = "paper_accounting_projection"
    note: str = (
        "PAPER MODE. On-demand accounting projection from immutable domain events. "
        "Not live venue cash. Native USD and GBP are never summed. "
        "Management reporting is a separate read model from the statutory GL."
    )
    general_ledger: GeneralLedgerProjection = Field(default_factory=GeneralLedgerProjection)
    balance_sheet: BalanceSheetProjection = Field(default_factory=BalanceSheetProjection)
    reconciliation: ProjectionReconciliation = Field(default_factory=ProjectionReconciliation)
    management_reporting: ManagementReportingProjection = Field(
        default_factory=ManagementReportingProjection
    )

    def canonical_dump(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"rebuilt_at"})

    @classmethod
    def unavailable(cls, reason: str) -> AccountingProjectionBundle:
        return cls(
            rebuilt_at=datetime.now(UTC),
            stale=True,
            unavailable_reason=reason,
            reconciliation=ProjectionReconciliation(ok=False, stale=True, mismatches=[reason]),
        )


class AccountingProjectionService:
    def __init__(self, store: AccountingEventStore | None = None) -> None:
        self.store = store if store is not None else InMemoryAccountingEventStore()
        self.reporter = StrategyBookReporter()

    def rebuild(self, events: Iterable[AccountingDomainEvent] | None = None) -> AccountingProjectionBundle:
        rows = list(events) if events is not None else self.store.list_in_order()
        postings = project_postings(rows)
        gl = _general_ledger(postings)
        sheet = _balance_sheet(postings)
        management = _management_reporting(self.reporter.report(postings), rows)
        last = rows[-1] if rows else None
        return AccountingProjectionBundle(
            rebuilt_at=datetime.now(UTC),
            last_event_id=None if last is None else last.event_id,
            last_event_sequence=last.sequence or 0 if last is not None else 0,
            event_count=len(rows),
            general_ledger=gl,
            balance_sheet=sheet,
            management_reporting=management,
        )

    def refresh(self) -> AccountingProjectionBundle:
        cached = _load_cached_bundle(self.store)
        if cached is not None and cached.last_event_sequence == self.store.last_sequence():
            if cached.projection_version == PROJECTION_VERSION:
                if cached.event_schema_version == EVENT_SCHEMA_VERSION:
                    return cached
        bundle = self.rebuild()
        _save_cached_bundle(self.store, bundle)
        return bundle


def project_postings(events: Iterable[AccountingDomainEvent]) -> list[DimensionedPosting]:
    """Deterministic statutory postings. Same events always yield the same postings."""

    facts: list[DimensionedPosting] = []
    for event in events:
        for index, posting in enumerate(_postings_for(event)):
            facts.append(
                posting.as_dimensioned(
                    journal_id=event.event_id,
                    posting_id=f"{event.event_id}:{index}",
                )
            )
    return facts


def _postings_for(event: AccountingDomainEvent) -> list[PaperJournalPosting]:
    payload = event.payload
    kind = event.event_type
    if kind is AccountingEventType.PAPER_TRADE_OPENED:
        return []
    if kind is AccountingEventType.PAPER_TRADE_FILL:
        return []
    if kind is AccountingEventType.FX_REVALUATION and payload.amount_gbp == 0:
        return []
    dims = _dims(payload)
    rate = _rate(payload)
    if kind is AccountingEventType.CAPITAL_INTRODUCED:
        return _cash_vs_equity(payload, dims, rate, introduce=True)
    if kind is AccountingEventType.CAPITAL_WITHDRAWN:
        return _cash_vs_equity(payload, dims, rate, introduce=False)
    if kind is AccountingEventType.MANUAL_ADJUSTMENT:
        introduce = payload.amount_native >= 0
        amount = abs(payload.amount_native)
        gbp = abs(payload.amount_gbp)
        adjusted = payload.model_copy(update={"amount_native": amount, "amount_gbp": gbp})
        return _cash_vs_equity(adjusted, dims, rate, introduce=introduce)
    if kind is AccountingEventType.PAPER_CAPITAL_LOCK:
        return _lock_postings(payload, dims, rate, lock=True)
    if kind is AccountingEventType.PAPER_CAPITAL_RELEASE:
        return _lock_postings(payload, dims, rate, lock=False)
    if kind is AccountingEventType.SETTLEMENT_PNL:
        return _pnl_postings(payload, dims, rate)
    if kind is AccountingEventType.FEE_POSTED:
        return _fee_reclass_postings(payload, dims, rate)
    if kind is AccountingEventType.TREASURY_TRANSFER:
        return _transfer_postings(payload, dims, rate)
    if kind is AccountingEventType.FX_CONVERSION:
        return _conversion_postings(payload, dims, rate)
    if kind is AccountingEventType.FX_REVALUATION:
        return _revaluation_postings(payload, dims, rate)
    return []


def _dims(payload: AccountingEventPayload) -> PostingDimensions:
    return PostingDimensions(
        attribution=payload.attribution,
        strategy_book=payload.strategy_book,
        capital_source=payload.capital_source,
        venue=payload.venue,
        currency=payload.currency or "GBP",
        opportunity_id=payload.opportunity_id,
        position_id=payload.trade_id or payload.lock_id,
    )


def _rate(payload: AccountingEventPayload) -> Decimal:
    if payload.currency == "GBP" or not payload.currency:
        return Decimal("1")
    if payload.fx_rate_gbp_per_unit is not None:
        return payload.fx_rate_gbp_per_unit
    raise missing_gbp_presentation_error(payload.currency)


def _post(
    account: str,
    side: PostingSide,
    native: Decimal,
    gbp: Decimal,
    dims: PostingDimensions,
    rate: Decimal,
) -> PaperJournalPosting:
    return PaperJournalPosting(
        account_code=account,
        side=side,
        amount_native=native,
        amount_gbp=gbp,
        fx_rate_gbp_per_unit=rate,
        dimensions=dims,
    )


def _cash_vs_equity(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
    *,
    introduce: bool,
) -> list[PaperJournalPosting]:
    assert payload.venue is not None
    cash = cash_account(payload.venue, payload.currency or "GBP", CashState.AVAILABLE)
    native = abs(payload.amount_native)
    gbp = abs(payload.amount_gbp) if payload.amount_gbp else native * rate
    debit_cash = introduce
    return [
        _post(cash, PostingSide.DEBIT if debit_cash else PostingSide.CREDIT, native, gbp, dims, rate),
        _post(
            EconomicAccount.EQUITY_PAPER_SEED.value,
            PostingSide.CREDIT if debit_cash else PostingSide.DEBIT,
            native,
            gbp,
            dims,
            rate,
        ),
    ]


def _lock_postings(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
    *,
    lock: bool,
) -> list[PaperJournalPosting]:
    assert payload.venue is not None
    currency = payload.currency or "GBP"
    locked = cash_account(payload.venue, currency, CashState.LOCKED)
    available = cash_account(payload.venue, currency, CashState.AVAILABLE)
    native = abs(payload.amount_native)
    gbp = abs(payload.amount_gbp) if payload.amount_gbp else native * rate
    if lock:
        return [
            _post(locked, PostingSide.DEBIT, native, gbp, dims, rate),
            _post(available, PostingSide.CREDIT, native, gbp, dims, rate),
        ]
    return [
        _post(available, PostingSide.DEBIT, native, gbp, dims, rate),
        _post(locked, PostingSide.CREDIT, native, gbp, dims, rate),
    ]


def _pnl_postings(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
) -> list[PaperJournalPosting]:
    assert payload.venue is not None
    native = payload.amount_native
    if native == 0 and payload.amount_gbp == 0:
        return []
    currency = payload.currency or "GBP"
    cash = cash_account(payload.venue, currency, CashState.AVAILABLE)
    gbp = payload.amount_gbp if payload.amount_gbp else native * rate
    profit = native > 0 or (native == 0 and gbp > 0)
    movement_native = abs(native)
    movement_gbp = abs(gbp)
    if profit:
        return [
            _post(cash, PostingSide.DEBIT, movement_native, movement_gbp, dims, rate),
            _post(EconomicAccount.PNL_BETTING.value, PostingSide.CREDIT, movement_native, movement_gbp, dims, rate),
        ]
    return [
        _post(EconomicAccount.PNL_BETTING.value, PostingSide.DEBIT, movement_native, movement_gbp, dims, rate),
        _post(cash, PostingSide.CREDIT, movement_native, movement_gbp, dims, rate),
    ]


def _fee_reclass_postings(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
) -> list[PaperJournalPosting]:
    native = abs(payload.amount_native)
    if native == 0:
        return []
    gbp = abs(payload.amount_gbp) if payload.amount_gbp else native * rate
    return [
        _post(EconomicAccount.PNL_VENUE_FEES.value, PostingSide.DEBIT, native, gbp, dims, rate),
        _post(EconomicAccount.PNL_BETTING.value, PostingSide.CREDIT, native, gbp, dims, rate),
    ]


def _transfer_postings(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
) -> list[PaperJournalPosting]:
    assert payload.venue is not None
    assert payload.counterparty_venue is not None
    src_currency = payload.currency or "GBP"
    dest_currency = payload.counterparty_currency or src_currency
    native = abs(payload.amount_native)
    gbp = abs(payload.amount_gbp) if payload.amount_gbp else native * rate
    dest_native = (
        abs(payload.counterparty_amount_native)
        if payload.counterparty_amount_native is not None
        else native
    )
    dest_gbp = (
        abs(payload.counterparty_amount_gbp)
        if payload.counterparty_amount_gbp is not None
        else gbp
    )
    dest_dims = dims.model_copy(
        update={"venue": payload.counterparty_venue, "currency": dest_currency}
    )
    dest_rate = dest_gbp / dest_native if dest_native else rate
    if dest_currency == "GBP":
        dest_rate = Decimal("1")
    return [
        _post(
            cash_account(payload.venue, src_currency, CashState.AVAILABLE),
            PostingSide.CREDIT,
            native,
            gbp,
            dims,
            rate,
        ),
        _post(
            cash_account(payload.counterparty_venue, dest_currency, CashState.AVAILABLE),
            PostingSide.DEBIT,
            dest_native,
            dest_gbp,
            dest_dims,
            dest_rate if dest_rate else rate,
        ),
    ]


def _conversion_postings(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
) -> list[PaperJournalPosting]:
    posts = _transfer_postings(payload, dims, rate)
    src_gbp = abs(payload.amount_gbp) if payload.amount_gbp else abs(payload.amount_native) * rate
    dest_gbp = (
        abs(payload.counterparty_amount_gbp)
        if payload.counterparty_amount_gbp is not None
        else src_gbp
    )
    delta = dest_gbp - src_gbp
    if delta == 0:
        return posts
    fx_currency = payload.counterparty_currency or payload.currency or "USD"
    fx_venue = payload.counterparty_venue or payload.venue
    if fx_currency == "GBP":
        fx_currency = payload.currency or "USD"
        fx_venue = payload.venue
    fx_dims = dims.model_copy(
        update={
            "attribution": AttributionScope.SHARED_UNALLOCATED,
            "strategy_book": None,
            "capital_source": CapitalSource.SHARED_UNALLOCATED,
            "currency": fx_currency,
            "venue": fx_venue,
        }
    )
    movement = abs(delta)
    fx_rate = payload.fx_rate_gbp_per_unit or rate
    if fx_currency == "GBP":
        fx_rate = Decimal("1")
        movement_native = movement
    else:
        movement_native = _ZERO
    posts.append(
        _post(
            EconomicAccount.PNL_FX_REALISED.value,
            PostingSide.CREDIT if delta > 0 else PostingSide.DEBIT,
            movement_native,
            movement,
            fx_dims,
            fx_rate,
        )
    )
    return posts


def _revaluation_postings(
    payload: AccountingEventPayload,
    dims: PostingDimensions,
    rate: Decimal,
) -> list[PaperJournalPosting]:
    movement = abs(payload.amount_gbp)
    if movement == 0:
        return []
    assert payload.venue is not None
    state = payload.cash_state or CashState.AVAILABLE
    gain = payload.amount_gbp > 0
    fx_dims = dims.model_copy(
        update={
            "attribution": AttributionScope.SHARED_UNALLOCATED,
            "strategy_book": None,
            "capital_source": CapitalSource.SHARED_UNALLOCATED,
        }
    )
    return [
        _post(
            cash_account(payload.venue, payload.currency or "USD", state),
            PostingSide.DEBIT if gain else PostingSide.CREDIT,
            _ZERO,
            movement,
            fx_dims,
            rate,
        ),
        _post(
            EconomicAccount.PNL_FX_UNREALISED.value,
            PostingSide.CREDIT if gain else PostingSide.DEBIT,
            _ZERO,
            movement,
            fx_dims,
            rate,
        ),
    ]


def _general_ledger(postings: list[DimensionedPosting]) -> GeneralLedgerProjection:
    buckets: dict[str, list[DimensionedPosting]] = defaultdict(list)
    for posting in postings:
        buckets[posting.account_code].append(posting)
    accounts: list[AccountBalance] = []
    signed_total = _ZERO
    for code in sorted(buckets):
        group = buckets[code]
        gbp = sum((item.signed_gbp for item in group), _ZERO)
        signed_total += gbp
        native: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for item in group:
            native[item.dimensions.currency] += item.signed_native
        accounts.append(
            AccountBalance(
                account_code=code,
                amount_gbp=gbp,
                native_by_currency={key: value for key, value in sorted(native.items())},
            )
        )
    return GeneralLedgerProjection(
        accounts=accounts,
        posting_count=len(postings),
        gbp_balanced=signed_total == _ZERO,
    )


def _balance_sheet(postings: list[DimensionedPosting]) -> BalanceSheetProjection:
    assets = _ZERO
    equity = _ZERO
    pnl = _ZERO
    native_assets: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for posting in postings:
        parsed = parse_cash_account(posting.account_code)
        if parsed is not None:
            assets += posting.signed_gbp
            identity = f"{parsed[1].value}/{parsed[2]}"
            native_assets[identity] += posting.signed_native
        elif posting.account_code == EconomicAccount.EQUITY_PAPER_SEED.value:
            equity -= posting.signed_gbp
        elif posting.account_code.startswith("PNL:"):
            pnl -= posting.signed_gbp
    native_text = {key: str(value) for key, value in sorted(native_assets.items())}
    balanced = assets == equity + pnl
    return BalanceSheetProjection(
        assets_gbp=assets,
        equity_gbp=equity,
        net_pnl_gbp=pnl,
        native_assets=native_text,
        balanced=balanced,
    )


def _management_reporting(
    report: StrategyBookReport,
    events: list[AccountingDomainEvent],
) -> ManagementReportingProjection:
    by_strategy: dict[str, dict[str, str]] = {}
    for book, totals in report.by_strategy.items():
        by_strategy[book.value] = {
            "realised_betting_pnl_gbp": str(totals.realised_betting_pnl_gbp),
            "venue_fees_gbp": str(totals.venue_fees_gbp),
            "allocated_realised_fx_gbp": str(totals.allocated_realised_fx_gbp),
            "allocated_unrealised_fx_gbp": str(totals.allocated_unrealised_fx_gbp),
            "net_pnl_gbp": str(totals.net_pnl_gbp),
            "available_gbp": str(totals.capital.available_gbp),
            "locked_gbp": str(totals.capital.locked_gbp),
        }
    native_rows = [
        {
            "pool": f"{row.pool.venue.value}/{row.pool.currency}",
            "state": row.state.value,
            "amount_native": str(row.amount_native),
            "amount_gbp": str(row.amount_gbp),
            "attribution": row.attribution.value,
            "capital_source": row.capital_source.value,
        }
        for row in report.native_pool_balances
    ]
    open_trades = {
        event.payload.trade_id
        for event in events
        if event.event_type is AccountingEventType.PAPER_TRADE_OPENED and event.payload.trade_id
    }
    fills = sum(1 for event in events if event.event_type is AccountingEventType.PAPER_TRADE_FILL)
    return ManagementReportingProjection(
        by_strategy=by_strategy,
        shared_treasury_pnl_gbp=str(report.shared_treasury.net_pnl_gbp),
        open_paper_trades=len(open_trades),
        paper_fills=fills,
        native_pool_balances=native_rows,
    )


def reconcile_projection_to_treasury(
    bundle: AccountingProjectionBundle,
    ledger: Any,
) -> AccountingProjectionBundle:
    """Compare projected native cash to operational Treasury pools. Read-only."""

    mismatches: list[str] = []
    native_available: dict[str, str] = {}
    native_locked: dict[str, str] = {}
    native_transit: dict[str, str] = {}
    try:
        snapshot = ledger.treasury.snapshot(event_limit=1)
    except Exception as exc:  # noqa: BLE001 — reporting must not raise into callers
        bundle.reconciliation = ProjectionReconciliation(
            ok=False,
            stale=True,
            mismatches=[f"treasury_unavailable:{exc}"],
        )
        bundle.stale = True
        bundle.unavailable_reason = str(exc)
        return bundle
    assets = {
        (row.account_code): row
        for row in bundle.general_ledger.accounts
        if row.account_code.startswith("ASSET:CASH:")
    }
    for pool in snapshot.pools:
        identity = f"{pool.venue.value}/{pool.native_currency}"
        available_code = cash_account(pool.venue, pool.native_currency, CashState.AVAILABLE)
        locked_code = cash_account(pool.venue, pool.native_currency, CashState.LOCKED)
        transit_code = cash_account(pool.venue, pool.native_currency, CashState.TRANSIT)
        projected_available = assets.get(available_code)
        projected_locked = assets.get(locked_code)
        projected_transit = assets.get(transit_code)
        avail = (
            projected_available.native_by_currency.get(pool.native_currency, _ZERO)
            if projected_available
            else _ZERO
        )
        locked = (
            projected_locked.native_by_currency.get(pool.native_currency, _ZERO)
            if projected_locked
            else _ZERO
        )
        transit = (
            projected_transit.native_by_currency.get(pool.native_currency, _ZERO)
            if projected_transit
            else _ZERO
        )
        native_available[identity] = str(avail)
        native_locked[identity] = str(locked)
        native_transit[identity] = str(transit)
        if avail != pool.available_cash:
            mismatches.append(
                f"available_mismatch:{identity} projection={avail} treasury={pool.available_cash}"
            )
        if locked != pool.locked_capital:
            mismatches.append(
                f"locked_mismatch:{identity} projection={locked} treasury={pool.locked_capital}"
            )
    if not bundle.general_ledger.gbp_balanced:
        mismatches.append("statutory_gl_unbalanced_gbp")
    if not bundle.balance_sheet.balanced:
        mismatches.append("balance_sheet_unbalanced")
    bundle.reconciliation = ProjectionReconciliation(
        ok=not mismatches,
        stale=False,
        mismatches=mismatches,
        native_available=native_available,
        native_locked=native_locked,
        native_transit=native_transit,
    )
    return bundle


def _load_cached_bundle(store: Any) -> AccountingProjectionBundle | None:
    loader = getattr(store, "load_projection_snapshot", None)
    if loader is None:
        return None
    row = loader(PROJECTION_BUNDLE_NAME, PROJECTION_VERSION)
    if row is None:
        return None
    payload = dict(row["payload"])
    payload["rebuilt_at"] = row["rebuilt_at"]
    return AccountingProjectionBundle.model_validate(payload)


def _save_cached_bundle(store: Any, bundle: AccountingProjectionBundle) -> None:
    saver = getattr(store, "save_projection_snapshot", None)
    if saver is None:
        return
    saver(
        projection_name=PROJECTION_BUNDLE_NAME,
        projection_version=bundle.projection_version,
        event_schema_version=bundle.event_schema_version,
        last_event_sequence=bundle.last_event_sequence,
        last_event_id=bundle.last_event_id,
        event_count=bundle.event_count,
        rebuilt_at=bundle.rebuilt_at,
        payload=bundle.model_dump(mode="json"),
    )
