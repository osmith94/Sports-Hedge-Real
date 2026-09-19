"""Issue #350 Phase 6 validation tooling.

Synthetic old-vs-new identity parity, empty-catalogue bootstrap helpers, and a
read-only PAPER soak observer/report. This module is **not** a second matcher,
not a durable price queue, and not scheduler authority. Equivalence still comes
only from the Approved Match Register.

CI uses fixture/demo snapshots only. Owner-live PAPER soak evidence is a
separate gate after architect acceptance and merge.

PAPER / read-only. No venue writes.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    family_period_line_from_key,
    required_outcomes_for_key,
)
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.catalogue_maintenance import (
    kalshi_constituent_tickers,
    ordered_native_ids,
    pair_identity_from_markets,
)
from sports_hedge.application.price_engine import (
    SCAN_BUDGET_EXHAUSTED_REASON,
    CataloguePriceEngine,
    PriceEngineItemStatus,
    PriceEngineRuntimeItem,
)
from sports_hedge.application.scanner_observability import PriceEnginePublicStatus
from sports_hedge.application.serving_build import ServingBuildInfo, get_serving_build_info
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.approved_register import (
    REGISTER_ADMITTED_REASON,
    registered_canonical_key,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    VenueNormalizationError,
)
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

PHASE6_ISSUE = 350
DATA_CLASS_FIXTURE_DEMO = "fixture_demo"
DATA_CLASS_OWNER_LIVE_OBSERVATION = "owner_live_observation"
SOAK_HTTP_GET_ONLY = ("GET",)
FORBIDDEN_SOAK_HTTP = ("POST", "PUT", "PATCH", "DELETE")
DEFAULT_SOAK_DURATION_SECONDS = 720
DEFAULT_SOAK_INTERVAL_SECONDS = 15
CONFIDENCE_REVIEW_TOKENS = (
    "min_mapping_confidence",
    "mapping_confidence",
    "review_required",
    "learned_rule",
    "mapping_review",
)
TRUTHFUL_UNEVALUATED_REASONS = frozenset(
    {
        PriceEngineItemStatus.RETRY_WAIT.value,
        PriceEngineItemStatus.DEFERRED.value,
        PriceEngineItemStatus.NOT_STARTED.value,
        PriceEngineItemStatus.REVALIDATION_NEEDED.value,
        PriceEngineItemStatus.IN_FLIGHT.value,
        PriceEngineItemStatus.FAILED.value,
        PriceEngineItemStatus.DUE.value,
        "provider_capacity_saturated",
    }
)


class RegisterNativeIdentity(BaseModel):
    """Exact register key plus native Matchbook/Kalshi IDs for one family."""

    model_config = ConfigDict(extra="forbid")

    register_canonical_key: str
    matchbook_event_id: str
    matchbook_market_id: str
    matchbook_runner_ids: list[OutcomeNativeId] = Field(default_factory=list)
    kalshi_event_ticker: str
    kalshi_market_tickers: list[str] = Field(default_factory=list)
    kalshi_outcome_ids: list[OutcomeNativeId] = Field(default_factory=list)
    required_outcomes: list[str] = Field(default_factory=list)
    family: str | None = None
    period: str | None = None
    line: str | None = None

    def identity_tuple(self) -> tuple[Any, ...]:
        return (
            self.register_canonical_key,
            self.matchbook_event_id,
            self.matchbook_market_id,
            tuple((item.outcome, item.native_id) for item in self.matchbook_runner_ids),
            self.kalshi_event_ticker,
            tuple(self.kalshi_market_tickers),
            tuple((item.outcome, item.native_id) for item in self.kalshi_outcome_ids),
            tuple(self.required_outcomes),
            self.line,
            self.period,
        )


class ParityMismatch(BaseModel):
    register_canonical_key: str
    field: str
    legacy: Any = None
    catalogue: Any = None


class ParityReport(BaseModel):
    """Identity/equivalence parity. Not orchestration-timing parity."""

    matches: bool
    data_kind: str = DATA_CLASS_FIXTURE_DEMO
    paper_only: bool = True
    dual_matcher: bool = False
    legacy_keys: list[str] = Field(default_factory=list)
    catalogue_keys: list[str] = Field(default_factory=list)
    mismatches: list[ParityMismatch] = Field(default_factory=list)
    missing_in_catalogue: list[str] = Field(default_factory=list)
    extra_in_catalogue: list[str] = Field(default_factory=list)
    forbidden_admissions: list[str] = Field(default_factory=list)


class VenueWriteEvidence(BaseModel):
    paper_mode: bool = True
    execution_enabled: bool = False
    forbidden_methods_present: list[str] = Field(default_factory=list)
    soak_http_methods: list[str] = Field(default_factory=list)
    venue_write_invoked: bool = False
    place_cancel_sign_reachable: bool = False


class SoakRowState(BaseModel):
    catalogue_row_id: str
    register_canonical_key: str
    canonical_event_id: str
    row_state: str
    priority: str | None = None
    status: str
    reason: str
    evaluated_at_least_once: bool = False
    last_priced_at: datetime | None = None
    last_error_stage: str | None = None
    last_error_detail: str | None = None
    revalidation_reason: str | None = None


class SoakEndpointSample(BaseModel):
    path: str
    status_code: int | None = None
    available: bool = False
    elapsed_ms: float | None = None
    error: str | None = None


class SoakCaptureSummary(BaseModel):
    paper_autofill_enabled: bool = False
    open_trades: int = 0
    paper_fill_rejected: int = 0
    paper_fill_attempted: int = 0
    duplicate_open_ids: list[str] = Field(default_factory=list)
    eligible_without_open_or_reject: int = 0
    persist_failures: int = 0


class SoakHardFail(BaseModel):
    code: str
    detail: str


class ScannerValidationSnapshot(BaseModel):
    """One observer read of catalogue + process-memory price-engine truth."""

    observed_at: datetime
    data_kind: str = DATA_CLASS_FIXTURE_DEMO
    paper_only: bool = True
    observer_only: bool = True
    triggered_discovery: bool = False
    triggered_pricing: bool = False
    mode: str = "paper"
    execution_enabled: bool = False
    paper_autofill_enabled: bool = False
    build: dict[str, Any] = Field(default_factory=dict)
    active_catalogue_row_count: int = 0
    rows: list[SoakRowState] = Field(default_factory=list)
    price_engine: PriceEnginePublicStatus = Field(default_factory=PriceEnginePublicStatus)
    durable_queue: bool = False
    hot_in_progress: bool = False
    background_in_progress: bool = False
    universe_in_progress: bool = False
    promoted_hot_count: int = 0
    hot_promotions: int = 0
    revalidation_requests: list[dict[str, str]] = Field(default_factory=list)
    capture: SoakCaptureSummary = Field(default_factory=SoakCaptureSummary)
    venue_write: VenueWriteEvidence = Field(default_factory=VenueWriteEvidence)
    scan_budget_exhausted_price_engine: int = 0
    endpoint_samples: list[SoakEndpointSample] = Field(default_factory=list)


class SoakReport(BaseModel):
    """Bounded observer report. Never scheduler authority."""

    schema_version: int = 1
    issue: int = PHASE6_ISSUE
    data_kind: str = DATA_CLASS_FIXTURE_DEMO
    paper_only: bool = True
    execution_enabled: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float = 0
    sample_count: int = 0
    observed_build_sha: str | None = None
    observed_build_branch: str | None = None
    active_catalogue_row_count: int = 0
    rows_evaluated_at_least_once: int = 0
    coverage_ratio: float | None = None
    unevaluated_rows: list[SoakRowState] = Field(default_factory=list)
    latest_rows: list[SoakRowState] = Field(default_factory=list)
    hot_evaluated: int = 0
    hot_retry: int = 0
    hot_deferred: int = 0
    hot_not_started: int = 0
    background_evaluated: int = 0
    background_retry: int = 0
    background_deferred: int = 0
    background_not_started: int = 0
    revalidation_needed: int = 0
    revalidation_reasons: list[str] = Field(default_factory=list)
    provider_health: dict[str, Any] = Field(default_factory=dict)
    scan_budget_exhausted_price_engine: int = 0
    hot_universe_overlap_samples: int = 0
    background_hot_promotions: int = 0
    capture: SoakCaptureSummary = Field(default_factory=SoakCaptureSummary)
    endpoint_samples: list[SoakEndpointSample] = Field(default_factory=list)
    venue_write: VenueWriteEvidence = Field(default_factory=VenueWriteEvidence)
    durable_queue: bool = False
    hard_fails: list[SoakHardFail] = Field(default_factory=list)
    accepted: bool | None = None
    notes: list[str] = Field(default_factory=list)


def venue_write_boundary_evidence(
    *,
    settings: Settings | None = None,
    soak_http_methods: list[str] | None = None,
) -> VenueWriteEvidence:
    resolved = settings or get_settings()
    present: list[str] = []
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            if hasattr(client, method):
                present.append(f"{client.__name__}.{method}")
    methods = list(soak_http_methods or [])
    write_invoked = any(item.upper() in FORBIDDEN_SOAK_HTTP for item in methods)
    return VenueWriteEvidence(
        paper_mode=resolved.sports_hedge_mode == "paper",
        execution_enabled=bool(resolved.sports_hedge_execution_enabled),
        forbidden_methods_present=present,
        soak_http_methods=methods,
        venue_write_invoked=write_invoked,
        place_cancel_sign_reachable=bool(present),
    )


def identity_from_canonical_pair(
    matchbook: CanonicalMarket,
    kalshi: CanonicalMarket,
    *,
    register_canonical_key: str,
) -> RegisterNativeIdentity:
    family, period, line = family_period_line_from_key(register_canonical_key)
    return RegisterNativeIdentity(
        register_canonical_key=register_canonical_key,
        matchbook_event_id=str(matchbook.event.source_event_id),
        matchbook_market_id=str(matchbook.source_market_id),
        matchbook_runner_ids=ordered_native_ids(matchbook, register_canonical_key),
        kalshi_event_ticker=str(kalshi.event.source_event_id),
        kalshi_market_tickers=kalshi_constituent_tickers(kalshi),
        kalshi_outcome_ids=ordered_native_ids(kalshi, register_canonical_key),
        required_outcomes=required_outcomes_for_key(register_canonical_key),
        family=family,
        period=period,
        line=line,
    )


def identity_from_catalogue_row(row: ApprovedMarketCatalogueRow) -> RegisterNativeIdentity:
    return RegisterNativeIdentity(
        register_canonical_key=row.register_canonical_key,
        matchbook_event_id=str(row.matchbook_event_id or ""),
        matchbook_market_id=str(row.matchbook_market_id or ""),
        matchbook_runner_ids=list(row.matchbook_runner_ids),
        kalshi_event_ticker=str(row.kalshi_event_ticker or ""),
        kalshi_market_tickers=list(row.kalshi_market_tickers),
        kalshi_outcome_ids=list(row.kalshi_outcome_ids),
        required_outcomes=list(row.required_outcomes),
        family=row.family,
        period=row.period,
        line=row.line,
    )


def _normalize_matchbook_markets(event_payload: dict[str, Any], markets: list[dict[str, Any]]) -> list[CanonicalMarket]:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(event_payload)
    items: list[CanonicalMarket] = []
    for payload in markets:
        try:
            items.append(normalizer.normalize_market(event, payload))
        except VenueNormalizationError:
            continue
    return items


def _normalize_kalshi_markets(
    events: list[dict[str, Any]],
    *,
    series_by_ticker: dict[str, dict[str, Any]] | None = None,
) -> list[CanonicalMarket]:
    normalizer = KalshiNormalizer()
    series_map = dict(series_by_ticker or {})
    items: list[CanonicalMarket] = []
    for event_payload in events:
        ticker = str(event_payload.get("series_ticker") or "").strip()
        series = series_map.get(ticker)
        try:
            event = normalizer.normalize_event(event_payload, series=series)
        except VenueNormalizationError:
            continue
        payloads = list(event_payload.get("markets") or [])
        try:
            items.extend(
                normalizer.assemble_canonical_markets(
                    event,
                    payloads,
                    series=series,
                    event_payload=event_payload,
                )
            )
        except VenueNormalizationError:
            continue
    return items


def legacy_register_identities_from_payloads(
    *,
    matchbook_event: dict[str, Any],
    matchbook_markets: list[dict[str, Any]],
    kalshi_events: list[dict[str, Any]],
    series_by_ticker: dict[str, dict[str, Any]] | None = None,
    matcher: MarketMatcher | None = None,
) -> tuple[dict[str, RegisterNativeIdentity], list[str]]:
    """Accepted discovery/onboarding identity via the production matcher + register.

    Test/validation only. Does not create a second equivalence authority.
    """

    matcher = matcher or MarketMatcher()
    matchbook_markets_n = _normalize_matchbook_markets(matchbook_event, matchbook_markets)
    kalshi_markets_n = _normalize_kalshi_markets(
        kalshi_events, series_by_ticker=series_by_ticker
    )
    identities: dict[str, RegisterNativeIdentity] = {}
    forbidden: list[str] = []
    for left in matchbook_markets_n:
        for right in kalshi_markets_n:
            result = matcher.match(left, right)
            reasons_blob = " ".join(result.reasons).casefold()
            if any(token in reasons_blob for token in CONFIDENCE_REVIEW_TOKENS):
                if result.matched:
                    forbidden.append(
                        f"{left.source_market_id}->{right.source_market_id}:{reasons_blob}"
                    )
            if not result.matched:
                continue
            key = registered_canonical_key(left, right)
            if key is None:
                forbidden.append(
                    f"matched_without_register:{left.source_market_id}:{right.source_market_id}"
                )
                continue
            if REGISTER_ADMITTED_REASON not in result.reasons:
                forbidden.append(f"matched_without_register_reason:{key}")
            identities[key] = identity_from_canonical_pair(
                left, right, register_canonical_key=key
            )
    return identities, forbidden


def catalogue_register_identities(
    rows: list[ApprovedMarketCatalogueRow],
) -> dict[str, RegisterNativeIdentity]:
    return {
        row.register_canonical_key: identity_from_catalogue_row(row)
        for row in rows
        if row.row_state is CatalogueRowState.ACTIVE
    }


def compare_register_parity(
    legacy: dict[str, RegisterNativeIdentity],
    catalogue: dict[str, RegisterNativeIdentity],
    *,
    forbidden_admissions: list[str] | None = None,
) -> ParityReport:
    mismatches: list[ParityMismatch] = []
    missing = sorted(set(legacy) - set(catalogue))
    extra = sorted(set(catalogue) - set(legacy))
    for key in sorted(set(legacy) & set(catalogue)):
        left = legacy[key]
        right = catalogue[key]
        if left.identity_tuple() == right.identity_tuple():
            continue
        left_dump = left.model_dump()
        right_dump = right.model_dump()
        for field_name in left_dump:
            if left_dump[field_name] != right_dump[field_name]:
                mismatches.append(
                    ParityMismatch(
                        register_canonical_key=key,
                        field=field_name,
                        legacy=left_dump[field_name],
                        catalogue=right_dump[field_name],
                    )
                )
    forbidden = list(forbidden_admissions or [])
    matches = not mismatches and not missing and not extra and not forbidden
    return ParityReport(
        matches=matches,
        dual_matcher=False,
        legacy_keys=sorted(legacy),
        catalogue_keys=sorted(catalogue),
        mismatches=mismatches,
        missing_in_catalogue=missing,
        extra_in_catalogue=extra,
        forbidden_admissions=forbidden,
    )


def _row_reason(runtime: PriceEngineRuntimeItem | None, *, status: str) -> str:
    if runtime is None:
        return PriceEngineItemStatus.NOT_STARTED.value
    detail = (runtime.last_error_detail or "").strip()
    if detail == SCAN_BUDGET_EXHAUSTED_REASON:
        return SCAN_BUDGET_EXHAUSTED_REASON
    if status == PriceEngineItemStatus.RETRY_WAIT.value:
        return detail or PriceEngineItemStatus.RETRY_WAIT.value
    if status == PriceEngineItemStatus.DEFERRED.value:
        return detail or "provider_capacity_saturated"
    if status == PriceEngineItemStatus.REVALIDATION_NEEDED.value:
        return detail or (runtime.last_error_stage or PriceEngineItemStatus.REVALIDATION_NEEDED.value)
    if status == PriceEngineItemStatus.FAILED.value:
        return detail or (runtime.last_error_stage or PriceEngineItemStatus.FAILED.value)
    if status == PriceEngineItemStatus.EVALUATED.value:
        return PriceEngineItemStatus.EVALUATED.value
    if status == PriceEngineItemStatus.IN_FLIGHT.value:
        return PriceEngineItemStatus.IN_FLIGHT.value
    if status == PriceEngineItemStatus.DUE.value:
        if runtime.last_priced_at is None:
            return PriceEngineItemStatus.NOT_STARTED.value
        return PriceEngineItemStatus.DUE.value
    if status == PriceEngineItemStatus.NOT_STARTED.value:
        return PriceEngineItemStatus.NOT_STARTED.value
    return status or PriceEngineItemStatus.NOT_STARTED.value


def _runtime_status(runtime: PriceEngineRuntimeItem | None, *, now: datetime) -> str:
    if runtime is None:
        return PriceEngineItemStatus.NOT_STARTED.value
    if runtime.in_flight or runtime.status is PriceEngineItemStatus.IN_FLIGHT:
        return PriceEngineItemStatus.IN_FLIGHT.value
    if runtime.status is PriceEngineItemStatus.RETRY_WAIT or (
        runtime.next_retry_at is not None and now < runtime.next_retry_at
    ):
        return PriceEngineItemStatus.RETRY_WAIT.value
    return runtime.status.value


def soak_row_from_catalogue(
    row: ApprovedMarketCatalogueRow,
    runtime: PriceEngineRuntimeItem | None,
    *,
    now: datetime,
    previously_evaluated: bool = False,
) -> SoakRowState:
    status = _runtime_status(runtime, now=now)
    evaluated = previously_evaluated or (
        runtime is not None
        and (runtime.last_priced_at is not None or runtime.status is PriceEngineItemStatus.EVALUATED)
    )
    revalidation = None
    if runtime is not None and runtime.status is PriceEngineItemStatus.REVALIDATION_NEEDED:
        revalidation = runtime.last_error_detail or runtime.last_error_stage
    return SoakRowState(
        catalogue_row_id=row.catalogue_row_id,
        register_canonical_key=row.register_canonical_key,
        canonical_event_id=row.canonical_event_id,
        row_state=row.row_state.value,
        priority=None if runtime is None else runtime.priority.value,
        status=status,
        reason=_row_reason(runtime, status=status),
        evaluated_at_least_once=evaluated,
        last_priced_at=None if runtime is None else runtime.last_priced_at,
        last_error_stage=None if runtime is None else runtime.last_error_stage,
        last_error_detail=None if runtime is None else runtime.last_error_detail,
        revalidation_reason=revalidation,
    )


def capture_summary_from_reads(
    *,
    paper_autofill_enabled: bool,
    trades: list[Any] | None = None,
    activity: list[Any] | None = None,
    persist_failures: int = 0,
) -> SoakCaptureSummary:
    open_ids: list[str] = []
    for trade in trades or []:
        payload = trade if isinstance(trade, dict) else None
        state = trade.get("state") if payload is not None else getattr(trade, "state", None)
        state_value = getattr(state, "value", state)
        if str(state_value).upper() == "OPEN":
            opportunity = str(
                (payload or {}).get("opportunity_id")
                if payload is not None
                else getattr(trade, "opportunity_id", "") or ""
            )
            trade_id = str(
                (payload or {}).get("trade_id")
                if payload is not None
                else getattr(trade, "trade_id", "") or ""
            )
            open_ids.append(opportunity or trade_id)
    counts: dict[str, int] = {}
    for item in open_ids:
        counts[item] = counts.get(item, 0) + 1
    duplicates = sorted(key for key, count in counts.items() if count > 1 and key)
    rejected = 0
    attempted = 0
    for event in activity or []:
        payload = event if isinstance(event, dict) else None
        event_type = (
            event.get("event_type") if payload is not None else getattr(event, "event_type", None)
        )
        value = str(getattr(event_type, "value", event_type) or "").casefold()
        if value == "paper_fill_rejected":
            rejected += 1
        if value == "paper_fill_attempted":
            attempted += 1
    eligible_gap = 0
    if paper_autofill_enabled and attempted and not (open_ids or rejected):
        eligible_gap = attempted
    return SoakCaptureSummary(
        paper_autofill_enabled=paper_autofill_enabled,
        open_trades=len(open_ids),
        paper_fill_rejected=rejected,
        paper_fill_attempted=attempted,
        duplicate_open_ids=duplicates,
        eligible_without_open_or_reject=eligible_gap,
        persist_failures=persist_failures,
    )


def observer_snapshot(
    *,
    coordinator: Any,
    catalogue_store: Any | None,
    settings: Settings | None = None,
    build: ServingBuildInfo | dict[str, Any] | None = None,
    trades: list[Any] | None = None,
    activity: list[Any] | None = None,
    now: datetime | None = None,
    data_kind: str = DATA_CLASS_FIXTURE_DEMO,
    endpoint_samples: list[SoakEndpointSample] | None = None,
    soak_http_methods: list[str] | None = None,
) -> ScannerValidationSnapshot:
    """Read catalogue + in-memory engine items. Never prices or discovers."""

    resolved = settings or get_settings()
    observed_at = now or datetime.now(UTC)
    engine: CataloguePriceEngine | None = getattr(coordinator, "_price_engine", None)
    rows = list(catalogue_store.list_active()) if catalogue_store is not None else []
    runtime_by_id: dict[str, PriceEngineRuntimeItem] = {}
    if engine is not None:
        for item in engine.items():
            runtime_by_id[item.identity.catalogue_row_id] = item
    soak_rows = [
        soak_row_from_catalogue(row, runtime_by_id.get(row.catalogue_row_id), now=observed_at)
        for row in rows
    ]
    price_status = (
        engine.public_status(now=observed_at)
        if engine is not None
        else PriceEnginePublicStatus()
    )
    dumped = json.dumps(price_status.model_dump(), default=str)
    exhausted = dumped.count(SCAN_BUDGET_EXHAUSTED_REASON)
    persist_failures = price_status.hot.persist_failures + price_status.background.persist_failures
    live = coordinator.health_live_refresh_fields() if hasattr(coordinator, "health_live_refresh_fields") else {}
    status = getattr(coordinator, "status", None)
    promoted = int(getattr(getattr(status, "hot", None), "promoted_hot_count", 0) or 0)
    promotions = int(getattr(getattr(status, "hot", None), "hot_promotions", 0) or 0)
    build_payload: dict[str, Any]
    if isinstance(build, ServingBuildInfo):
        build_payload = build.as_public_dict()
    elif isinstance(build, dict):
        build_payload = dict(build)
    else:
        build_payload = get_serving_build_info().as_public_dict()
    revalidation = list(getattr(engine, "revalidation_requests", []) or []) if engine is not None else []
    return ScannerValidationSnapshot(
        observed_at=observed_at,
        data_kind=data_kind,
        mode=resolved.sports_hedge_mode,
        execution_enabled=bool(resolved.sports_hedge_execution_enabled),
        paper_autofill_enabled=bool(resolved.paper_autofill_enabled),
        build=build_payload,
        active_catalogue_row_count=len(rows),
        rows=soak_rows,
        price_engine=price_status,
        durable_queue=bool(getattr(price_status, "durable_queue", False)),
        hot_in_progress=bool(live.get("hot_in_progress")),
        background_in_progress=bool(live.get("background_in_progress")),
        universe_in_progress=bool(live.get("universe_in_progress")),
        promoted_hot_count=promoted,
        hot_promotions=promotions,
        revalidation_requests=revalidation,
        capture=capture_summary_from_reads(
            paper_autofill_enabled=bool(resolved.paper_autofill_enabled),
            trades=trades,
            activity=activity,
            persist_failures=persist_failures,
        ),
        venue_write=venue_write_boundary_evidence(
            settings=resolved, soak_http_methods=soak_http_methods or list(SOAK_HTTP_GET_ONLY)
        ),
        scan_budget_exhausted_price_engine=exhausted,
        endpoint_samples=list(endpoint_samples or []),
    )


def _json_contains_budget_exhausted(payload: Any) -> int:
    return json.dumps(payload, default=str).count(SCAN_BUDGET_EXHAUSTED_REASON)


def evaluate_soak_acceptance(report: SoakReport) -> list[SoakHardFail]:
    fails: list[SoakHardFail] = []
    if report.scan_budget_exhausted_price_engine:
        fails.append(
            SoakHardFail(
                code="scan_budget_exhausted",
                detail="price-engine path reported scan_budget_exhausted",
            )
        )
    if report.durable_queue:
        fails.append(SoakHardFail(code="durable_queue", detail="durable price queue present"))
    if report.venue_write.venue_write_invoked or report.venue_write.place_cancel_sign_reachable:
        fails.append(
            SoakHardFail(
                code="venue_write",
                detail="venue write/place/cancel/sign reachable or invoked",
            )
        )
    if report.venue_write.execution_enabled or not report.paper_only:
        fails.append(
            SoakHardFail(code="execution_boundary", detail="PAPER/read-only boundary not held")
        )
    if any(item.upper() in FORBIDDEN_SOAK_HTTP for item in report.venue_write.soak_http_methods):
        fails.append(
            SoakHardFail(
                code="status_triggered_work",
                detail="soak harness used a mutating HTTP method",
            )
        )
    for row in report.unevaluated_rows:
        if row.reason == SCAN_BUDGET_EXHAUSTED_REASON:
            fails.append(
                SoakHardFail(
                    code="scan_budget_exhausted_row",
                    detail=f"{row.catalogue_row_id} leftover as scan_budget_exhausted",
                )
            )
            continue
        if row.evaluated_at_least_once:
            continue
        if not row.reason or row.reason.casefold() in {"unknown", "silent"}:
            fails.append(
                SoakHardFail(
                    code="silent_active_row",
                    detail=f"{row.catalogue_row_id} has no truthful unevaluated reason",
                )
            )
            continue
        if row.reason not in TRUTHFUL_UNEVALUATED_REASONS and not row.last_error_detail:
            if row.status not in TRUTHFUL_UNEVALUATED_REASONS:
                fails.append(
                    SoakHardFail(
                        code="silent_active_row",
                        detail=(
                            f"{row.catalogue_row_id} status={row.status} reason={row.reason}"
                        ),
                    )
                )
    if report.capture.duplicate_open_ids:
        fails.append(
            SoakHardFail(
                code="duplicate_open",
                detail="duplicate OPEN/treasury lock ids: "
                + ",".join(report.capture.duplicate_open_ids),
            )
        )
    if report.capture.eligible_without_open_or_reject:
        fails.append(
            SoakHardFail(
                code="silent_eligible_capture",
                detail="eligible+autofill ON without OPEN or PAPER_FILL_REJECTED",
            )
        )
    if report.notes and "hot_blocked_universe" in report.notes:
        fails.append(
            SoakHardFail(
                code="hot_blocked_universe",
                detail="HOT prevented UNIVERSE progress during soak",
            )
        )
    if report.notes and "background_starvation" in report.notes:
        fails.append(
            SoakHardFail(
                code="background_starvation",
                detail="BACKGROUND received no work while ACTIVE rows existed",
            )
        )
    if report.notes and "unrelated_timeout_leftover" in report.notes:
        fails.append(
            SoakHardFail(
                code="unrelated_timeout_leftover",
                detail="one provider timeout leftover-marked unrelated items",
            )
        )
    if report.notes and "dual_matcher" in report.notes:
        fails.append(
            SoakHardFail(code="dual_matcher", detail="second matcher/equivalence authority")
        )
    if report.notes and "stale_projection_resurrection" in report.notes:
        fails.append(
            SoakHardFail(
                code="stale_projection",
                detail="stale pre-reset projection resurrection observed",
            )
        )
    return fails


def accumulate_soak_report(
    samples: list[ScannerValidationSnapshot],
    *,
    data_kind: str = DATA_CLASS_FIXTURE_DEMO,
    notes: list[str] | None = None,
) -> SoakReport:
    if not samples:
        report = SoakReport(data_kind=data_kind, notes=list(notes or []))
        report.hard_fails = evaluate_soak_acceptance(report)
        report.accepted = not report.hard_fails
        return report
    first = samples[0]
    last = samples[-1]
    evaluated_ids: set[str] = set()
    latest_by_id: dict[str, SoakRowState] = {}
    exhausted = 0
    overlap = 0
    promotions = 0
    endpoint_samples: list[SoakEndpointSample] = []
    reasons: list[str] = []
    for sample in samples:
        exhausted += sample.scan_budget_exhausted_price_engine
        exhausted += _json_contains_budget_exhausted(sample.price_engine.model_dump())
        if sample.hot_in_progress and sample.universe_in_progress:
            overlap += 1
        promotions = max(promotions, sample.promoted_hot_count, sample.hot_promotions)
        endpoint_samples.extend(sample.endpoint_samples)
        for row in sample.rows:
            latest_by_id[row.catalogue_row_id] = row
            if row.evaluated_at_least_once or row.status == PriceEngineItemStatus.EVALUATED.value:
                evaluated_ids.add(row.catalogue_row_id)
            if row.revalidation_reason:
                reasons.append(row.revalidation_reason)
        for item in sample.revalidation_requests:
            text = str(item.get("reason") or item.get("detail") or "").strip()
            if text:
                reasons.append(text)
    active_ids = set(latest_by_id)
    unevaluated = [
        latest_by_id[row_id]
        for row_id in sorted(active_ids - evaluated_ids)
    ]
    coverage = None
    if latest_by_id:
        coverage = round(len(evaluated_ids) / len(latest_by_id), 4)
    pe = last.price_engine
    report = SoakReport(
        data_kind=data_kind,
        paper_only=last.paper_only and not last.execution_enabled,
        execution_enabled=last.execution_enabled,
        started_at=first.observed_at,
        ended_at=last.observed_at,
        duration_seconds=max(0.0, (last.observed_at - first.observed_at).total_seconds()),
        sample_count=len(samples),
        observed_build_sha=str(last.build.get("git_sha") or "") or None,
        observed_build_branch=str(last.build.get("git_branch") or "") or None,
        active_catalogue_row_count=len(latest_by_id),
        rows_evaluated_at_least_once=len(evaluated_ids),
        coverage_ratio=coverage,
        unevaluated_rows=unevaluated,
        latest_rows=list(latest_by_id.values()),
        hot_evaluated=pe.hot.evaluated,
        hot_retry=pe.hot.retry_wait,
        hot_deferred=pe.hot.deferred,
        hot_not_started=pe.hot.not_started_this_cadence,
        background_evaluated=pe.background.evaluated,
        background_retry=pe.background.retry_wait,
        background_deferred=pe.background.deferred,
        background_not_started=pe.background.not_started_this_cadence,
        revalidation_needed=pe.hot.revalidation_needed + pe.background.revalidation_needed,
        revalidation_reasons=sorted(set(reasons)),
        provider_health={
            "hot": {
                "venue": pe.hot.venue_health,
                "operation": pe.hot.operation_health,
            },
            "background": {
                "venue": pe.background.venue_health,
                "operation": pe.background.operation_health,
            },
        },
        scan_budget_exhausted_price_engine=exhausted,
        hot_universe_overlap_samples=overlap,
        background_hot_promotions=promotions,
        capture=last.capture,
        endpoint_samples=endpoint_samples,
        venue_write=last.venue_write,
        durable_queue=any(sample.durable_queue for sample in samples),
        notes=list(notes or []),
    )
    report.hard_fails = evaluate_soak_acceptance(report)
    report.accepted = not report.hard_fails
    return report


def soak_harness_is_observer_only() -> bool:
    source = inspect.getsource(run_http_soak)
    if "collect_and_scan" in source:
        return False
    if "place_order" in source or "cancel_order" in source or "sign_wallet" in source:
        return False
    if "client.post(" in source or "client.put(" in source or "client.patch(" in source:
        return False
    if "client.delete(" in source:
        return False
    return "client.get(" in source


def _get_json(client: Any, url: str) -> tuple[SoakEndpointSample, Any | None]:
    started = monotonic()
    try:
        response = client.get(url)
        elapsed = (monotonic() - started) * 1000
        sample = SoakEndpointSample(
            path=url,
            status_code=int(response.status_code),
            available=200 <= int(response.status_code) < 300,
            elapsed_ms=round(elapsed, 2),
        )
        payload = None
        if sample.available:
            payload = response.json()
        return sample, payload
    except Exception as exc:  # noqa: BLE001 — observer must record transport failure
        elapsed = (monotonic() - started) * 1000
        return (
            SoakEndpointSample(
                path=url,
                available=False,
                elapsed_ms=round(elapsed, 2),
                error=str(exc),
            ),
            None,
        )


def snapshot_from_http_payloads(
    *,
    observed_at: datetime,
    health: dict[str, Any] | None,
    build: dict[str, Any] | None,
    live: dict[str, Any] | None,
    validation: dict[str, Any] | None,
    trades: list[Any] | None,
    activity: list[Any] | None,
    endpoint_samples: list[SoakEndpointSample],
    data_kind: str,
) -> ScannerValidationSnapshot:
    if validation:
        snapshot = ScannerValidationSnapshot.model_validate(validation)
        snapshot.observed_at = observed_at
        snapshot.data_kind = data_kind
        snapshot.endpoint_samples = endpoint_samples
        snapshot.venue_write.soak_http_methods = list(SOAK_HTTP_GET_ONLY)
        return snapshot
    live = live or {}
    price = live.get("price_engine") or {}
    health = health or {}
    rows = []
    return ScannerValidationSnapshot(
        observed_at=observed_at,
        data_kind=data_kind,
        mode=str(health.get("mode") or "paper"),
        execution_enabled=bool(health.get("execution_enabled")),
        paper_autofill_enabled=bool(health.get("paper_autofill_enabled")),
        build=dict(build or health.get("build") or {}),
        active_catalogue_row_count=int((price.get("hot") or {}).get("working_set") or 0)
        + int((price.get("background") or {}).get("working_set") or 0),
        rows=rows,
        price_engine=PriceEnginePublicStatus.model_validate(price) if price else PriceEnginePublicStatus(),
        durable_queue=bool(price.get("durable_queue", False)),
        hot_in_progress=bool((health.get("live_refresh") or {}).get("hot_in_progress") or live.get("hot", {}).get("cycle_in_progress")),
        background_in_progress=bool(
            (health.get("live_refresh") or {}).get("background_in_progress")
            or live.get("background", {}).get("cycle_in_progress")
        ),
        universe_in_progress=bool(
            (health.get("live_refresh") or {}).get("universe_in_progress")
            or live.get("universe", {}).get("cycle_in_progress")
        ),
        promoted_hot_count=int((live.get("hot") or {}).get("promoted_hot_count") or 0),
        hot_promotions=int((live.get("hot") or {}).get("hot_promotions") or 0),
        capture=capture_summary_from_reads(
            paper_autofill_enabled=bool(health.get("paper_autofill_enabled")),
            trades=trades,
            activity=activity,
        ),
        venue_write=venue_write_boundary_evidence(soak_http_methods=list(SOAK_HTTP_GET_ONLY)),
        scan_budget_exhausted_price_engine=_json_contains_budget_exhausted(price),
        endpoint_samples=endpoint_samples,
    )


def run_http_soak(
    *,
    base_url: str = "http://127.0.0.1:8000",
    duration_seconds: int = DEFAULT_SOAK_DURATION_SECONDS,
    interval_seconds: int = DEFAULT_SOAK_INTERVAL_SECONDS,
    data_kind: str = DATA_CLASS_OWNER_LIVE_OBSERVATION,
) -> SoakReport:
    """Poll cheap observer endpoints. Never POST collect or touch venue writes."""

    import httpx

    deadline = monotonic() + max(0, int(duration_seconds))
    samples: list[ScannerValidationSnapshot] = []
    base = base_url.rstrip("/") + "/"
    with httpx.Client(timeout=5.0) as client:
        while True:
            observed_at = datetime.now(UTC)
            health_sample, health = _get_json(client, urljoin(base, "health"))
            build_sample, build = _get_json(client, urljoin(base, "build-info"))
            live_sample, live = _get_json(client, urljoin(base, "paper/live-refresh"))
            validation_sample, validation = _get_json(
                client, urljoin(base, "paper/scanner-validation")
            )
            trades_sample, trades = _get_json(client, urljoin(base, "paper/trades/active"))
            activity_sample, activity = _get_json(
                client, urljoin(base, "paper/watchlist/activity?limit=100")
            )
            samples.append(
                snapshot_from_http_payloads(
                    observed_at=observed_at,
                    health=health if isinstance(health, dict) else None,
                    build=build if isinstance(build, dict) else None,
                    live=live if isinstance(live, dict) else None,
                    validation=validation if isinstance(validation, dict) else None,
                    trades=trades if isinstance(trades, list) else None,
                    activity=activity if isinstance(activity, list) else None,
                    endpoint_samples=[
                        health_sample,
                        build_sample,
                        live_sample,
                        validation_sample,
                        trades_sample,
                        activity_sample,
                    ],
                    data_kind=data_kind,
                )
            )
            if monotonic() >= deadline:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(max(0.0, float(interval_seconds)), remaining))
    notes: list[str] = []
    if data_kind == DATA_CLASS_OWNER_LIVE_OBSERVATION:
        notes.append("owner_live_observation; not fabricated by CI")
    return accumulate_soak_report(samples, data_kind=data_kind, notes=notes)


def pair_identity_uses_register_only() -> bool:
    source = inspect.getsource(pair_identity_from_markets)
    return "registered_canonical_key" in source and "min_mapping_confidence" not in source


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only PAPER scanner soak observer (Issue #350 Phase 6). "
            "Does not place, cancel, or sign venue orders."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=DEFAULT_SOAK_DURATION_SECONDS,
        help="Bounded soak length. Default 720s (12 minutes).",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_SOAK_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--output",
        default="",
        help="JSON report path. Default logs/scanner-phase6-soak.<utc>.json",
    )
    parser.add_argument(
        "--data-kind",
        default=DATA_CLASS_OWNER_LIVE_OBSERVATION,
        choices=(DATA_CLASS_OWNER_LIVE_OBSERVATION, DATA_CLASS_FIXTURE_DEMO),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = run_http_soak(
        base_url=args.base_url,
        duration_seconds=args.duration_seconds,
        interval_seconds=args.interval_seconds,
        data_kind=args.data_kind,
    )
    output = args.output.strip()
    if not output:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        logs = Path("logs")
        logs.mkdir(parents=True, exist_ok=True)
        output = str(logs / f"scanner-phase6-soak.{stamp}.json")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(path)
    print(
        json.dumps(
            {
                "accepted": report.accepted,
                "coverage_ratio": report.coverage_ratio,
                "active_catalogue_row_count": report.active_catalogue_row_count,
                "rows_evaluated_at_least_once": report.rows_evaluated_at_least_once,
                "hard_fails": [item.model_dump() for item in report.hard_fails],
                "data_kind": report.data_kind,
                "paper_only": report.paper_only,
            },
            indent=2,
        )
    )
    return 0 if report.accepted else 2


if __name__ == "__main__":
    sys.exit(main())
