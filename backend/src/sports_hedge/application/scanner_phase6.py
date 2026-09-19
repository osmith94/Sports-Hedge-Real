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
CORE_OBSERVER_PATHS = (
    "/health",
    "/build-info",
    "/paper/live-refresh",
    "/paper/scanner-validation",
)
CAPTURE_OBSERVER_PATHS = (
    "/paper/trades/active",
    "/paper/watchlist/activity",
)
PROGRESSED_TRADE_STATES = frozenset(
    {"OPEN", "PARTIAL", "CLOSED", "AWAITING_MANUAL_EXTERNAL"}
)
CAPTURE_ATTEMPT_EVENTS = frozenset({"paper_fill_attempted"})
CAPTURE_REJECT_EVENTS = frozenset({"paper_fill_rejected", "paper_entry_rejected"})
CAPTURE_PROGRESS_EVENTS = frozenset({"paper_fill_complete", "paper_fill_partial"})
TIMEOUT_ERROR_TOKENS = (
    "timeout",
    "timed out",
    "market_timeout",
    "provider_timeout",
)
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
    content_version: int = 1
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

    def coverage_identity(self) -> str:
        return f"{self.catalogue_row_id}:{self.content_version}"


class SoakEndpointSample(BaseModel):
    path: str
    status_code: int | None = None
    available: bool = False
    elapsed_ms: float | None = None
    error: str | None = None


class SoakCaptureOpportunity(BaseModel):
    """Durable capture outcome for one opportunity/attempt identity."""

    opportunity_id: str
    attempted: bool = False
    open: bool = False
    rejected: bool = False
    progressed: bool = False
    duplicate_open: bool = False
    silent: bool = False
    trade_ids: list[str] = Field(default_factory=list)
    attempt_event_ids: list[str] = Field(default_factory=list)
    reject_event_ids: list[str] = Field(default_factory=list)


class SoakCaptureSummary(BaseModel):
    paper_autofill_enabled: bool = False
    evidence_usable: bool = True
    open_trades: int = 0
    paper_fill_rejected: int = 0
    paper_fill_attempted: int = 0
    duplicate_open_ids: list[str] = Field(default_factory=list)
    eligible_without_open_or_reject: int = 0
    silent_opportunity_ids: list[str] = Field(default_factory=list)
    persist_failures: int = 0
    opportunities: list[SoakCaptureOpportunity] = Field(default_factory=list)


class SoakLaneProgress(BaseModel):
    """Read-only lane timestamps/counters. Not scheduler authority."""

    hot_last_started_at: datetime | None = None
    hot_last_completed_at: datetime | None = None
    hot_next_due_at: datetime | None = None
    universe_last_started_at: datetime | None = None
    universe_last_completed_at: datetime | None = None
    universe_next_due_at: datetime | None = None
    universe_discovered_total: int = 0
    universe_remaining: int = 0
    universe_generation_work_used_s: float = 0
    universe_resume_cursor: str | None = None
    background_last_started_at: datetime | None = None
    background_last_completed_at: datetime | None = None
    background_next_due_at: datetime | None = None
    background_working_set: int = 0


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
    usable: bool = True
    evidence_errors: list[str] = Field(default_factory=list)
    coordinator_catalogue_bound: bool | None = None
    coordinator_price_engine_bound: bool | None = None
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
    promoted_hot_ids: list[str] = Field(default_factory=list)
    promoted_hot_row_ids: list[str] = Field(default_factory=list)
    lane_progress: SoakLaneProgress = Field(default_factory=SoakLaneProgress)
    revalidation_requests: list[dict[str, str]] = Field(default_factory=list)
    capture: SoakCaptureSummary = Field(default_factory=SoakCaptureSummary)
    venue_write: VenueWriteEvidence = Field(default_factory=VenueWriteEvidence)
    scan_budget_exhausted_price_engine: int = 0
    endpoint_samples: list[SoakEndpointSample] = Field(default_factory=list)


class SoakReport(BaseModel):
    """Bounded observer report. Never scheduler authority."""

    schema_version: int = 2
    issue: int = PHASE6_ISSUE
    data_kind: str = DATA_CLASS_FIXTURE_DEMO
    paper_only: bool = True
    execution_enabled: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float = 0
    sample_count: int = 0
    usable_sample_count: int = 0
    observed_build_sha: str | None = None
    observed_build_branch: str | None = None
    observed_build_shas: list[str] = Field(default_factory=list)
    build_sha_changed: bool = False
    coordinator_catalogue_bound: bool | None = None
    coordinator_price_engine_bound: bool | None = None
    active_catalogue_row_count: int = 0
    catalogue_count_mismatches: int = 0
    rows_evaluated_at_least_once: int = 0
    coverage_ratio: float | None = None
    evaluated_identities: list[str] = Field(default_factory=list)
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
    hot_work_samples: int = 0
    background_work_samples: int = 0
    universe_progress_samples: int = 0
    background_hot_promotions: int = 0
    promoted_hot_ids_baseline: list[str] = Field(default_factory=list)
    promoted_hot_ids_observed: list[str] = Field(default_factory=list)
    lane_progress_first: SoakLaneProgress | None = None
    lane_progress_last: SoakLaneProgress | None = None
    capture: SoakCaptureSummary = Field(default_factory=SoakCaptureSummary)
    endpoint_samples: list[SoakEndpointSample] = Field(default_factory=list)
    venue_write: VenueWriteEvidence = Field(default_factory=VenueWriteEvidence)
    durable_queue: bool = False
    evidence_errors: list[str] = Field(default_factory=list)
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
    version_matches = (
        runtime is not None and runtime.identity.content_version == row.content_version
    )
    matched = runtime if version_matches else None
    status = _runtime_status(matched, now=now)
    evaluated = previously_evaluated or (
        matched is not None
        and (matched.last_priced_at is not None or matched.status is PriceEngineItemStatus.EVALUATED)
    )
    revalidation = None
    if matched is not None and matched.status is PriceEngineItemStatus.REVALIDATION_NEEDED:
        revalidation = matched.last_error_detail or matched.last_error_stage
    return SoakRowState(
        catalogue_row_id=row.catalogue_row_id,
        content_version=row.content_version,
        register_canonical_key=row.register_canonical_key,
        canonical_event_id=row.canonical_event_id,
        row_state=row.row_state.value,
        priority=None if matched is None else matched.priority.value,
        status=status,
        reason=_row_reason(matched, status=status),
        evaluated_at_least_once=evaluated,
        last_priced_at=None if matched is None else matched.last_priced_at,
        last_error_stage=None if matched is None else matched.last_error_stage,
        last_error_detail=None if matched is None else matched.last_error_detail,
        revalidation_reason=revalidation,
    )


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _normalize_trades(trades: list[Any] | dict[str, Any] | None) -> list[Any]:
    if trades is None:
        return []
    if isinstance(trades, dict):
        for key in ("items", "trades", "results"):
            value = trades.get(key)
            if isinstance(value, list):
                return value
        return []
    return list(trades)


def _normalize_activity(activity: list[Any] | dict[str, Any] | None) -> list[Any]:
    if activity is None:
        return []
    if isinstance(activity, dict):
        for key in ("items", "events", "activity", "results"):
            value = activity.get(key)
            if isinstance(value, list):
                return value
        return []
    return list(activity)


def _opportunity_bucket(
    buckets: dict[str, SoakCaptureOpportunity], opportunity_id: str
) -> SoakCaptureOpportunity:
    existing = buckets.get(opportunity_id)
    if existing is None:
        existing = SoakCaptureOpportunity(opportunity_id=opportunity_id)
        buckets[opportunity_id] = existing
    return existing


def _finalize_capture_buckets(
    buckets: dict[str, SoakCaptureOpportunity],
    *,
    paper_autofill_enabled: bool,
    persist_failures: int,
    evidence_usable: bool,
) -> SoakCaptureSummary:
    silent_ids: list[str] = []
    duplicates: list[str] = []
    open_count = 0
    rejected = 0
    attempted = 0
    opportunities: list[SoakCaptureOpportunity] = []
    for opportunity_id, item in sorted(buckets.items()):
        unique_trades = list(dict.fromkeys(item.trade_ids))
        item.trade_ids = unique_trades
        item.attempt_event_ids = list(dict.fromkeys(item.attempt_event_ids))
        item.reject_event_ids = list(dict.fromkeys(item.reject_event_ids))
        item.duplicate_open = item.open and len(unique_trades) > 1
        item.progressed = item.progressed or item.open
        item.silent = bool(
            paper_autofill_enabled and item.attempted and not (item.open or item.progressed or item.rejected)
        )
        if item.silent:
            silent_ids.append(opportunity_id)
        if item.duplicate_open:
            duplicates.append(opportunity_id)
        if item.open:
            open_count += 1
        if item.rejected:
            rejected += 1
        if item.attempted:
            attempted += 1
        opportunities.append(item)
    return SoakCaptureSummary(
        paper_autofill_enabled=paper_autofill_enabled,
        evidence_usable=evidence_usable,
        open_trades=open_count,
        paper_fill_rejected=rejected,
        paper_fill_attempted=attempted,
        duplicate_open_ids=duplicates,
        eligible_without_open_or_reject=len(silent_ids),
        silent_opportunity_ids=silent_ids,
        persist_failures=persist_failures,
        opportunities=opportunities,
    )


def capture_summary_from_reads(
    *,
    paper_autofill_enabled: bool,
    trades: list[Any] | dict[str, Any] | None = None,
    activity: list[Any] | dict[str, Any] | None = None,
    persist_failures: int = 0,
    evidence_usable: bool = True,
) -> SoakCaptureSummary:
    buckets: dict[str, SoakCaptureOpportunity] = {}
    for trade in _normalize_trades(trades):
        opportunity = str(_field(trade, "opportunity_id") or _field(trade, "canonical_market_id") or "")
        trade_id = str(_field(trade, "trade_id") or "")
        state = _field(trade, "state")
        state_value = str(getattr(state, "value", state) or "").upper()
        identity = opportunity or trade_id
        if not identity:
            continue
        bucket = _opportunity_bucket(buckets, identity)
        if state_value == "OPEN":
            bucket.open = True
            if trade_id and trade_id not in bucket.trade_ids:
                bucket.trade_ids.append(trade_id)
        if state_value in PROGRESSED_TRADE_STATES:
            bucket.progressed = True
    for event in _normalize_activity(activity):
        opportunity = str(_field(event, "opportunity_id") or "")
        event_id = str(_field(event, "event_id") or "")
        event_type = _field(event, "event_type")
        value = str(getattr(event_type, "value", event_type) or "").casefold()
        if not opportunity:
            continue
        bucket = _opportunity_bucket(buckets, opportunity)
        if value in CAPTURE_ATTEMPT_EVENTS:
            bucket.attempted = True
            if event_id:
                bucket.attempt_event_ids.append(event_id)
        if value in CAPTURE_REJECT_EVENTS:
            bucket.rejected = True
            if event_id:
                bucket.reject_event_ids.append(event_id)
        if value in CAPTURE_PROGRESS_EVENTS:
            bucket.progressed = True
    return _finalize_capture_buckets(
        buckets,
        paper_autofill_enabled=paper_autofill_enabled,
        persist_failures=persist_failures,
        evidence_usable=evidence_usable,
    )


def merge_capture_summaries(*summaries: SoakCaptureSummary) -> SoakCaptureSummary:
    buckets: dict[str, SoakCaptureOpportunity] = {}
    paper_autofill = False
    persist_failures = 0
    evidence_usable = True
    for summary in summaries:
        paper_autofill = paper_autofill or summary.paper_autofill_enabled
        persist_failures = max(persist_failures, summary.persist_failures)
        evidence_usable = evidence_usable and summary.evidence_usable
        for item in summary.opportunities:
            bucket = _opportunity_bucket(buckets, item.opportunity_id)
            bucket.attempted = bucket.attempted or item.attempted
            bucket.open = bucket.open or item.open
            bucket.rejected = bucket.rejected or item.rejected
            bucket.progressed = bucket.progressed or item.progressed
            for trade_id in item.trade_ids:
                if trade_id not in bucket.trade_ids:
                    bucket.trade_ids.append(trade_id)
            for event_id in item.attempt_event_ids:
                if event_id not in bucket.attempt_event_ids:
                    bucket.attempt_event_ids.append(event_id)
            for event_id in item.reject_event_ids:
                if event_id not in bucket.reject_event_ids:
                    bucket.reject_event_ids.append(event_id)
    return _finalize_capture_buckets(
        buckets,
        paper_autofill_enabled=paper_autofill,
        persist_failures=persist_failures,
        evidence_usable=evidence_usable,
    )


def _lane_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return None


def lane_progress_from_coordinator(coordinator: Any) -> SoakLaneProgress:
    """Read already-published lane timestamps. Does not call public_status()."""

    status = getattr(coordinator, "status", None)
    hot = getattr(status, "hot", None)
    universe = getattr(status, "universe", None)
    background = getattr(status, "background", None)
    return SoakLaneProgress(
        hot_last_started_at=_lane_datetime(getattr(hot, "last_started_at", None)),
        hot_last_completed_at=_lane_datetime(getattr(hot, "last_completed_at", None)),
        hot_next_due_at=_lane_datetime(
            getattr(coordinator, "_next_hot_due", None) or getattr(hot, "next_due_at", None)
        ),
        universe_last_started_at=_lane_datetime(getattr(universe, "last_started_at", None)),
        universe_last_completed_at=_lane_datetime(getattr(universe, "last_completed_at", None)),
        universe_next_due_at=_lane_datetime(
            getattr(coordinator, "_next_universe_due", None) or getattr(universe, "next_due_at", None)
        ),
        universe_discovered_total=int(
            getattr(coordinator, "_universe_discovered_total", None)
            or getattr(universe, "discovered_total", 0)
            or 0
        ),
        universe_remaining=int(getattr(universe, "remaining", 0) or 0),
        universe_generation_work_used_s=float(
            getattr(coordinator, "_universe_work_used", None)
            or getattr(universe, "generation_work_used_s", 0)
            or 0
        ),
        universe_resume_cursor=(
            getattr(coordinator, "_universe_cursor", None)
            or getattr(universe, "resume_cursor", None)
        ),
        background_last_started_at=_lane_datetime(getattr(background, "last_started_at", None)),
        background_last_completed_at=_lane_datetime(getattr(background, "last_completed_at", None)),
        background_next_due_at=_lane_datetime(
            getattr(coordinator, "_next_background_due", None)
            or getattr(background, "next_due_at", None)
        ),
        background_working_set=int(getattr(background, "evaluated_count", 0) or 0),
    )


def lane_progress_from_live(live: dict[str, Any] | None) -> SoakLaneProgress:
    payload = live or {}
    hot = payload.get("hot") or {}
    universe = payload.get("universe") or {}
    background = payload.get("background") or {}
    return SoakLaneProgress(
        hot_last_started_at=_lane_datetime(hot.get("last_started_at")),
        hot_last_completed_at=_lane_datetime(hot.get("last_completed_at")),
        hot_next_due_at=_lane_datetime(hot.get("next_due_at")),
        universe_last_started_at=_lane_datetime(universe.get("last_started_at")),
        universe_last_completed_at=_lane_datetime(universe.get("last_completed_at")),
        universe_next_due_at=_lane_datetime(universe.get("next_due_at")),
        universe_discovered_total=int(universe.get("discovered_total") or 0),
        universe_remaining=int(universe.get("remaining") or 0),
        universe_generation_work_used_s=float(universe.get("generation_work_used_s") or 0),
        universe_resume_cursor=universe.get("resume_cursor"),
        background_last_started_at=_lane_datetime(background.get("last_started_at")),
        background_last_completed_at=_lane_datetime(background.get("last_completed_at")),
        background_next_due_at=_lane_datetime(background.get("next_due_at")),
        background_working_set=int((payload.get("price_engine") or {}).get("background", {}).get("working_set") or 0),
    )


def _promoted_identities(engine: CataloguePriceEngine | None) -> tuple[list[str], list[str]]:
    if engine is None:
        return [], []
    fixture_ids = sorted(str(item) for item in getattr(engine, "_promoted_hot_ids", set()) or [])
    row_ids = sorted(str(item) for item in getattr(engine, "_promoted_hot_rows", {}) or [])
    return fixture_ids, row_ids


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
    coordinator_catalogue_bound: bool | None = None,
) -> ScannerValidationSnapshot:
    """Read catalogue + in-memory engine items. Never prices, discovers, or binds."""

    resolved = settings or get_settings()
    observed_at = now or datetime.now(UTC)
    engine: CataloguePriceEngine | None = getattr(coordinator, "_price_engine", None)
    bound_store = getattr(coordinator, "_catalogue_store", None)
    catalogue_bound = bound_store is not None if coordinator_catalogue_bound is None else coordinator_catalogue_bound
    engine_bound = engine is not None
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
    promoted_ids, promoted_rows = _promoted_identities(engine)
    build_payload: dict[str, Any]
    if isinstance(build, ServingBuildInfo):
        build_payload = build.as_public_dict()
    elif isinstance(build, dict):
        build_payload = dict(build)
    else:
        build_payload = get_serving_build_info().as_public_dict()
    revalidation = list(getattr(engine, "revalidation_requests", []) or []) if engine is not None else []
    evidence_errors: list[str] = []
    if len(soak_rows) != len(rows):
        evidence_errors.append("catalogue_count_mismatch")
    if not catalogue_bound:
        evidence_errors.append("coordinator_catalogue_unbound")
    if not engine_bound:
        evidence_errors.append("coordinator_price_engine_unbound")
    lane = lane_progress_from_coordinator(coordinator)
    if engine is not None:
        lane = lane.model_copy(update={"background_working_set": price_status.background.working_set})
    usable = "catalogue_count_mismatch" not in evidence_errors
    return ScannerValidationSnapshot(
        observed_at=observed_at,
        data_kind=data_kind,
        mode=resolved.sports_hedge_mode,
        execution_enabled=bool(resolved.sports_hedge_execution_enabled),
        paper_autofill_enabled=bool(resolved.paper_autofill_enabled),
        build=build_payload,
        usable=usable,
        evidence_errors=evidence_errors,
        coordinator_catalogue_bound=catalogue_bound,
        coordinator_price_engine_bound=engine_bound,
        active_catalogue_row_count=len(rows),
        rows=soak_rows,
        price_engine=price_status,
        durable_queue=bool(getattr(price_status, "durable_queue", False)),
        hot_in_progress=bool(live.get("hot_in_progress")),
        background_in_progress=bool(live.get("background_in_progress")),
        universe_in_progress=bool(live.get("universe_in_progress")),
        promoted_hot_count=promoted,
        hot_promotions=promotions,
        promoted_hot_ids=promoted_ids,
        promoted_hot_row_ids=promoted_rows,
        lane_progress=lane,
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


def _endpoint_path(sample: SoakEndpointSample) -> str:
    raw = sample.path or ""
    if "://" in raw:
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        raw = parsed.path or raw
    path = raw.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    return path


def _endpoint_attempted(samples: list[SoakEndpointSample], suffix: str) -> bool:
    return any(_endpoint_path(item).endswith(suffix) for item in samples)


def _endpoint_available(samples: list[SoakEndpointSample], suffix: str) -> bool:
    return any(
        item.available and _endpoint_path(item).endswith(suffix) for item in samples
    )


def _has_timeout_token(text: str | None) -> bool:
    blob = (text or "").casefold()
    return any(token in blob for token in TIMEOUT_ERROR_TOKENS)


def _universe_due(progress: SoakLaneProgress, observed_at: datetime) -> bool:
    due = progress.universe_next_due_at
    if due is None:
        return True
    return due <= observed_at


def _universe_progressed(previous: SoakLaneProgress, current: SoakLaneProgress) -> bool:
    if current.universe_last_completed_at != previous.universe_last_completed_at and current.universe_last_completed_at:
        return True
    if current.universe_generation_work_used_s > previous.universe_generation_work_used_s:
        return True
    if current.universe_discovered_total > previous.universe_discovered_total:
        return True
    if current.universe_resume_cursor and current.universe_resume_cursor != previous.universe_resume_cursor:
        return True
    if current.universe_remaining and previous.universe_remaining and current.universe_remaining < previous.universe_remaining:
        return True
    return False


def _build_sha(sample: ScannerValidationSnapshot) -> str | None:
    sha = str(sample.build.get("git_sha") or sample.build.get("sha") or "").strip()
    return sha or None


def evaluate_soak_acceptance(report: SoakReport) -> list[SoakHardFail]:
    fails: list[SoakHardFail] = []
    if report.usable_sample_count <= 0 or report.sample_count <= 0:
        fails.append(
            SoakHardFail(
                code="no_usable_samples",
                detail="no usable observer samples; soak cannot pass closed",
            )
        )
    missing_core = [
        path
        for path in CORE_OBSERVER_PATHS
        if _endpoint_attempted(report.endpoint_samples, path)
        and not _endpoint_available(report.endpoint_samples, path)
    ]
    if report.data_kind == DATA_CLASS_OWNER_LIVE_OBSERVATION:
        missing_core = [
            path
            for path in CORE_OBSERVER_PATHS
            if not _endpoint_available(report.endpoint_samples, path)
        ]
    if missing_core:
        fails.append(
            SoakHardFail(
                code="core_endpoints_unavailable",
                detail="core observer endpoints unavailable: " + ",".join(missing_core),
            )
        )
    if _endpoint_attempted(report.endpoint_samples, "/paper/scanner-validation") and not _endpoint_available(
        report.endpoint_samples, "/paper/scanner-validation"
    ):
        fails.append(
            SoakHardFail(
                code="scanner_validation_unavailable",
                detail="scanner-validation missing; empty catalogue was not assumed",
            )
        )
    if report.catalogue_count_mismatches:
        fails.append(
            SoakHardFail(
                code="catalogue_count_mismatch",
                detail="stated ACTIVE count disagrees with row payload",
            )
        )
    if report.build_sha_changed:
        fails.append(
            SoakHardFail(
                code="build_sha_changed",
                detail="serving build SHA changed mid-soak: "
                + ",".join(report.observed_build_shas),
            )
        )
    if report.usable_sample_count > 0 and report.active_catalogue_row_count <= 0:
        fails.append(
            SoakHardFail(
                code="no_active_catalogue_rows",
                detail="no ACTIVE catalogue row observed; insufficient rollout evidence",
            )
        )
    if report.coordinator_catalogue_bound is False or report.coordinator_price_engine_bound is False:
        fails.append(
            SoakHardFail(
                code="coordinator_unbound",
                detail="GET observed unbound coordinator catalogue/engine wiring",
            )
        )
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
    if report.capture.paper_autofill_enabled and not report.capture.evidence_usable:
        fails.append(
            SoakHardFail(
                code="capture_evidence_unavailable",
                detail="autofill ON but capture reads were unusable",
            )
        )
    missing_capture = [
        path
        for path in CAPTURE_OBSERVER_PATHS
        if report.capture.paper_autofill_enabled
        and _endpoint_attempted(report.endpoint_samples, path)
        and not _endpoint_available(report.endpoint_samples, path)
    ]
    if missing_capture and "capture_evidence_unavailable" not in {item.code for item in fails}:
        fails.append(
            SoakHardFail(
                code="capture_evidence_unavailable",
                detail="autofill ON but capture observer endpoints unavailable: "
                + ",".join(missing_capture),
            )
        )
    for row in report.unevaluated_rows:
        if row.reason == SCAN_BUDGET_EXHAUSTED_REASON:
            fails.append(
                SoakHardFail(
                    code="scan_budget_exhausted_row",
                    detail=f"{row.coverage_identity()} leftover as scan_budget_exhausted",
                )
            )
            continue
        if row.evaluated_at_least_once:
            continue
        if not row.reason or row.reason.casefold() in {"unknown", "silent"}:
            fails.append(
                SoakHardFail(
                    code="silent_active_row",
                    detail=f"{row.coverage_identity()} has no truthful unevaluated reason",
                )
            )
            continue
        if row.reason not in TRUTHFUL_UNEVALUATED_REASONS and not row.last_error_detail:
            if row.status not in TRUTHFUL_UNEVALUATED_REASONS:
                fails.append(
                    SoakHardFail(
                        code="silent_active_row",
                        detail=(
                            f"{row.coverage_identity()} status={row.status} reason={row.reason}"
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
    if report.capture.eligible_without_open_or_reject or report.capture.silent_opportunity_ids:
        silent = ",".join(report.capture.silent_opportunity_ids) or "per-opportunity gap"
        fails.append(
            SoakHardFail(
                code="silent_eligible_capture",
                detail="eligible+autofill ON without OPEN or PAPER_FILL_REJECTED: " + silent,
            )
        )
    had_background = any(
        (row.priority or "").casefold() == "background" for row in report.latest_rows
    )
    if report.lane_progress_last is not None and report.lane_progress_last.background_working_set:
        had_background = True
    background_due = False
    if (
        report.lane_progress_last is not None
        and report.ended_at is not None
        and report.lane_progress_last.background_next_due_at is not None
    ):
        background_due = report.lane_progress_last.background_next_due_at <= report.ended_at
    starve = had_background and report.background_work_samples <= 0 and report.background_evaluated <= 0
    if starve and (background_due or report.data_kind == DATA_CLASS_OWNER_LIVE_OBSERVATION):
        fails.append(
            SoakHardFail(
                code="background_starvation",
                detail="BACKGROUND ACTIVE rows existed but BACKGROUND received no work",
            )
        )
    universe_due = False
    if report.lane_progress_last is not None and report.ended_at is not None:
        universe_due = _universe_due(report.lane_progress_last, report.ended_at)
    if (
        report.hot_work_samples
        and universe_due
        and report.universe_progress_samples <= 0
        and report.hot_universe_overlap_samples <= 0
    ):
        fails.append(
            SoakHardFail(
                code="hot_blocked_universe",
                detail="HOT worked while UNIVERSE was due and made no observed progress",
            )
        )
    timeout_rows = [
        row
        for row in report.latest_rows
        if _has_timeout_token(row.last_error_detail)
        or _has_timeout_token(row.reason)
        or _has_timeout_token(row.last_error_stage)
    ]
    leftover_unrelated = [
        row
        for row in report.latest_rows
        if row.coverage_identity() not in {item.coverage_identity() for item in timeout_rows}
        and (
            row.reason == SCAN_BUDGET_EXHAUSTED_REASON
            or (
                row.status in {"failed", "unknown"}
                and not row.last_error_detail
                and not row.evaluated_at_least_once
            )
        )
    ]
    if timeout_rows and leftover_unrelated:
        fails.append(
            SoakHardFail(
                code="unrelated_timeout_leftover",
                detail="provider timeout leftover-marked unrelated items",
            )
        )
    for extra in report.evidence_errors:
        if extra in {
            "dual_matcher",
            "stale_projection_resurrection",
            "status_triggered_work",
        }:
            fails.append(SoakHardFail(code=extra, detail=extra))
    return fails


def accumulate_soak_report(
    samples: list[ScannerValidationSnapshot],
    *,
    data_kind: str = DATA_CLASS_FIXTURE_DEMO,
    notes: list[str] | None = None,
) -> SoakReport:
    if not samples:
        report = SoakReport(
            data_kind=data_kind,
            notes=list(notes or []),
            sample_count=0,
            usable_sample_count=0,
            evidence_errors=["no_usable_samples"],
        )
        report.hard_fails = evaluate_soak_acceptance(report)
        report.accepted = False
        return report
    usable = [sample for sample in samples if sample.usable]
    endpoint_samples: list[SoakEndpointSample] = []
    evidence_errors: list[str] = []
    exhausted = 0
    overlap = 0
    hot_work = 0
    background_work = 0
    universe_progress = 0
    mismatches = 0
    evaluated_ids: set[str] = set()
    capture = SoakCaptureSummary()
    for sample in samples:
        endpoint_samples.extend(sample.endpoint_samples)
        evidence_errors.extend(sample.evidence_errors)
        exhausted += sample.scan_budget_exhausted_price_engine
        exhausted += _json_contains_budget_exhausted(sample.price_engine.model_dump())
        if sample.active_catalogue_row_count != len(sample.rows):
            mismatches += 1
            evidence_errors.append("catalogue_count_mismatch")
        capture = merge_capture_summaries(capture, sample.capture)
        if sample.hot_in_progress:
            hot_work += 1
        if sample.background_in_progress or sample.price_engine.background.evaluated:
            background_work += 1
        if sample.universe_in_progress:
            universe_progress += 1
        if sample.hot_in_progress and sample.universe_in_progress:
            overlap += 1
        for row in sample.rows:
            if row.evaluated_at_least_once or row.status == PriceEngineItemStatus.EVALUATED.value:
                evaluated_ids.add(row.coverage_identity())
            if (row.priority or "").casefold() == "background" and (
                row.evaluated_at_least_once
                or row.last_priced_at is not None
                or row.status
                in {
                    PriceEngineItemStatus.EVALUATED.value,
                    PriceEngineItemStatus.RETRY_WAIT.value,
                    PriceEngineItemStatus.DEFERRED.value,
                    PriceEngineItemStatus.IN_FLIGHT.value,
                }
            ):
                background_work += 1
    if not usable:
        last = samples[-1]
        report = SoakReport(
            data_kind=data_kind,
            paper_only=last.paper_only and not last.execution_enabled,
            execution_enabled=last.execution_enabled,
            started_at=samples[0].observed_at,
            ended_at=last.observed_at,
            duration_seconds=max(0.0, (last.observed_at - samples[0].observed_at).total_seconds()),
            sample_count=len(samples),
            usable_sample_count=0,
            observed_build_sha=_build_sha(last),
            observed_build_branch=str(last.build.get("git_branch") or "") or None,
            observed_build_shas=list(dict.fromkeys(sha for sha in (_build_sha(item) for item in samples) if sha)),
            catalogue_count_mismatches=mismatches,
            endpoint_samples=endpoint_samples,
            venue_write=last.venue_write,
            capture=capture,
            evidence_errors=list(dict.fromkeys(evidence_errors or ["no_usable_samples"])),
            notes=list(notes or []),
        )
        report.hard_fails = evaluate_soak_acceptance(report)
        report.accepted = False
        return report
    first = usable[0]
    last = usable[-1]
    current_rows = {row.coverage_identity(): row for row in last.rows}
    unevaluated = [
        row for key, row in sorted(current_rows.items()) if key not in evaluated_ids
    ]
    coverage = None
    if current_rows:
        covered = sum(1 for key in current_rows if key in evaluated_ids)
        coverage = round(covered / len(current_rows), 4)
    shas = list(dict.fromkeys(sha for sha in (_build_sha(item) for item in usable) if sha))
    sha_changed = len(shas) > 1
    reasons: list[str] = []
    for sample in usable:
        for row in sample.rows:
            if row.revalidation_reason:
                reasons.append(row.revalidation_reason)
        for item in sample.revalidation_requests:
            text = str(item.get("reason") or item.get("detail") or "").strip()
            if text:
                reasons.append(text)
    baseline_ids = list(first.promoted_hot_ids)
    observed_new: list[str] = []
    seen_new: set[str] = set()
    for sample in usable[1:]:
        for ident in sample.promoted_hot_ids:
            if ident not in first.promoted_hot_ids and ident not in seen_new:
                seen_new.add(ident)
                observed_new.append(ident)
    first_lane = first.lane_progress
    last_lane = last.lane_progress
    for previous, current in zip(usable, usable[1:]):
        if _universe_progressed(previous.lane_progress, current.lane_progress):
            universe_progress += 1
        if current.price_engine.hot.evaluated > previous.price_engine.hot.evaluated:
            hot_work += 1
        if current.price_engine.background.evaluated > previous.price_engine.background.evaluated:
            background_work += 1
    pe = last.price_engine
    if pe.background.evaluated or pe.background.retry_wait or pe.background.deferred:
        background_work = max(background_work, 1)
    if pe.hot.evaluated:
        hot_work = max(hot_work, 1)
    report = SoakReport(
        data_kind=data_kind,
        paper_only=last.paper_only and not last.execution_enabled,
        execution_enabled=last.execution_enabled,
        started_at=first.observed_at,
        ended_at=last.observed_at,
        duration_seconds=max(0.0, (last.observed_at - first.observed_at).total_seconds()),
        sample_count=len(samples),
        usable_sample_count=len(usable),
        observed_build_sha=_build_sha(last),
        observed_build_branch=str(last.build.get("git_branch") or "") or None,
        observed_build_shas=shas,
        build_sha_changed=sha_changed,
        coordinator_catalogue_bound=last.coordinator_catalogue_bound,
        coordinator_price_engine_bound=last.coordinator_price_engine_bound,
        active_catalogue_row_count=last.active_catalogue_row_count,
        catalogue_count_mismatches=mismatches,
        rows_evaluated_at_least_once=sum(1 for key in current_rows if key in evaluated_ids),
        coverage_ratio=coverage,
        evaluated_identities=sorted(evaluated_ids),
        unevaluated_rows=unevaluated,
        latest_rows=list(current_rows.values()),
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
        hot_work_samples=hot_work,
        background_work_samples=background_work,
        universe_progress_samples=universe_progress,
        background_hot_promotions=len(observed_new),
        promoted_hot_ids_baseline=baseline_ids,
        promoted_hot_ids_observed=observed_new,
        lane_progress_first=first_lane,
        lane_progress_last=last_lane,
        capture=capture,
        endpoint_samples=endpoint_samples,
        venue_write=last.venue_write,
        durable_queue=any(sample.durable_queue for sample in samples),
        evidence_errors=list(dict.fromkeys(evidence_errors)),
        notes=list(notes or []),
    )
    report.hard_fails = evaluate_soak_acceptance(report)
    report.accepted = not report.hard_fails
    return report


def soak_harness_is_observer_only() -> bool:
    soak_src = inspect.getsource(run_http_soak)
    get_src = inspect.getsource(_get_json)
    source = soak_src + "\n" + get_src
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
    trades: list[Any] | dict[str, Any] | None,
    activity: list[Any] | dict[str, Any] | None,
    endpoint_samples: list[SoakEndpointSample],
    data_kind: str,
    trades_available: bool | None = None,
    activity_available: bool | None = None,
) -> ScannerValidationSnapshot:
    capture_available = True
    if trades_available is False or activity_available is False:
        capture_available = False
    extra_capture = capture_summary_from_reads(
        paper_autofill_enabled=bool((health or {}).get("paper_autofill_enabled")),
        trades=trades,
        activity=activity,
        evidence_usable=capture_available,
    )
    if validation:
        snapshot = ScannerValidationSnapshot.model_validate(validation)
        snapshot.observed_at = observed_at
        snapshot.data_kind = data_kind
        snapshot.endpoint_samples = endpoint_samples
        snapshot.venue_write.soak_http_methods = list(SOAK_HTTP_GET_ONLY)
        errors = list(snapshot.evidence_errors)
        if snapshot.active_catalogue_row_count != len(snapshot.rows):
            errors.append("catalogue_count_mismatch")
        if build:
            build_sha = str(build.get("git_sha") or "").strip()
            snap_sha = str(snapshot.build.get("git_sha") or "").strip()
            if build_sha and snap_sha and build_sha != snap_sha:
                errors.append("build_sha_inconsistent")
            if not snapshot.build:
                snapshot.build = dict(build)
        snapshot.capture = merge_capture_summaries(snapshot.capture, extra_capture)
        if snapshot.paper_autofill_enabled and not capture_available:
            errors.append("capture_evidence_unavailable")
            snapshot.capture.evidence_usable = False
        snapshot.evidence_errors = list(dict.fromkeys(errors))
        blocking = {
            "catalogue_count_mismatch",
            "build_sha_inconsistent",
            "scanner_validation_unavailable",
        }
        snapshot.usable = snapshot.usable and not any(item in blocking for item in snapshot.evidence_errors)
        return snapshot
    errors = ["scanner_validation_unavailable"]
    health = health or {}
    live = live or {}
    build_payload = dict(build or health.get("build") or {})
    return ScannerValidationSnapshot(
        observed_at=observed_at,
        data_kind=data_kind,
        usable=False,
        evidence_errors=errors,
        mode=str(health.get("mode") or "paper"),
        execution_enabled=bool(health.get("execution_enabled")),
        paper_autofill_enabled=bool(health.get("paper_autofill_enabled")),
        build=build_payload,
        active_catalogue_row_count=0,
        rows=[],
        price_engine=PriceEnginePublicStatus(),
        durable_queue=False,
        hot_in_progress=bool((health.get("live_refresh") or {}).get("hot_in_progress") or live.get("hot", {}).get("cycle_in_progress")),
        background_in_progress=bool(
            (health.get("live_refresh") or {}).get("background_in_progress")
            or live.get("background", {}).get("cycle_in_progress")
        ),
        universe_in_progress=bool(
            (health.get("live_refresh") or {}).get("universe_in_progress")
            or live.get("universe", {}).get("cycle_in_progress")
        ),
        lane_progress=lane_progress_from_live(live),
        capture=extra_capture,
        venue_write=venue_write_boundary_evidence(soak_http_methods=list(SOAK_HTTP_GET_ONLY)),
        scan_budget_exhausted_price_engine=0,
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
                    trades=trades if isinstance(trades, (list, dict)) else None,
                    activity=activity if isinstance(activity, (list, dict)) else None,
                    trades_available=trades_sample.available,
                    activity_available=activity_sample.available,
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
