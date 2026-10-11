"""Diagnostic STREAM comparison. Reuses observation builders and paper scan_pair.

Price-2 / paper opening is a separate handoff after coalesced Matchbook quotes.
Shadow is never execution authority: it must reuse the same fee, FX, catalogue
and operator-threshold inputs as HOT/BACKGROUND, then hand a qualifying
decision to the existing Price-2 capture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sports_hedge.application.executable_liquidity import (
    decision_is_solver_arbitrage,
    decision_net_edge,
)
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import (
    _canonical_matchbook_market,
    _canonical_polymarket_market,
)
from sports_hedge.application.quote_freshness import matchbook_market_quote_age
from sports_hedge.application.stream.coalescer import MatchbookQuote
from sports_hedge.application.stream.order_book import StreamOrderBooks
from sports_hedge.application.stream.pin import StreamFixturePin
from sports_hedge.application.stream.protocol import FUTURE_TIMESTAMP_SLACK_MS, STALE_AFTER_SECONDS
from sports_hedge.arbitrage.min_net_threshold import catalogue_market_scope
from sports_hedge.fx.scanner_context import bind_lane_fx
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.config import Settings
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.persistence.operator_scanner_settings import (
    effective_operator_scanner_settings,
)

_FX_BIND_TTL = timedelta(seconds=30)
_DATA_INTEGRITY_PREFIXES = (
    "incomplete_stream_or_matchbook_pair",
    "missing_token_timestamp",
    "future_token_timestamp",
    "canonical_identity_unavailable",
    "stream_constituent_stale",
    "shadow_compare_failed",
    "naive_quote_timestamp",
    "invalid_quote_timestamp",
    "unknown_quote_age",
    "stale_quote",
    "stale_quote_age",
    "stale_pre_event_quote",
    "future_fee_snapshot",
    "future_fx_snapshot",
    "invalid_fee_snapshot_time",
    "invalid_fx_snapshot_time",
)
@dataclass(frozen=True)
class StreamCandidate:
    catalogue_row_id: str
    register_canonical_key: str
    trustworthy: bool
    net_edge: str | None
    rejection_reasons: tuple[str, ...]
    stream_quote_at: str | None
    matchbook_quote_at: str | None
    pair_age_ms: int | None
    skew_ms: int | None
    data_class: str = "stream_shadow_candidate"
    data_valid: bool = False
    economics_state: str = "blocked"
    top_reason: str | None = None
    current: bool = True


class StreamShadowComparer:
    def __init__(
        self,
        paper_scan: PaperScanService | None = None,
        *,
        catalogue_store: Any = None,
        fx_snapshots: list[Any] | None = None,
        fx_unavailable_reason: str | None = None,
        use_process_authorities: bool = False,
    ) -> None:
        self.matchbook_builder = MatchbookObservationBuilder()
        self.polymarket_builder = PolymarketObservationBuilder()
        self._paper_scan = paper_scan
        self.catalogue_store = catalogue_store
        self._use_process_authorities = use_process_authorities
        self._explicit_fx = list(fx_snapshots) if fx_snapshots else None
        self._explicit_fx_reason = fx_unavailable_reason
        self._fx_snapshots: list[Any] = list(fx_snapshots or [])
        self._fx_reason: str | None = fx_unavailable_reason
        self._fx_bound_at: datetime | None = None
        self.candidates: list[StreamCandidate] = []
        self.decisions: dict[str, Any] = {}

    @classmethod
    def shared(cls, **kwargs: Any) -> StreamShadowComparer:
        kwargs.setdefault("use_process_authorities", True)
        return cls(**kwargs)

    @property
    def paper_scan(self) -> PaperScanService:
        if self._paper_scan is None:
            self._paper_scan = (
                _shared_paper_scan() if self._use_process_authorities else _isolated_paper_scan()
            )
        return self._paper_scan

    @paper_scan.setter
    def paper_scan(self, value: PaperScanService) -> None:
        self._paper_scan = value

    def compare(
        self,
        pin: StreamFixturePin,
        books: StreamOrderBooks,
        quotes: dict[str, MatchbookQuote],
        *,
        now: datetime | None = None,
    ) -> list[StreamCandidate]:
        evaluated = now or datetime.now(UTC)
        self._refresh_fx(evaluated)
        results: list[StreamCandidate] = []
        decisions: dict[str, Any] = {}
        for market in pin.markets:
            if market.unavailable_reason:
                continue
            identity = market.identity
            quote = quotes.get(f"{identity.matchbook_event_id}:{identity.matchbook_market_id}")
            token_payloads = {
                token: books.books[token].snapshot_payload()
                for token in market.token_ids
                if token in books.books and books.books[token].healthy
            }
            stream_ts, stream_reason = _oldest_required_stream_ts(
                books, market.token_ids, now=evaluated
            )
            if quote is None or stream_ts is None or len(token_payloads) != len(market.token_ids):
                results.append(
                    _integrity_candidate(
                        identity,
                        stream_reason or "incomplete_stream_or_matchbook_pair",
                        stream_ts,
                        None if quote is None else quote.retrieved_at,
                    )
                )
                continue
            mb_market = _canonical_matchbook_market(identity)
            pm_market = _canonical_polymarket_market(identity)
            if mb_market is None or pm_market is None:
                results.append(
                    _integrity_candidate(
                        identity,
                        "canonical_identity_unavailable",
                        stream_ts,
                        quote.retrieved_at,
                    )
                )
                continue
            mb_age = matchbook_market_quote_age(
                quote.payload, retrieved_at=quote.retrieved_at, evaluated_at=evaluated
            )
            pm_age_ms = max(0, int((evaluated - stream_ts).total_seconds() * 1000))
            if pm_age_ms > int(STALE_AFTER_SECONDS * 1000):
                results.append(
                    _integrity_candidate(
                        identity,
                        "stream_constituent_stale",
                        stream_ts,
                        quote.retrieved_at,
                        pair_age_ms=pm_age_ms,
                        skew_ms=abs(int((quote.retrieved_at - stream_ts).total_seconds() * 1000)),
                    )
                )
                continue
            skew_ms = abs(int((quote.retrieved_at - stream_ts).total_seconds() * 1000))
            pair_age_ms = max(pm_age_ms, int(mb_age.quote_age_ms or 0))
            try:
                matchbook_obs = self.matchbook_builder.build_from_canonical(
                    mb_market,
                    quote.payload,
                    observed_at=evaluated,
                    quote_age_ms=mb_age.quote_age_ms,
                    quote_age_basis=mb_age.basis or "retrieval",
                    quote_age_reason=mb_age.reason,
                )
                polymarket_obs = self.polymarket_builder.build(
                    {
                        "id": identity.polymarket_event_id,
                        "title": f"{identity.home_canonical} vs {identity.away_canonical}",
                    },
                    {
                        "id": identity.polymarket_market_id,
                        "conditionId": identity.polymarket_condition_id,
                        "clobTokenIds": list(market.token_ids),
                    },
                    token_payloads,
                    canonical=pm_market,
                    observed_at=evaluated,
                    quote_age_ms=pm_age_ms,
                    quote_age_basis="stream_book",
                    quote_age_reason=None,
                    fee_snapshot=self._polymarket_fee_snapshot_payload(identity),
                )
                polymarket_obs.metadata["stream_data_class"] = "stream_shadow_not_price2"
                decision = self.paper_scan.scan_pair(
                    matchbook_obs,
                    polymarket_obs,
                    **self._scan_kwargs(identity),
                )
            except Exception as exc:  # noqa: BLE001 — STREAM compare must not kill the observer
                results.append(
                    _integrity_candidate(
                        identity,
                        f"shadow_compare_failed:{type(exc).__name__}",
                        stream_ts,
                        quote.retrieved_at,
                        pair_age_ms=pair_age_ms,
                        skew_ms=skew_ms,
                    )
                )
                continue
            results.append(
                _candidate_from_decision(identity, decision, stream_ts, quote, pair_age_ms, skew_ms)
            )
            decisions[identity.catalogue_row_id] = decision
        self.candidates = results
        self.decisions = decisions
        return results

    def _scan_kwargs(self, identity: Any) -> dict[str, Any]:
        """Match HOT `_scan_kwargs`. Leave capital to `policy_from_settings`.

        Do not pass `capital_limit_gbp` or invented Max Event / Opportunity /
        One-Time values. PaperScanService already sizes from persisted operator
        settings.
        """
        kwargs: dict[str, Any] = {
            "fixture_canonical_event_id": identity.canonical_event_id,
        }
        if self._fx_snapshots:
            kwargs["fx_snapshots"] = list(self._fx_snapshots)
        elif self._fx_reason:
            kwargs["fx_unavailable_reason"] = self._fx_reason
        settings = getattr(self.paper_scan, "settings", None)
        if settings is not None:
            kwargs["market_scope"] = catalogue_market_scope(identity)
            kwargs["assumed_latency_ms"] = int(settings.simulated_latency_ms)
            if self._use_process_authorities:
                operator = effective_operator_scanner_settings(settings)
                kwargs["minimum_net_edge"] = operator.min_net_edge
                kwargs["outright_min_net_edge"] = operator.outright_min_net_edge
                kwargs["maximum_execution_risk"] = operator.max_execution_risk
            else:
                kwargs["minimum_net_edge"] = settings.min_net_edge
                kwargs["outright_min_net_edge"] = settings.outright_min_net_edge
                kwargs["maximum_execution_risk"] = int(settings.max_execution_risk)
        return kwargs

    def _refresh_fx(self, as_of: datetime) -> None:
        if self._explicit_fx:
            self._fx_snapshots = list(self._explicit_fx)
            self._fx_reason = None
            self._fx_bound_at = as_of
            return
        if self._explicit_fx_reason:
            self._fx_snapshots = []
            self._fx_reason = self._explicit_fx_reason
            self._fx_bound_at = as_of
            return
        if self._fx_bound_at is not None and as_of - self._fx_bound_at < _FX_BIND_TTL:
            return
        if not self._use_process_authorities:
            self._fx_snapshots = []
            self._fx_reason = None
            self._fx_bound_at = as_of
            return
        engine = _existing_price_engine()
        if engine is not None and getattr(engine, "_scanner_fx_bound", False):
            if engine.fx_snapshots:
                self._fx_snapshots = list(engine.fx_snapshots)
                self._fx_reason = None
                self._fx_bound_at = as_of
                return
            if getattr(engine, "_scanner_fx_reason", None):
                self._fx_snapshots = []
                self._fx_reason = engine._scanner_fx_reason
                self._fx_bound_at = as_of
                return
        snapshots, reason = bind_lane_fx(
            explicit=None,
            fx_service=getattr(self.paper_scan, "fx_service", None),
            as_of=as_of,
        )
        self._fx_snapshots = list(snapshots or [])
        self._fx_reason = reason
        self._fx_bound_at = as_of

    def _polymarket_fee_snapshot_payload(self, identity: Any) -> dict[str, Any] | None:
        store = self.catalogue_store
        if store is None and self._use_process_authorities:
            engine = _existing_price_engine()
            store = None if engine is None else getattr(engine, "catalogue_store", None)
            if store is None:
                store = _process_catalogue_store()
            self.catalogue_store = store
        snapshot_id = getattr(identity, "polymarket_fee_snapshot_id", None)
        if store is None or not snapshot_id:
            return None
        snapshot = store.get_polymarket_fee_snapshot(snapshot_id)
        if snapshot is None:
            return None
        return snapshot.observation_metadata()


def _candidate_from_decision(
    identity: Any,
    decision: PaperScanDecision,
    stream_ts: datetime,
    quote: MatchbookQuote,
    pair_age_ms: int,
    skew_ms: int,
) -> StreamCandidate:
    reasons = tuple(
        str(item)
        for item in (decision.rejection_reasons or [])
        if str(item) not in {"stream_shadow_not_price2", "stream_shadow_not_executable"}
    )
    edge = decision_net_edge(decision)
    data_valid = not any(_is_data_integrity_reason(reason) for reason in reasons)
    economics_state = _economics_state(decision, reasons, data_valid)
    trustworthy = economics_state == "qualified"
    top_reason = reasons[0] if reasons else None
    if economics_state == "no_arb" and top_reason is None:
        top_reason = "valid_no_arb"
    return StreamCandidate(
        catalogue_row_id=identity.catalogue_row_id,
        register_canonical_key=identity.register_canonical_key,
        trustworthy=trustworthy,
        net_edge=None if edge is None else format(Decimal(str(edge)), "f"),
        rejection_reasons=reasons,
        stream_quote_at=_iso(stream_ts),
        matchbook_quote_at=_iso(quote.retrieved_at),
        pair_age_ms=pair_age_ms,
        skew_ms=skew_ms,
        data_valid=data_valid,
        economics_state=economics_state,
        top_reason=top_reason,
    )


def _integrity_candidate(
    identity: Any,
    reason: str,
    stream_ts: datetime | None,
    matchbook_ts: datetime | None,
    *,
    pair_age_ms: int | None = None,
    skew_ms: int | None = None,
) -> StreamCandidate:
    return StreamCandidate(
        catalogue_row_id=identity.catalogue_row_id,
        register_canonical_key=identity.register_canonical_key,
        trustworthy=False,
        net_edge=None,
        rejection_reasons=(reason,),
        stream_quote_at=_iso(stream_ts),
        matchbook_quote_at=_iso(matchbook_ts),
        pair_age_ms=pair_age_ms,
        skew_ms=skew_ms,
        data_valid=False,
        economics_state="blocked",
        top_reason=reason,
    )


def _economics_state(
    decision: PaperScanDecision,
    reasons: tuple[str, ...],
    data_valid: bool,
) -> str:
    if not data_valid:
        return "blocked"
    if any(_is_missing_cost_or_fx(reason) for reason in reasons):
        return "blocked"
    edge = decision_net_edge(decision)
    if decision_is_solver_arbitrage(decision) and edge is not None:
        minimum = decision.minimum_net_edge
        if minimum is None or edge >= minimum:
            blocking = [
                reason
                for reason in reasons
                if not reason.startswith("net_edge_below_threshold")
                and reason not in {"no_arbitrage", "no_executable_arb", "valid_no_arb"}
            ]
            if decision.eligible_for_paper_simulation or not blocking:
                return "qualified"
    return "no_arb"


def _is_missing_cost_or_fx(reason: str) -> bool:
    return reason.startswith(
        ("unknown_required_venue_cost", "missing_venue_cost", "unknown_costs", "missing_fx_rate")
    )


def _is_data_integrity_reason(reason: str) -> bool:
    return any(reason == prefix or reason.startswith(f"{prefix}:") for prefix in _DATA_INTEGRITY_PREFIXES)


def mark_candidates_current(candidates: list[StreamCandidate], *, current: bool) -> list[StreamCandidate]:
    if current:
        return candidates
    return [replace(item, current=False) for item in candidates]


def _shared_paper_scan() -> PaperScanService:
    engine = _existing_price_engine()
    bound = None if engine is None else getattr(engine, "paper_scan", None)
    if bound is not None:
        return bound
    try:
        from sports_hedge.api.paper import scheduled_paper_scan_service

        return scheduled_paper_scan_service()
    except Exception:  # noqa: BLE001 — tests and partial boots still need a scanner
        return _isolated_paper_scan()


def _existing_price_engine() -> Any | None:
    try:
        from sports_hedge.application.live_refresh import get_live_refresh_coordinator

        return getattr(get_live_refresh_coordinator(), "_price_engine", None)
    except Exception:  # noqa: BLE001 — STREAM must not fail status if HOT is unbound
        return None


def _process_catalogue_store() -> Any | None:
    try:
        from sports_hedge.persistence.approved_market_catalogue import (
            get_approved_market_catalogue_store,
        )

        return get_approved_market_catalogue_store()
    except Exception:  # noqa: BLE001 — missing catalogue is fail-closed fee evidence
        return None


def _isolated_paper_scan() -> PaperScanService:
    return PaperScanService(
        MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
        settings=Settings(),
    )


def _oldest_required_stream_ts(
    books: StreamOrderBooks,
    token_ids: tuple[str, ...],
    *,
    now: datetime,
) -> tuple[datetime | None, str | None]:
    stamps: list[int] = []
    now_ms = int(now.timestamp() * 1000)
    for token in token_ids:
        book = books.books.get(token)
        if book is None or not book.healthy:
            return None, "incomplete_stream_or_matchbook_pair"
        ts = book.last_applied_ts_ms
        if ts is None:
            return None, "missing_token_timestamp"
        if ts > now_ms + FUTURE_TIMESTAMP_SLACK_MS:
            return None, "future_token_timestamp"
        stamps.append(ts)
    if not stamps:
        return None, "incomplete_stream_or_matchbook_pair"
    oldest = min(stamps)
    return datetime.fromtimestamp(oldest / 1000, tz=UTC), None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()
