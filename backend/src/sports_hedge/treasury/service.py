"""Authoritative paper treasury transitions on the Step 5 SQLite ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sports_hedge.accounting.dimensions import CapitalSource, PostingSide, parse_capital_source
from sports_hedge.accounting.paper_journal import (
    DataProvenance,
    DuplicateJournalError,
    PaperJournalEntry,
    cash_lock_postings,
    seed_funding_postings,
    settlement_leg_postings,
    unwind_close_postings,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.settlement import PaperSettlementComputation
from sports_hedge.paper.trades import PaperSettlementRequest, PaperTrade
from sports_hedge.persistence.paper_ledger import SerializedLedgerBound
from sports_hedge.treasury.models import (
    PaperTreasuryEvent,
    PaperTreasuryEventType,
    PaperTreasuryPoolState,
    PaperTreasurySession,
    PaperTreasurySnapshot,
    TreasuryLockRequest,
    UnwindReleaseLeg,
    ValidatedUnwindResult,
)

DEMO_SEED_GBP = Decimal("1000")
DEFAULT_PAPER_FX_USD = Decimal("0.80")
DEFAULT_PAPER_FX_SOURCE = "paper_demo_fx_snapshot"


class PaperTreasuryError(ValueError):
    """Fail-closed paper treasury mutation."""


class PaperTreasuryService(SerializedLedgerBound):
    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._connection = ledger._connection

    def snapshot(self, *, event_limit: int = 50) -> PaperTreasurySnapshot:
        session = self.active_session()
        pools = self._load_pools(session.session_id) if session else []
        events = self.list_events(limit=event_limit, session_id=session.session_id if session else None)
        return PaperTreasurySnapshot(
            session=session,
            pools=pools,
            events=events,
        )

    def active_session(self) -> PaperTreasurySession | None:
        row = self._connection.execute(
            "SELECT * FROM paper_treasury_sessions WHERE active = 1 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return _session_from_row(row) if row else None

    def ensure_demo_session(
        self,
        *,
        seed_gbp: Decimal = DEMO_SEED_GBP,
        usd_gbp_per_unit: Decimal = DEFAULT_PAPER_FX_USD,
        fx_source: str = DEFAULT_PAPER_FX_SOURCE,
        fx_as_of: datetime | None = None,
        include_kalshi: bool = True,
        reason: str = "demo paper treasury seed",
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
        now: datetime | None = None,
    ) -> PaperTreasurySnapshot:
        if self.active_session() is not None:
            return self.snapshot()
        return self.open_demo_session(
            seed_gbp=seed_gbp,
            usd_gbp_per_unit=usd_gbp_per_unit,
            fx_source=fx_source,
            fx_as_of=fx_as_of,
            include_kalshi=include_kalshi,
            reason=reason,
            provenance=provenance,
            now=now,
        )

    def reset_demo_session(
        self,
        *,
        seed_gbp: Decimal = DEMO_SEED_GBP,
        usd_gbp_per_unit: Decimal = DEFAULT_PAPER_FX_USD,
        fx_source: str = DEFAULT_PAPER_FX_SOURCE,
        fx_as_of: datetime | None = None,
        include_kalshi: bool = True,
        reason: str = "explicit paper treasury demo reset",
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
        now: datetime | None = None,
    ) -> PaperTreasurySnapshot:
        """Close the active session and open a new seeded book. History is retained."""

        return self.open_demo_session(
            seed_gbp=seed_gbp,
            usd_gbp_per_unit=usd_gbp_per_unit,
            fx_source=fx_source,
            fx_as_of=fx_as_of,
            include_kalshi=include_kalshi,
            reason=reason,
            provenance=provenance,
            now=now,
        )

    def open_demo_session(
        self,
        *,
        seed_gbp: Decimal,
        usd_gbp_per_unit: Decimal,
        fx_source: str,
        fx_as_of: datetime | None = None,
        include_kalshi: bool = True,
        reason: str,
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
        now: datetime | None = None,
    ) -> PaperTreasurySnapshot:
        if seed_gbp <= 0:
            raise PaperTreasuryError("invalid_seed_amount")
        if usd_gbp_per_unit <= 0:
            raise PaperTreasuryError("missing_fx_snapshot")
        if not fx_source.strip():
            raise PaperTreasuryError("missing_fx_provenance")
        occurred = now or datetime.now(UTC)
        fx_at = fx_as_of or occurred
        usd_native = (seed_gbp / usd_gbp_per_unit).quantize(Decimal("0.00000001"))
        specs: list[tuple[VenueName, str, Decimal, Decimal, str]] = [
            (VenueName.MATCHBOOK, "GBP", seed_gbp, Decimal("1"), "functional_currency"),
            (VenueName.POLYMARKET, "USD", usd_native, usd_gbp_per_unit, fx_source),
        ]
        if include_kalshi:
            specs.append((VenueName.KALSHI, "USD", usd_native, usd_gbp_per_unit, fx_source))

        with self._ledger.transaction():
            current = self.active_session()
            if current is not None:
                self._assert_reset_allowed(current)
                self._close_session(current, occurred_at=occurred, reason=reason)
            session_id = f"pts-{uuid4()}"
            self._connection.execute(
                """
                INSERT INTO paper_treasury_sessions (
                    session_id, opened_at, closed_at, active, provenance, reason,
                    seed_gbp, fx_rate_usd_gbp, fx_source, fx_as_of, include_kalshi
                ) VALUES (?, ?, NULL, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    occurred.isoformat(),
                    provenance.value,
                    reason,
                    str(seed_gbp),
                    str(usd_gbp_per_unit),
                    fx_source,
                    fx_at.isoformat(),
                    1 if include_kalshi else 0,
                ),
            )
            self._append_session_event(
                session_id=session_id,
                event_type=PaperTreasuryEventType.SESSION_OPEN,
                occurred_at=occurred,
                reason=reason,
                fx_rate=usd_gbp_per_unit,
                fx_source=fx_source,
            )
            opportunity_id = f"treasury:{session_id}"
            for venue, currency, native, rate, source in specs:
                pool_id = _pool_id(session_id, venue, currency)
                gbp = native * rate
                self._connection.execute(
                    """
                    INSERT INTO paper_treasury_pools (
                        pool_id, session_id, venue, native_currency, seed_native,
                        available_cash, locked_capital, realised_pnl_native,
                        cumulative_fees_native, fx_rate_gbp_per_unit, fx_source, fx_as_of
                    ) VALUES (?, ?, ?, ?, ?, ?, '0', '0', '0', ?, ?, ?)
                    """,
                    (
                        pool_id,
                        session_id,
                        venue.value,
                        currency,
                        str(native),
                        str(native),
                        str(rate),
                        source,
                        fx_at.isoformat(),
                    ),
                )
                journal, _created = self._ledger.journal.append_idempotent(
                    PaperJournalEntry(
                        source="paper_treasury_seed",
                        source_id=f"{session_id}:{venue.value}:{currency}",
                        occurred_at=occurred,
                        description=f"PAPER-ONLY treasury seed {venue.value} {currency}",
                        opportunity_id=opportunity_id,
                        provenance=provenance,
                        postings=seed_funding_postings(
                            venue=venue,
                            currency=currency,
                            amount_native=native,
                            amount_gbp=gbp,
                            fx_rate_gbp_per_unit=rate,
                            opportunity_id=opportunity_id,
                        ),
                    )
                )
                self._insert_event(
                    PaperTreasuryEvent(
                        event_id=str(uuid4()),
                        session_id=session_id,
                        pool_id=pool_id,
                        venue=venue,
                        native_currency=currency,
                        event_type=PaperTreasuryEventType.SEED,
                        native_amount=native,
                        occurred_at=occurred,
                        opportunity_id=opportunity_id,
                        source="paper_treasury_seed",
                        source_id=f"{session_id}:{venue.value}:{currency}",
                        reason=reason,
                        fx_rate_gbp_per_unit=rate,
                        fx_source=source,
                        journal_id=journal.journal_id,
                    )
                )
        return self.snapshot()

    def set_available_amounts(
        self,
        amounts: dict[VenueName, Decimal],
        *,
        reason: str = "operator paper treasury edit",
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
        now: datetime | None = None,
    ) -> PaperTreasurySnapshot:
        """Set native available cash per venue. Fail-closed while locks/trades are open."""

        if any(value < 0 for value in amounts.values()):
            raise PaperTreasuryError("invalid_seed_amount")
        self.assert_mutation_safe()
        session = self.active_session()
        if session is None:
            self.ensure_demo_session(reason=reason, provenance=provenance, now=now)
            session = self.active_session()
        if session is None:
            raise PaperTreasuryError("stale_unknown_pool")
        occurred = now or datetime.now(UTC)
        with self._ledger.transaction():
            self._assert_reset_allowed(session)
            opportunity_id = f"treasury:{session.session_id}:adjust"
            for venue, native in amounts.items():
                if venue is VenueName.SMARKETS:
                    continue
                pool = self._pool_row(session.session_id, venue, "GBP" if venue is VenueName.MATCHBOOK else "USD")
                current = Decimal(pool["available_cash"])
                delta = native - current
                if delta == 0:
                    continue
                rate = Decimal(pool["fx_rate_gbp_per_unit"] or "0")
                source = pool["fx_source"]
                if rate <= 0:
                    raise PaperTreasuryError("missing_fx_snapshot")
                gbp = abs(delta) * rate
                postings = seed_funding_postings(
                    venue=venue,
                    currency=pool["native_currency"],
                    amount_native=abs(delta),
                    amount_gbp=gbp,
                    fx_rate_gbp_per_unit=rate,
                    opportunity_id=opportunity_id,
                )
                if delta < 0:
                    postings = [
                        item.model_copy(
                            update={
                                "side": PostingSide.CREDIT
                                if item.side is PostingSide.DEBIT
                                else PostingSide.DEBIT
                            }
                        )
                        for item in postings
                    ]
                journal, _created = self._ledger.journal.append_idempotent(
                    PaperJournalEntry(
                        source="paper_treasury_adjust",
                        source_id=f"{session.session_id}:{venue.value}:{occurred.isoformat()}:{native}",
                        occurred_at=occurred,
                        description=f"PAPER-ONLY treasury adjust {venue.value}",
                        opportunity_id=opportunity_id,
                        provenance=provenance,
                        postings=postings,
                    )
                )
                self._connection.execute(
                    """
                    UPDATE paper_treasury_pools
                    SET available_cash = ?, seed_native = ?
                    WHERE pool_id = ?
                    """,
                    (str(native), str(native), pool["pool_id"]),
                )
                self._insert_event(
                    PaperTreasuryEvent(
                        event_id=str(uuid4()),
                        session_id=session.session_id,
                        pool_id=pool["pool_id"],
                        venue=venue,
                        native_currency=pool["native_currency"],
                        event_type=PaperTreasuryEventType.CORRECTION,
                        native_amount=delta,
                        occurred_at=occurred,
                        opportunity_id=opportunity_id,
                        source="paper_treasury_adjust",
                        source_id=f"{session.session_id}:{venue.value}:{occurred.isoformat()}",
                        reason=reason,
                        fx_rate_gbp_per_unit=rate,
                        fx_source=source,
                        journal_id=journal.journal_id,
                    )
                )
        return self.snapshot()

    def lock_capital(
        self,
        requests: list[TreasuryLockRequest],
        *,
        occurred_at: datetime | None = None,
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
    ) -> list[PaperJournalEntry]:
        if not requests:
            raise PaperTreasuryError("empty_lock_request")
        occurred = occurred_at or datetime.now(UTC)
        try:
            with self._ledger.transaction():
                session = self._require_session()
                entries: list[PaperJournalEntry] = []
                for request in requests:
                    entries.append(
                        self._lock_one(session, request, occurred_at=occurred, provenance=provenance)
                    )
                return entries
        except Exception:
            self._ledger.reload_journal()
            raise

    def apply_settlement(
        self,
        trade: PaperTrade,
        computation: PaperSettlementComputation,
        request: PaperSettlementRequest,
        *,
        settled_at: datetime,
    ) -> PaperJournalEntry:
        source_id = f"settle:{trade.trade_id}:{request.source}:{request.source_id}"
        existing = self._ledger.journal.get("paper_settlement", source_id)
        if existing is not None:
            if any(
                self._event_exists(
                    "paper_settlement",
                    f"{source_id}:{leg.fill_id or leg.venue}:{leg.outcome}",
                )
                for leg in computation.legs
            ):
                return existing
        fill_ids = [leg.fill_id for leg in computation.legs]
        if any(not item for item in fill_ids):
            raise PaperTreasuryError("missing_lock_identity")
        if len(fill_ids) != len(set(fill_ids)):
            raise PaperTreasuryError("ambiguous_lock_identity")
        postings = []
        for leg in computation.legs:
            postings.extend(
                settlement_leg_postings(
                    venue=VenueName(leg.venue),
                    currency=leg.currency,
                    stake_native=leg.filled_stake,
                    net_payoff_native=leg.net_payoff,
                    venue_fee_native=leg.venue_fee,
                    amount_gbp_per_native=leg.fx_rate_gbp_per_unit,
                    opportunity_id=trade.opportunity_id,
                    capital_source=CapitalSource(leg.capital_source),
                    canonical_event_id=trade.canonical_event_id,
                    position_id=trade.trade_id,
                    won=leg.won,
                )
            )
        entry = PaperJournalEntry(
            source="paper_settlement",
            source_id=source_id,
            occurred_at=settled_at,
            description=(
                f"PAPER-ONLY settlement outcome={request.winning_outcome} "
                f"source={request.source}:{request.source_id}"
            ),
            opportunity_id=trade.opportunity_id,
            trade_id=trade.trade_id,
            provenance=request.provenance,
            postings=postings,
        )
        try:
            with self._ledger.transaction():
                posted, created = self._ledger.journal.append_idempotent(entry)
                if created:
                    for leg in computation.legs:
                        self._apply_leg_settlement(
                            trade,
                            venue=VenueName(leg.venue),
                            currency=leg.currency,
                            fill_id=leg.fill_id,
                            stake_native=leg.filled_stake,
                            net_payoff_native=leg.net_payoff,
                            venue_fee_native=leg.venue_fee,
                            realised_pnl_native=leg.native_pnl,
                            fx_rate=leg.fx_rate_gbp_per_unit,
                            occurred_at=settled_at,
                            source="paper_settlement",
                            source_id=f"{source_id}:{leg.fill_id}:{leg.outcome}",
                            journal_id=posted.journal_id,
                            reason=f"settlement {request.winning_outcome}",
                        )
                return posted
        except DuplicateJournalError as exc:
            self._ledger.reload_journal()
            raise PaperTreasuryError("conflicting_journal_facts") from exc
        except Exception:
            self._ledger.reload_journal()
            raise

    def post_unwind(self, result: ValidatedUnwindResult, *, now: datetime | None = None) -> list[PaperJournalEntry]:
        if not result.close_completed:
            return []
        if not result.releases:
            return []
        occurred = now or datetime.now(UTC)
        source_id = result.source_id or f"unwind:{result.trade_id}"
        entries: list[PaperJournalEntry] = []
        try:
            with self._ledger.transaction():
                session = self._require_session()
                for leg in result.releases:
                    lock = self._lock_row(leg.lock_id)
                    if lock is None:
                        raise PaperTreasuryError("unknown_lock")
                    if lock["session_id"] != session.session_id:
                        raise PaperTreasuryError("lock_not_in_active_session")
                    if (lock["trade_id"] or None) != result.trade_id:
                        raise PaperTreasuryError("unknown_trade_lock")
                    remaining = Decimal(lock["locked_native"]) - Decimal(lock["released_native"])
                    if VenueName(lock["venue"]) is not leg.venue:
                        raise PaperTreasuryError("venue_mismatch")
                    if lock["native_currency"] != leg.native_currency.upper():
                        raise PaperTreasuryError("currency_mismatch")
                    capital_source = parse_capital_source(
                        _row_value(lock, "capital_source", "AUTO_POOL")
                    )
                    journal_source_id = f"{source_id}:{leg.lock_id}"
                    expected = PaperJournalEntry(
                        source=result.source,
                        source_id=journal_source_id,
                        occurred_at=occurred,
                        description="PAPER-ONLY validated unwind capital release",
                        opportunity_id=result.opportunity_id or result.trade_id,
                        trade_id=result.trade_id,
                        postings=unwind_close_postings(
                            venue=leg.venue,
                            currency=leg.native_currency,
                            locked_native=leg.amount_native,
                            realised_pnl_native=leg.realised_pnl_native,
                            fee_native=leg.fee_native,
                            amount_gbp_per_native=leg.fx_rate_gbp_per_unit,
                            opportunity_id=result.opportunity_id or result.trade_id,
                            capital_source=capital_source,
                            position_id=result.trade_id,
                        ),
                    )
                    existing = self._ledger.journal.get(result.source, journal_source_id)
                    if remaining == 0 and existing is not None:
                        if not existing.facts_match(expected):
                            raise PaperTreasuryError("conflicting_journal_facts")
                        entries.append(existing)
                        continue
                    if remaining == 0:
                        raise PaperTreasuryError("release_exceeds_lock")
                    if existing is not None:
                        raise PaperTreasuryError("conflicting_journal_facts")
                    if leg.amount_native > remaining:
                        raise PaperTreasuryError("release_exceeds_lock")
                    pool = self._pool_row(session.session_id, leg.venue, leg.native_currency)
                    self._release_locked(
                        pool,
                        amount=leg.amount_native,
                        into_available=leg.amount_native + leg.realised_pnl_native,
                    )
                    pool = self._pool_row(session.session_id, leg.venue, leg.native_currency)
                    self._add_realised(pool, leg.realised_pnl_native, leg.fee_native)
                    self._bump_lock_released(leg.lock_id, leg.amount_native)
                    posted, created = self._ledger.journal.append_idempotent(expected)
                    entries.append(posted)
                    if created:
                        self._insert_event(
                            PaperTreasuryEvent(
                                event_id=str(uuid4()),
                                session_id=session.session_id,
                                pool_id=pool["pool_id"],
                                venue=leg.venue,
                                native_currency=leg.native_currency.upper(),
                                event_type=PaperTreasuryEventType.RELEASE,
                                native_amount=leg.amount_native,
                                occurred_at=occurred,
                                trade_id=result.trade_id,
                                opportunity_id=result.opportunity_id,
                                lock_id=leg.lock_id,
                                source=result.source,
                                source_id=f"{source_id}:{leg.lock_id}",
                                reason=result.reason,
                                fx_rate_gbp_per_unit=leg.fx_rate_gbp_per_unit,
                                fx_source=session.fx_source,
                                journal_id=posted.journal_id,
                            )
                        )
                        if leg.realised_pnl_native != 0:
                            self._insert_event(
                                PaperTreasuryEvent(
                                    event_id=str(uuid4()),
                                    session_id=session.session_id,
                                    pool_id=pool["pool_id"],
                                    venue=leg.venue,
                                    native_currency=leg.native_currency.upper(),
                                    event_type=PaperTreasuryEventType.REALISED_PNL,
                                    native_amount=leg.realised_pnl_native,
                                    occurred_at=occurred,
                                    trade_id=result.trade_id,
                                    lock_id=leg.lock_id,
                                    source=result.source,
                                    source_id=f"{source_id}:{leg.lock_id}:pnl",
                                    reason=result.reason,
                                    fx_rate_gbp_per_unit=leg.fx_rate_gbp_per_unit,
                                    fx_source=session.fx_source,
                                    journal_id=posted.journal_id,
                                )
                            )
                        if leg.fee_native > 0:
                            self._insert_event(
                                PaperTreasuryEvent(
                                    event_id=str(uuid4()),
                                    session_id=session.session_id,
                                    pool_id=pool["pool_id"],
                                    venue=leg.venue,
                                    native_currency=leg.native_currency.upper(),
                                    event_type=PaperTreasuryEventType.FEE,
                                    native_amount=leg.fee_native,
                                    occurred_at=occurred,
                                    trade_id=result.trade_id,
                                    lock_id=leg.lock_id,
                                    source=result.source,
                                    source_id=f"{source_id}:{leg.lock_id}:fee",
                                    reason=result.reason,
                                    fx_rate_gbp_per_unit=leg.fx_rate_gbp_per_unit,
                                    fx_source=session.fx_source,
                                    journal_id=posted.journal_id,
                                )
                            )
            return entries
        except Exception:
            self._ledger.reload_journal()
            raise

    def apply_fx_snapshot(
        self,
        *,
        usd_gbp_per_unit: Decimal,
        fx_source: str,
        fx_as_of: datetime | None = None,
    ) -> PaperTreasurySnapshot:
        if usd_gbp_per_unit <= 0 or not fx_source.strip():
            raise PaperTreasuryError("missing_fx_snapshot")
        occurred = fx_as_of or datetime.now(UTC)
        with self._ledger.transaction():
            session = self._require_session()
            self._connection.execute(
                """
                UPDATE paper_treasury_sessions
                SET fx_rate_usd_gbp = ?, fx_source = ?, fx_as_of = ?
                WHERE session_id = ?
                """,
                (str(usd_gbp_per_unit), fx_source, occurred.isoformat(), session.session_id),
            )
            for pool in self._load_pool_rows(session.session_id):
                venue = VenueName(pool["venue"])
                currency = pool["native_currency"]
                rate = Decimal("1") if currency == "GBP" else usd_gbp_per_unit
                source = "functional_currency" if currency == "GBP" else fx_source
                self._connection.execute(
                    """
                    UPDATE paper_treasury_pools
                    SET fx_rate_gbp_per_unit = ?, fx_source = ?, fx_as_of = ?
                    WHERE pool_id = ?
                    """,
                    (str(rate), source, occurred.isoformat(), pool["pool_id"]),
                )
                self._insert_event(
                    PaperTreasuryEvent(
                        event_id=str(uuid4()),
                        session_id=session.session_id,
                        pool_id=pool["pool_id"],
                        venue=venue,
                        native_currency=currency,
                        event_type=PaperTreasuryEventType.FX_CARRYING_SNAPSHOT,
                        native_amount=Decimal("0"),
                        occurred_at=occurred,
                        source="paper_treasury_fx",
                        source_id=f"{session.session_id}:{pool['pool_id']}:{occurred.isoformat()}",
                        reason="GBP carrying value snapshot; native balances unchanged",
                        fx_rate_gbp_per_unit=rate,
                        fx_source=source,
                    )
                )
        return self.snapshot()

    def list_events(
        self,
        *,
        limit: int = 50,
        session_id: str | None = None,
    ) -> list[PaperTreasuryEvent]:
        if session_id is None:
            rows = self._connection.execute(
                "SELECT * FROM paper_treasury_events ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM paper_treasury_events
                WHERE session_id = ?
                ORDER BY rowid DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def release_open_locks_for_demo_reset(
        self,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> list[PaperJournalEntry]:
        """Return remaining locked cash at zero betting P&L. Not a market settlement."""

        session = self.active_session()
        if session is None:
            return []
        rows = list(
            self._connection.execute(
                """
                SELECT * FROM paper_treasury_locks
                WHERE session_id = ? AND status = 'open'
                """,
                (session.session_id,),
            )
        )
        grouped: dict[str, list[UnwindReleaseLeg]] = {}
        occurred = now or datetime.now(UTC)
        for row in rows:
            remaining = Decimal(row["locked_native"]) - Decimal(row["released_native"])
            if remaining <= 0:
                continue
            pool = self._pool_row(session.session_id, VenueName(row["venue"]), row["native_currency"])
            rate = Decimal(pool["fx_rate_gbp_per_unit"])
            trade_id = row["trade_id"] or "demo-reset"
            grouped.setdefault(trade_id, []).append(
                UnwindReleaseLeg(
                    venue=VenueName(row["venue"]),
                    native_currency=row["native_currency"],
                    lock_id=row["lock_id"],
                    amount_native=remaining,
                    realised_pnl_native=Decimal("0"),
                    fee_native=Decimal("0"),
                    fx_rate_gbp_per_unit=rate,
                )
            )
        entries: list[PaperJournalEntry] = []
        for trade_id, releases in grouped.items():
            entries.extend(
                self.post_unwind(
                    ValidatedUnwindResult(
                        trade_id=trade_id,
                        close_completed=True,
                        opportunity_id=None,
                        source="paper_demo_reset",
                        source_id=f"demo-reset:{session.session_id}:{trade_id}:{occurred.isoformat()}",
                        reason=reason,
                        releases=releases,
                    ),
                    now=occurred,
                )
            )
        return entries

    def _lock_one(
        self,
        session: PaperTreasurySession,
        request: TreasuryLockRequest,
        *,
        occurred_at: datetime,
        provenance: DataProvenance,
    ) -> PaperJournalEntry:
        with self._ledger.transaction():
            return self._lock_one_in_transaction(
                session, request, occurred_at=occurred_at, provenance=provenance
            )

    def _lock_one_in_transaction(
        self,
        session: PaperTreasurySession,
        request: TreasuryLockRequest,
        *,
        occurred_at: datetime,
        provenance: DataProvenance,
    ) -> PaperJournalEntry:
        if request.amount_native <= 0:
            raise PaperTreasuryError("invalid_lock_amount")
        existing = self._lock_row(request.lock_id)
        if existing is not None:
            if not _lock_facts_match(existing, request):
                raise PaperTreasuryError("conflicting_lock_facts")
            posted = self._ledger.journal.get(request.source, request.lock_id)
            if posted is not None:
                return posted
            return self._repair_lock_journal(
                session, request, existing, occurred_at=occurred_at, provenance=provenance
            )
        pool = self._pool_row(session.session_id, request.venue, request.native_currency)
        available = Decimal(pool["available_cash"])
        locked = Decimal(pool["locked_capital"])
        if request.amount_native > available:
            raise PaperTreasuryError("insufficient_available_cash")
        journal_rate, fx_source = self._authoritative_lock_fx(pool, request)
        amount_gbp = request.amount_native * journal_rate
        capital_source = parse_capital_source(request.capital_source)
        new_available = available - request.amount_native
        new_locked = locked + request.amount_native
        if new_available < 0:
            raise PaperTreasuryError("insufficient_available_cash")
        self._connection.execute(
            """
            UPDATE paper_treasury_pools
            SET available_cash = ?, locked_capital = ?
            WHERE pool_id = ?
            """,
            (str(new_available), str(new_locked), pool["pool_id"]),
        )
        self._connection.execute(
            """
            INSERT INTO paper_treasury_locks (
                lock_id, session_id, pool_id, trade_id, opportunity_id,
                venue, native_currency, locked_native, released_native, status,
                source, capital_source, fill_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', 'open', ?, ?, ?)
            """,
            (
                request.lock_id,
                session.session_id,
                pool["pool_id"],
                request.trade_id,
                request.opportunity_id,
                request.venue.value,
                request.native_currency,
                str(request.amount_native),
                request.source,
                capital_source.value,
                request.fill_id or request.lock_id,
            ),
        )
        return self._post_lock_journal_and_event(
            session,
            request,
            pool_id=pool["pool_id"],
            journal_rate=journal_rate,
            fx_source=fx_source,
            amount_gbp=amount_gbp,
            capital_source=capital_source,
            occurred_at=occurred_at,
            provenance=provenance,
        )

    def _repair_lock_journal(
        self,
        session: PaperTreasurySession,
        request: TreasuryLockRequest,
        existing: Any,
        *,
        occurred_at: datetime,
        provenance: DataProvenance,
    ) -> PaperJournalEntry:
        """Post the missing journal for a committed lock without mutating balances again."""

        pool = self._pool_row(session.session_id, request.venue, request.native_currency)
        journal_rate, fx_source = self._authoritative_lock_fx(pool, request)
        amount_gbp = request.amount_native * journal_rate
        capital_source = parse_capital_source(request.capital_source)
        return self._post_lock_journal_and_event(
            session,
            request,
            pool_id=existing["pool_id"],
            journal_rate=journal_rate,
            fx_source=fx_source,
            amount_gbp=amount_gbp,
            capital_source=capital_source,
            occurred_at=occurred_at,
            provenance=provenance,
        )

    def _post_lock_journal_and_event(
        self,
        session: PaperTreasurySession,
        request: TreasuryLockRequest,
        *,
        pool_id: str,
        journal_rate: Decimal,
        fx_source: str,
        amount_gbp: Decimal,
        capital_source: CapitalSource,
        occurred_at: datetime,
        provenance: DataProvenance,
    ) -> PaperJournalEntry:
        posted, _created = self._ledger.journal.append_idempotent(
            PaperJournalEntry(
                source=request.source,
                source_id=request.lock_id,
                occurred_at=occurred_at,
                description=request.reason,
                opportunity_id=request.opportunity_id or request.lock_id,
                trade_id=request.trade_id,
                provenance=provenance,
                postings=cash_lock_postings(
                    venue=request.venue,
                    currency=request.native_currency,
                    amount_native=request.amount_native,
                    amount_gbp=amount_gbp,
                    fx_rate_gbp_per_unit=journal_rate,
                    opportunity_id=request.opportunity_id or request.lock_id,
                    capital_source=capital_source,
                    position_id=request.lock_id,
                ),
            )
        )
        if not self._event_exists(request.source, request.lock_id):
            self._insert_event(
                PaperTreasuryEvent(
                    event_id=f"lock:{request.lock_id}",
                    session_id=session.session_id,
                    pool_id=pool_id,
                    venue=request.venue,
                    native_currency=request.native_currency,
                    event_type=PaperTreasuryEventType.LOCK,
                    native_amount=request.amount_native,
                    occurred_at=occurred_at,
                    trade_id=request.trade_id,
                    opportunity_id=request.opportunity_id,
                    lock_id=request.lock_id,
                    source=request.source,
                    source_id=request.lock_id,
                    reason=request.reason,
                    fx_rate_gbp_per_unit=journal_rate,
                    fx_source=fx_source,
                    journal_id=posted.journal_id,
                )
            )
        return posted

    def _apply_leg_settlement(
        self,
        trade: PaperTrade,
        *,
        venue: VenueName,
        currency: str,
        fill_id: str | None,
        stake_native: Decimal,
        net_payoff_native: Decimal,
        venue_fee_native: Decimal,
        realised_pnl_native: Decimal,
        fx_rate: Decimal,
        occurred_at: datetime,
        source: str,
        source_id: str,
        journal_id: str,
        reason: str,
    ) -> None:
        session = self._require_session()
        pool = self._pool_row(session.session_id, venue, currency)
        lock = self._lock_for_settlement_leg(
            session.session_id, trade.trade_id, venue, currency, fill_id
        )
        if lock is None or (lock["trade_id"] or None) != trade.trade_id:
            raise PaperTreasuryError("unknown_trade_lock")
        remaining = Decimal(lock["locked_native"]) - Decimal(lock["released_native"])
        if stake_native > remaining:
            raise PaperTreasuryError("release_exceeds_lock")
        locked_release = stake_native
        self._bump_lock_released(lock["lock_id"], locked_release)
        lock_id = lock["lock_id"]
        available_delta = net_payoff_native
        locked = Decimal(pool["locked_capital"])
        available = Decimal(pool["available_cash"])
        if locked_release > locked:
            raise PaperTreasuryError("release_exceeds_lock")
        new_locked = locked - locked_release
        new_available = available + available_delta
        if new_available < 0 or new_locked < 0:
            raise PaperTreasuryError("negative_treasury_balance")
        self._connection.execute(
            """
            UPDATE paper_treasury_pools
            SET available_cash = ?, locked_capital = ?,
                realised_pnl_native = ?, cumulative_fees_native = ?
            WHERE pool_id = ?
            """,
            (
                str(new_available),
                str(new_locked),
                str(Decimal(pool["realised_pnl_native"]) + realised_pnl_native),
                str(Decimal(pool["cumulative_fees_native"]) + venue_fee_native),
                pool["pool_id"],
            ),
        )
        self._insert_event(
            PaperTreasuryEvent(
                event_id=str(uuid4()),
                session_id=session.session_id,
                pool_id=pool["pool_id"],
                venue=venue,
                native_currency=currency.upper(),
                event_type=PaperTreasuryEventType.RELEASE,
                native_amount=locked_release,
                occurred_at=occurred_at,
                trade_id=trade.trade_id,
                opportunity_id=trade.opportunity_id,
                lock_id=lock_id,
                source=source,
                source_id=source_id,
                reason=reason,
                fx_rate_gbp_per_unit=fx_rate,
                fx_source=session.fx_source,
                journal_id=journal_id,
            )
        )
        self._insert_event(
            PaperTreasuryEvent(
                event_id=str(uuid4()),
                session_id=session.session_id,
                pool_id=pool["pool_id"],
                venue=venue,
                native_currency=currency.upper(),
                event_type=PaperTreasuryEventType.REALISED_PNL,
                native_amount=realised_pnl_native,
                occurred_at=occurred_at,
                trade_id=trade.trade_id,
                opportunity_id=trade.opportunity_id,
                lock_id=lock_id,
                source=source,
                source_id=f"{source_id}:pnl",
                reason=reason,
                fx_rate_gbp_per_unit=fx_rate,
                fx_source=session.fx_source,
                journal_id=journal_id,
            )
        )
        if venue_fee_native > 0:
            self._insert_event(
                PaperTreasuryEvent(
                    event_id=str(uuid4()),
                    session_id=session.session_id,
                    pool_id=pool["pool_id"],
                    venue=venue,
                    native_currency=currency.upper(),
                    event_type=PaperTreasuryEventType.FEE,
                    native_amount=venue_fee_native,
                    occurred_at=occurred_at,
                    trade_id=trade.trade_id,
                    opportunity_id=trade.opportunity_id,
                    lock_id=lock_id,
                    source=source,
                    source_id=f"{source_id}:fee",
                    reason=reason,
                    fx_rate_gbp_per_unit=fx_rate,
                    fx_source=session.fx_source,
                    journal_id=journal_id,
                )
            )

    def _close_session(self, session: PaperTreasurySession, *, occurred_at: datetime, reason: str) -> None:
        self._connection.execute(
            """
            UPDATE paper_treasury_sessions
            SET active = 0, closed_at = ?
            WHERE session_id = ?
            """,
            (occurred_at.isoformat(), session.session_id),
        )
        self._append_session_event(
            session_id=session.session_id,
            event_type=PaperTreasuryEventType.SESSION_CLOSE,
            occurred_at=occurred_at,
            reason=reason,
            fx_rate=session.fx_rate_usd_gbp,
            fx_source=session.fx_source,
        )

    def _append_session_event(
        self,
        *,
        session_id: str,
        event_type: PaperTreasuryEventType,
        occurred_at: datetime,
        reason: str,
        fx_rate: Decimal,
        fx_source: str,
    ) -> None:
        self._insert_event(
            PaperTreasuryEvent(
                event_id=str(uuid4()),
                session_id=session_id,
                pool_id=f"{session_id}:session",
                venue=VenueName.MATCHBOOK,
                native_currency="GBP",
                event_type=event_type,
                native_amount=Decimal("0"),
                occurred_at=occurred_at,
                source="paper_treasury_session",
                source_id=f"{session_id}:{event_type.value}:{occurred_at.isoformat()}",
                reason=reason,
                fx_rate_gbp_per_unit=fx_rate,
                fx_source=fx_source,
            )
        )

    def assert_mutation_safe(self) -> None:
        session = self.active_session()
        if session is None:
            return
        self._assert_reset_allowed(session)

    def _assert_reset_allowed(self, session: PaperTreasurySession) -> None:
        lock_rows = list(
            self._connection.execute(
                """
                SELECT lock_id, trade_id FROM paper_treasury_locks
                WHERE session_id = ? AND status = 'open'
                ORDER BY lock_id
                """,
                (session.session_id,),
            )
        )
        if lock_rows:
            raise PaperTreasuryError(
                _mutation_block_message(
                    "active_treasury_locks",
                    lock_ids=[row["lock_id"] for row in lock_rows],
                    trade_ids=[row["trade_id"] for row in lock_rows if row["trade_id"]],
                )
            )
        trades = getattr(self._ledger, "trades", None)
        if trades is not None:
            active = trades.list_active()
            if active:
                raise PaperTreasuryError(
                    _mutation_block_message(
                        "open_paper_positions",
                        trade_ids=[trade.trade_id for trade in active],
                    )
                )

    def _authoritative_lock_fx(self, pool: Any, request: TreasuryLockRequest) -> tuple[Decimal, str]:
        if pool["fx_rate_gbp_per_unit"] is None:
            raise PaperTreasuryError("missing_fx_snapshot")
        pool_rate = Decimal(pool["fx_rate_gbp_per_unit"])
        pool_source = pool["fx_source"]
        if request.native_currency == "GBP":
            if pool_rate != Decimal("1") or request.fx_rate_gbp_per_unit != Decimal("1"):
                raise PaperTreasuryError("fx_rate_mismatch")
            return Decimal("1"), "functional_currency"
        if not pool_source:
            raise PaperTreasuryError("missing_fx_provenance")
        if request.fx_rate_gbp_per_unit != pool_rate:
            raise PaperTreasuryError("fx_rate_mismatch")
        return pool_rate, pool_source

    def lock_fx_rate(self, venue: VenueName, currency: str) -> Decimal:
        session = self._require_session()
        pool = self._pool_row(session.session_id, venue, currency)
        if pool["fx_rate_gbp_per_unit"] is None:
            raise PaperTreasuryError("missing_fx_snapshot")
        return Decimal(pool["fx_rate_gbp_per_unit"])

    def _require_session(self) -> PaperTreasurySession:
        session = self.active_session()
        if session is None:
            raise PaperTreasuryError("stale_unknown_pool")
        return session

    def _pool_row(self, session_id: str, venue: VenueName, currency: str) -> Any:
        row = self._connection.execute(
            """
            SELECT * FROM paper_treasury_pools
            WHERE session_id = ? AND venue = ? AND native_currency = ?
            """,
            (session_id, venue.value, currency.upper()),
        ).fetchone()
        if row is None:
            raise PaperTreasuryError("stale_unknown_pool")
        return row

    def _load_pools(self, session_id: str) -> list[PaperTreasuryPoolState]:
        return [_pool_from_row(row) for row in self._load_pool_rows(session_id)]

    def _load_pool_rows(self, session_id: str) -> list[Any]:
        return list(
            self._connection.execute(
                "SELECT * FROM paper_treasury_pools WHERE session_id = ? ORDER BY venue, native_currency",
                (session_id,),
            )
        )

    def _lock_row(self, lock_id: str) -> Any:
        return self._connection.execute(
            "SELECT * FROM paper_treasury_locks WHERE lock_id = ?",
            (lock_id,),
        ).fetchone()

    def _lock_for_settlement_leg(
        self,
        session_id: str,
        trade_id: str,
        venue: VenueName,
        currency: str,
        fill_id: str | None,
    ) -> Any:
        if not fill_id:
            raise PaperTreasuryError("missing_lock_identity")
        rows = list(
            self._connection.execute(
                """
                SELECT * FROM paper_treasury_locks
                WHERE session_id = ?
                  AND trade_id = ?
                  AND venue = ?
                  AND native_currency = ?
                  AND status = 'open'
                  AND (lock_id = ? OR fill_id = ?)
                ORDER BY rowid
                """,
                (
                    session_id,
                    trade_id,
                    venue.value,
                    currency.upper(),
                    fill_id,
                    fill_id,
                ),
            )
        )
        if not rows:
            raise PaperTreasuryError("unknown_trade_lock")
        if len(rows) > 1:
            raise PaperTreasuryError("ambiguous_lock_identity")
        return rows[0]

    def _bump_lock_released(self, lock_id: str, amount: Decimal) -> None:
        row = self._lock_row(lock_id)
        if row is None:
            return
        released = Decimal(row["released_native"]) + amount
        locked = Decimal(row["locked_native"])
        status = "released" if released >= locked else "open"
        self._connection.execute(
            """
            UPDATE paper_treasury_locks
            SET released_native = ?, status = ?
            WHERE lock_id = ?
            """,
            (str(released), status, lock_id),
        )

    def _release_locked(self, pool: Any, *, amount: Decimal, into_available: Decimal) -> None:
        locked = Decimal(pool["locked_capital"])
        available = Decimal(pool["available_cash"])
        if amount > locked:
            # Sequential native-lock releases can exceed stored pool locked
            # by Decimal representation dust (sum(locks) == pool, but
            # pool - a - b < c). Clamp that dust so every lock can still
            # be released exactly once.
            dust = amount - locked
            if dust <= Decimal("1e-18"):
                into_available -= dust
                amount = locked
            else:
                raise PaperTreasuryError("release_exceeds_lock")
        new_locked = locked - amount
        new_available = available + into_available
        if new_available < 0 or new_locked < 0:
            raise PaperTreasuryError("negative_treasury_balance")
        self._connection.execute(
            """
            UPDATE paper_treasury_pools
            SET available_cash = ?, locked_capital = ?
            WHERE pool_id = ?
            """,
            (str(new_available), str(new_locked), pool["pool_id"]),
        )

    def _add_realised(self, pool: Any, pnl: Decimal, fee: Decimal) -> None:
        self._connection.execute(
            """
            UPDATE paper_treasury_pools
            SET realised_pnl_native = ?, cumulative_fees_native = ?
            WHERE pool_id = ?
            """,
            (
                str(Decimal(pool["realised_pnl_native"]) + pnl),
                str(Decimal(pool["cumulative_fees_native"]) + fee),
                pool["pool_id"],
            ),
        )

    def _event_exists(self, source: str, source_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM paper_treasury_events WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        return row is not None

    def _insert_event(self, event: PaperTreasuryEvent) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO paper_treasury_events (
                    event_id, session_id, pool_id, venue, native_currency, event_type,
                    native_amount, occurred_at, trade_id, opportunity_id, lock_id,
                    source, source_id, reason, fx_rate_gbp_per_unit, fx_source, journal_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.session_id,
                    event.pool_id,
                    event.venue.value,
                    event.native_currency,
                    event.event_type.value,
                    str(event.native_amount),
                    event.occurred_at.isoformat(),
                    event.trade_id,
                    event.opportunity_id,
                    event.lock_id,
                    event.source,
                    event.source_id,
                    event.reason,
                    str(event.fx_rate_gbp_per_unit) if event.fx_rate_gbp_per_unit is not None else None,
                    event.fx_source,
                    event.journal_id,
                ),
            )
        except Exception as exc:
            raise PaperTreasuryError(f"duplicate_treasury_event:{event.source}:{event.source_id}") from exc
        emitter = getattr(self._ledger, "accounting_emitter", None)
        if emitter is not None:
            journal = None
            if event.journal_id:
                for entry in self._ledger.journal.list_entries():
                    if entry.journal_id == event.journal_id:
                        journal = entry
                        break
            if journal is None:
                journal = self._ledger.journal.get(event.source, event.source_id)
            emitter.emit_treasury(event, journal=journal)


def _pool_id(session_id: str, venue: VenueName, currency: str) -> str:
    return f"{session_id}:{venue.value}/{currency.upper()}"


def _session_from_row(row: Any) -> PaperTreasurySession:
    return PaperTreasurySession(
        session_id=row["session_id"],
        opened_at=datetime.fromisoformat(row["opened_at"]),
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        active=bool(row["active"]),
        provenance=row["provenance"],
        reason=row["reason"],
        seed_gbp=Decimal(row["seed_gbp"]),
        fx_rate_usd_gbp=Decimal(row["fx_rate_usd_gbp"]),
        fx_source=row["fx_source"],
        fx_as_of=datetime.fromisoformat(row["fx_as_of"]),
        include_kalshi=bool(row["include_kalshi"]),
    )


def _pool_from_row(row: Any) -> PaperTreasuryPoolState:
    currency = row["native_currency"]
    available = Decimal(row["available_cash"])
    locked = Decimal(row["locked_capital"])
    rate_raw = _row_value(row, "fx_rate_gbp_per_unit", "")
    if rate_raw:
        rate = Decimal(rate_raw)
    elif currency.upper() == "GBP":
        rate = Decimal("1")
    else:
        rate = None
    native_total = available + locked
    gbp = native_total * rate if rate is not None else None
    if currency == "GBP":
        status = "identity"
    elif rate is not None:
        status = "fx_converted"
    else:
        status = "fx_unavailable"
    fx_as_of_raw = _row_value(row, "fx_as_of", "")
    return PaperTreasuryPoolState(
        pool_id=row["pool_id"],
        session_id=row["session_id"],
        venue=VenueName(row["venue"]),
        native_currency=currency,
        identity=f"{row['venue']}/{currency}",
        seed_native=Decimal(row["seed_native"]),
        available_cash=available,
        locked_capital=locked,
        realised_pnl_native=Decimal(row["realised_pnl_native"]),
        cumulative_fees_native=Decimal(row["cumulative_fees_native"]),
        gbp_carrying_value=gbp,
        gbp_carrying_status=status,
        fx_rate_gbp_per_unit=rate,
        fx_source=_row_value(row, "fx_source", "") or None,
        fx_as_of=datetime.fromisoformat(fx_as_of_raw) if fx_as_of_raw else None,
    )


def _event_from_row(row: Any) -> PaperTreasuryEvent:
    return PaperTreasuryEvent(
        event_id=row["event_id"],
        session_id=row["session_id"],
        pool_id=row["pool_id"],
        venue=VenueName(row["venue"]),
        native_currency=row["native_currency"],
        event_type=row["event_type"],
        native_amount=Decimal(row["native_amount"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        trade_id=row["trade_id"],
        opportunity_id=row["opportunity_id"],
        lock_id=row["lock_id"],
        source=row["source"],
        source_id=row["source_id"],
        reason=row["reason"],
        fx_rate_gbp_per_unit=Decimal(row["fx_rate_gbp_per_unit"]) if row["fx_rate_gbp_per_unit"] else None,
        fx_source=row["fx_source"],
        journal_id=row["journal_id"],
    )


def _row_value(row: Any, key: str, default: str) -> str:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else str(value)


def _lock_facts_match(existing: Any, request: TreasuryLockRequest) -> bool:
    return (
        VenueName(existing["venue"]) is request.venue
        and existing["native_currency"].upper() == request.native_currency
        and Decimal(existing["locked_native"]) == request.amount_native
        and (existing["trade_id"] or None) == request.trade_id
        and (existing["opportunity_id"] or None) == request.opportunity_id
        and _row_value(existing, "source", "paper_fill_simulator") == request.source
        and _row_value(existing, "capital_source", "AUTO_POOL")
        == parse_capital_source(request.capital_source).value
        and _row_value(existing, "fill_id", existing["lock_id"])
        == (request.fill_id or request.lock_id)
    )


def _mutation_block_message(
    code: str,
    *,
    lock_ids: list[str] | None = None,
    trade_ids: list[str] | None = None,
) -> str:
    """Keep the fail-closed code as the prefix so existing matchers still pass."""

    parts = [code]
    unique_locks = list(dict.fromkeys(lock_ids or []))
    unique_trades = list(dict.fromkeys(trade_ids or []))
    if unique_locks:
        parts.append("lock_id=" + ",".join(unique_locks))
    if unique_trades:
        parts.append("trade_id=" + ",".join(unique_trades))
    return " ".join(parts)
