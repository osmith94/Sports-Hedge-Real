import {
  NearOpportunity,
  OpportunityLifecycleEvent,
  Price2ActivityObservation,
  Venue,
  WatchlistLifecycleEventType,
  WatchlistOpportunityStatus,
} from "./api";
import {
  ActivityEvent,
  ActivityPrice2,
  ArbitrageOpportunity,
  OpportunityStatus,
} from "./arbitrage-ops";
import { venueShortLabel } from "./fixture-inventory-display";
import { formatLocalClockWithMs, money, nativeStake, number, percent } from "./format";
import { scanLaneLabel } from "./opportunity-monitor-display";

const NEAR_STATUSES = new Set<WatchlistOpportunityStatus>(["WATCHING", "APPROACHING"]);
const TRIGGERED_STATUSES = new Set<WatchlistOpportunityStatus>(["TRIGGERED"]);

function formatLock(minutes: string | number | null | undefined): string | null {
  const parsed = number(minutes);
  if (parsed === null) return null;
  if (parsed >= 60) {
    const hours = Math.floor(parsed / 60);
    const rest = Math.round(parsed - hours * 60);
    return rest ? `${hours}h ${rest}m` : `${hours}h`;
  }
  return `${Math.round(parsed)}m`;
}

function formatQuoteAge(
  ms: number | null | undefined,
  basis?: string | null,
): string | null {
  const age =
    ms === null || ms === undefined ? null : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  const parts = [age, basis, age || basis ? "at last evaluation" : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function displayStatus(status: WatchlistOpportunityStatus): OpportunityStatus {
  return status;
}

function narrativeMovement(value: string | null | undefined): "up" | "down" | "flat" | null {
  if (value === "approaching") return "up";
  if (value === "moving_away") return "down";
  if (value === "stable") return "flat";
  return null;
}

function liveScoreLabel(item: NearOpportunity): string {
  if (item.live_score_supported && item.home_score != null && item.away_score != null) {
    return `${item.home_score}–${item.away_score} · Matchbook`;
  }
  return "unavailable · not in Matchbook payload";
}

export function opportunityFromWatchlist(item: NearOpportunity): ArbitrageOpportunity {
  const executable = TRIGGERED_STATUSES.has(item.status);
  const family = item.market_family ? item.market_family.replaceAll("_", " ") : "market";
  const period = item.period ? item.period.replaceAll("_", " ") : "canonical settlement";
  const settlement = [item.settlement_key, period, item.classification.replaceAll("_", " ")]
    .filter(Boolean)
    .join(" · ");
  const currencies = Array.from(
    new Set(item.legs.map((leg) => leg.currency).filter((value): value is string => Boolean(value))),
  );
  const depth = number(item.limiting_depth_gbp);
  const narrative = item.strike_narrative ?? null;

  return {
    id: item.opportunity_id,
    provenance: "LIVE_PAPER",
    canonicalEventId: item.canonical_event_id || null,
    eventLabel: `${item.home_team ?? "Unknown"} v ${item.away_team ?? "Unknown"}`,
    competition: item.competition,
    marketLabel: family,
    settlement,
    venues: item.venues,
    netArb: number(item.current_net_edge),
    grossArb: number(item.gross_edge),
    trigger: number(item.trigger_net_edge) ?? 0,
    distanceToTriggerPp: number(item.distance_to_trigger_pp),
    movement: narrativeMovement(narrative),
    capitalRequiredGbp: number(item.capital_required_gbp),
    expectedLock: formatLock(item.expected_lock_minutes),
    quoteFreshness: formatQuoteAge(item.quote_age_ms, item.quote_age_basis),
    executableDepth: depth === null ? null : money(depth),
    limitingLeg: item.limiting_leg_outcome ?? null,
    riskFlags: [...item.insufficiency_reasons, ...item.rejection_reasons],
    currencies,
    status: displayStatus(item.status),
    executable,
    scannedAt: item.last_seen_at,
    guaranteedProfitGbp: executable ? number(item.guaranteed_profit_gbp) : null,
    executionRisk: item.execution_risk_score != null ? String(item.execution_risk_score) : null,
    discoverySource: item.fixture_discovery_source ?? "matchbook",
    fixtureStatus: item.fixture_status ?? null,
    inRunning: item.in_running ?? null,
    liveScoreLabel: liveScoreLabel(item),
    strikeNarrative: narrative,
    observationCount: item.observation_count ?? null,
    betActionable: item.bet_actionable === true,
    betBlockedReason: item.bet_blocked_reason ?? null,
  };
}

export function nearOpportunitiesFromWatchlist(items: NearOpportunity[]): ArbitrageOpportunity[] {
  return items.filter((item) => NEAR_STATUSES.has(item.status)).map(opportunityFromWatchlist);
}

export function triggeredOpportunitiesFromWatchlist(items: NearOpportunity[]): ArbitrageOpportunity[] {
  return items.filter((item) => TRIGGERED_STATUSES.has(item.status)).map(opportunityFromWatchlist);
}

export function trackedOpportunitiesFromWatchlist(items: NearOpportunity[]): ArbitrageOpportunity[] {
  return items.map(opportunityFromWatchlist);
}

const ACTIVITY_TITLES: Record<WatchlistLifecycleEventType, string> = {
  candidate_first_seen: "Entered near-arb watchlist",
  moved_closer_to_trigger: "Moved closer to trigger",
  moved_further_from_trigger: "Moved further from trigger",
  trigger_crossed: "Threshold crossed",
  trigger_lost_before_fill: "Trigger lost before fill",
  promoted_to_hot: "Promoted to HOT",
  qualifying_detected: "Qualifying opportunity",
  qualifying_lost: "Qualifying lost",
  qualifying_expired: "Radar expired",
  paper_eligible: "Paper eligible",
  paper_fill_attempted: "Paper fill attempted",
  paper_fill_partial: "Partial paper fill",
  paper_fill_complete: "Trade entered",
  paper_fill_rejected: "Paper capture rejected",
  rejected_stale_quote: "Rejected · stale quote",
  rejected_insufficient_depth: "Rejected · insufficient depth",
  rejected_semantics: "Rejected · semantics",
  rejected_missing_costs: "Rejected · missing costs",
  rejected_execution_risk: "Rejected · execution risk",
  closed: "Trade exited",
  expired: "Expired",
};

export const OPERATOR_ACTIVITY_EVENT_TYPES = [
  "promoted_to_hot",
  "qualifying_detected",
  "qualifying_lost",
  "qualifying_expired",
  "paper_eligible",
  "trigger_lost_before_fill",
  "paper_fill_complete",
  "closed",
] as const satisfies readonly WatchlistLifecycleEventType[];

export function isOperatorActivityEvent(
  eventType: string,
): eventType is (typeof OPERATOR_ACTIVITY_EVENT_TYPES)[number] {
  return (OPERATOR_ACTIVITY_EVENT_TYPES as readonly string[]).includes(eventType);
}

export function isVisibleOperatorActivityEvent(event: OpportunityLifecycleEvent): boolean {
  if (!isOperatorActivityEvent(event.event_type)) return false;
  if (event.event_type === "trigger_lost_before_fill") {
    return event.capture_eligible === true;
  }
  return true;
}

export function activitySubjectFromEvent(event: OpportunityLifecycleEvent): string | null {
  const fixture = event.fixture_label?.trim() || "";
  const market = (event.market_family ?? "").replaceAll("_", " ").trim();
  const parts = [fixture, market].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function activityDetail(event: OpportunityLifecycleEvent, subject: string | null): string {
  const raw = event.detail?.trim() ? event.detail.trim() : `opportunity ${event.opportunity_id}`;
  if (!subject) return raw;
  if (raw === subject) return "";
  if (raw.startsWith(`${subject} · `)) return raw.slice(subject.length + 3).trim();
  return raw;
}

function activityKind(eventType: WatchlistLifecycleEventType): string {
  if (eventType === "promoted_to_hot") return "PROMOTED_TO_HOT";
  if (eventType === "qualifying_detected") return "QUALIFYING_OPPORTUNITY";
  if (eventType === "qualifying_lost") return "QUALIFYING_LOST";
  if (eventType === "qualifying_expired") return "QUALIFYING_EXPIRED";
  if (eventType === "paper_eligible") return "PAPER_ELIGIBLE";
  if (eventType === "trigger_lost_before_fill") return "TRIGGER_LOST_BEFORE_FILL";
  if (eventType === "paper_fill_complete") return "TRADE_ENTERED";
  if (eventType === "closed") return "TRADE_EXITED";
  if (eventType === "candidate_first_seen") return "WATCHLIST_ENTERED";
  if (eventType === "trigger_crossed") return "THRESHOLD_CROSSED";
  if (eventType === "paper_fill_attempted") return "PAPER_FILL_ATTEMPTED";
  if (eventType === "paper_fill_partial") return "PARTIAL_FILL";
  if (eventType === "paper_fill_rejected") return "PAPER_CAPTURE_REJECTED";
  if (eventType === "expired") return "EXPIRED";
  if (eventType.startsWith("rejected_")) return "REJECTED";
  return eventType.toUpperCase();
}

export function lifecycleEventTitle(eventType: string): string {
  return ACTIVITY_TITLES[eventType as WatchlistLifecycleEventType] ?? eventType.replaceAll("_", " ");
}

export function canonicalEventIdFromHotOpportunityId(opportunityId: string): string | null {
  return opportunityId.startsWith("hot:") ? opportunityId.slice(4) || null : null;
}

export function activityHistoryPath(
  opportunityId: string,
  canonicalEventId?: string | null,
): string {
  const path = `/activity/${encodeURIComponent(opportunityId)}`;
  const canonical = canonicalEventId?.trim() || canonicalEventIdFromHotOpportunityId(opportunityId);
  if (!canonical) return path;
  return `${path}?canonical_event_id=${encodeURIComponent(canonical)}`;
}

const PAPER_FILL_EVENT_TYPES = new Set([
  "paper_fill_attempted",
  "paper_fill_partial",
  "paper_fill_complete",
  "paper_fill_rejected",
]);

export function attemptIdFromLifecycleEvent(event: OpportunityLifecycleEvent): string | null {
  const stored = event.attempt_id?.trim();
  if (stored) return stored;
  if (!PAPER_FILL_EVENT_TYPES.has(event.event_type)) return null;
  const prefix = `${event.opportunity_id}:${event.event_type}:`;
  if (!event.event_id.startsWith(prefix)) return null;
  return event.event_id.slice(prefix.length).trim() || null;
}

function qualifyingDiscoveryDetail(event: OpportunityLifecycleEvent): string | null {
  if (event.event_type !== "qualifying_detected") return null;
  const parts: string[] = [];
  const venues = (event.venue_pair ?? "")
    .split(",")
    .map((venue) => venue.trim())
    .filter(Boolean)
    .map((venue) => venueShortLabel(venue as Venue));
  if (venues.length) parts.push(venues.join(" / "));
  if (event.gross_edge != null) parts.push(`gross ${percent(event.gross_edge)}`);
  if (event.current_net_edge != null) parts.push(`net ${percent(event.current_net_edge)}`);
  if (event.limiting_depth_gbp != null) parts.push(`executable ${money(event.limiting_depth_gbp)}`);
  if (event.guaranteed_profit_gbp != null) {
    parts.push(`guaranteed ${money(event.guaranteed_profit_gbp)}`);
  }
  const lane = scanLaneLabel(event.pricing_lane);
  if (lane !== "—") parts.push(lane);
  return parts.length ? parts.join(" · ") : null;
}

export function activityFromWatchlist(events: OpportunityLifecycleEvent[]): ActivityEvent[] {
  return events.filter(isVisibleOperatorActivityEvent).map((item) => {
    const subject = activitySubjectFromEvent(item);
    const discovery = qualifyingDiscoveryDetail(item);
    return {
      id: item.event_id,
      provenance: "LIVE_PAPER",
      at: item.occurred_at,
      kind: activityKind(item.event_type),
      title: lifecycleEventTitle(item.event_type),
      subject,
      detail: discovery ?? activityDetail(item, subject),
      opportunityId: item.opportunity_id,
      eventType: item.event_type,
      missedTriggerEventId:
        item.event_type === "trigger_lost_before_fill" ? item.event_id : null,
      fixtureLabel: item.fixture_label ?? null,
      marketFamily: item.market_family ?? null,
      canonicalEventId: item.canonical_event_id ?? null,
      canonicalMarketId: item.canonical_market_id ?? null,
      attemptId: attemptIdFromLifecycleEvent(item),
    };
  });
}

export function visibleOpportunityIds(events: OpportunityLifecycleEvent[]): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const event of events) {
    if (!isVisibleOperatorActivityEvent(event)) continue;
    const id = event.opportunity_id?.trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
    if (ids.length >= 100) break;
  }
  return ids;
}

export function oldestVisibleOccurredAt(events: OpportunityLifecycleEvent[]): string | null {
  let oldest: string | null = null;
  let oldestMs = Number.POSITIVE_INFINITY;
  for (const event of events) {
    if (!isVisibleOperatorActivityEvent(event)) continue;
    const ms = Date.parse(event.occurred_at);
    if (!Number.isFinite(ms) || ms >= oldestMs) continue;
    oldestMs = ms;
    oldest = event.occurred_at;
  }
  return oldest;
}

function recorded(value: string | number | null | undefined, format?: (raw: string | number) => string): string {
  if (value === null || value === undefined || value === "") return "not recorded";
  return format ? format(value) : String(value);
}

export function price2Title(status: Price2ActivityObservation["status"]): string {
  if (status === "accepted") return "Price-2 accepted";
  if (status === "rejected") return "Price-2 rejected";
  return "Price-2 incomplete/unavailable";
}

export function price2Kind(status: Price2ActivityObservation["status"]): string {
  if (status === "accepted") return "PRICE2_ACCEPTED";
  if (status === "rejected") return "PRICE2_REJECTED";
  return "PRICE2_UNAVAILABLE";
}

const POLYMARKET_CONSTRAINTS_UNPROVEN = "polymarket_native_constraints_unproven";

export function price2CompactDetail(item: Price2ActivityObservation): string {
  if (item.source === "lifecycle_rejection" || item.status === "incomplete_unavailable") {
    const reason = item.rejection_reason?.trim() || "not recorded";
    return `recorded Price-2 miss · ${reason} · quotes not recorded`;
  }
  const parts: string[] = [];
  parts.push(`net ${recorded(item.net_edge, (value) => percent(value))}`);
  const thresholdLine = price2ThresholdLine(item);
  if (thresholdLine) parts.push(thresholdLine);
  if (item.execution_size != null && item.execution_size !== "") {
    parts.push(`size ${nativeStake(item.execution_size, item.execution_size_currency)}`);
  } else {
    parts.push("size not recorded");
  }
  parts.push(
    `guaranteed ${item.guaranteed_profit != null && item.guaranteed_profit !== "" ? money(item.guaranteed_profit) : "not recorded"}`,
  );
  parts.push(
    item.oldest_quote_age_ms == null ? "quote age not recorded" : `quote age ${item.oldest_quote_age_ms}ms`,
  );
  parts.push(item.skew_ms == null ? "skew not recorded" : `skew ${item.skew_ms}ms`);
  if (item.status === "rejected") {
    parts.push(item.rejection_reason?.trim() || "rejection reason not recorded");
    const native = price2NativeOrderLine(item);
    if (native) parts.push(native);
  } else if (item.filled) {
    parts.push("paper fill recorded separately");
  } else if (item.trade_linked) {
    parts.push("trade linked · fill not recorded");
  } else {
    parts.push("accepted · not a fill");
  }
  if (item.execution_cycle != null) parts.push(`cycle ${item.execution_cycle}`);
  return parts.join(" · ");
}

export function price2ThresholdLine(item: Price2ActivityObservation): string | null {
  const economics = item.economics_vs_threshold;
  if (economics === "below_configured_threshold") {
    return `below configured ${recorded(item.minimum_net_edge, (value) => percent(value))} threshold`;
  }
  if (economics === "meets_or_exceeds_configured_threshold") {
    return `meets configured ${recorded(item.minimum_net_edge, (value) => percent(value))} threshold`;
  }
  if (item.status === "rejected" && item.rejection_reason === POLYMARKET_CONSTRAINTS_UNPROVEN) {
    if (economics === "threshold_not_recorded" || item.minimum_net_edge == null) {
      return "configured threshold not recorded";
    }
  }
  return null;
}

export function price2NativeOrderLine(item: Price2ActivityObservation): string | null {
  const pmFails = (item.legs ?? []).filter(
    (leg) =>
      (leg.venue ?? "") === "polymarket" &&
      (leg.freeze_status === "not_frozen" ||
        (item.rejection_reason === POLYMARKET_CONSTRAINTS_UNPROVEN &&
          (leg.freeze_status === "details_not_recorded" || !leg.freeze_status))),
  );
  if (item.rejection_reason === POLYMARKET_CONSTRAINTS_UNPROVEN && pmFails.length === 0) {
    return "PM native order not proved: details not recorded";
  }
  if (pmFails.length === 0) return null;
  const reasons = pmFails.map((leg) => {
    if (leg.freeze_status === "details_not_recorded" || !leg.freeze_reason) {
      return "details not recorded";
    }
    return leg.freeze_reason.replaceAll("_", " ");
  });
  const unique = [...new Set(reasons)];
  return `PM native order not proved: ${unique.join(", ")}`;
}

export function price2TimingLine(
  item: Pick<ActivityPrice2, "startedAt" | "finishedAt" | "elapsedMs"> & { occurredAt?: string | null },
  nowMs?: number | null,
): string {
  const evaluated = item.finishedAt
    ? requireLocalClock(item.finishedAt, nowMs)
    : "evaluation time not recorded";
  const elapsed =
    item.elapsedMs == null
      ? "quote evaluation duration not recorded"
      : `quote evaluation ${item.elapsedMs} ms`;
  const audit = item.occurredAt
    ? `audit ${requireLocalClock(item.occurredAt, nowMs)}`
    : "audit time not recorded";
  return `Price-2 ${evaluated} · ${elapsed} · ${audit}`;
}

export const PRICE2_RECENT_HORIZON_MS = 45 * 60 * 1000;

export function price2ActivityQuery(
  opportunityIds: string[],
  oldestOccurredAt: string | null,
  nowMs: number = Date.now(),
): string {
  const floor = nowMs - PRICE2_RECENT_HORIZON_MS;
  const oldestMs = oldestOccurredAt ? Date.parse(oldestOccurredAt) : Number.NaN;
  const sinceMs = Number.isFinite(oldestMs) ? Math.min(oldestMs, floor) : floor;
  const params = new URLSearchParams({
    since: new Date(sinceMs).toISOString(),
    limit: "200",
    include_recent: "true",
  });
  if (opportunityIds.length) {
    params.set("opportunity_ids", opportunityIds.join(","));
  }
  return params.toString();
}

function requireLocalClock(iso: string, nowMs?: number | null): string {
  return formatLocalClockWithMs(iso, nowMs);
}

export function activityFromPrice2(item: Price2ActivityObservation): ActivityEvent {
  const fixture = item.fixture_label?.trim() || "";
  const market = (item.market_family ?? "").replaceAll("_", " ").trim();
  const subject = [fixture, market].filter(Boolean).join(" · ") || null;
  const price2: ActivityPrice2 = {
    snapshotId: item.snapshot_id ?? null,
    executionCycle: item.execution_cycle ?? null,
    cycleOutcome: item.cycle_outcome ?? null,
    tradeId: item.trade_id ?? null,
    status: item.status,
    filled: item.filled,
    tradeLinked: item.trade_linked === true,
    startedAt: item.started_at ?? null,
    finishedAt: item.finished_at ?? null,
    elapsedMs: item.elapsed_ms ?? null,
    netEdge: item.net_edge ?? null,
    guaranteedProfit: item.guaranteed_profit ?? null,
    executionSize: item.execution_size ?? null,
    executionSizeCurrency: item.execution_size_currency ?? null,
    oldestQuoteAgeMs: item.oldest_quote_age_ms ?? null,
    skewMs: item.skew_ms ?? null,
    rejectionReason: item.rejection_reason ?? null,
    minimumNetEdge: item.minimum_net_edge ?? null,
    economicsVsThreshold: item.economics_vs_threshold ?? null,
    nativeOrderFreezeRecorded: item.native_order_freeze_recorded === true,
    source: item.source,
    dataKind: "historical_recorded",
    legs: (item.legs ?? []).map((leg) => ({
      venue: leg.venue ?? null,
      outcome: leg.outcome ?? null,
      displayedOdds: leg.displayed_odds == null ? null : String(leg.displayed_odds),
      requestedStake: leg.requested_stake == null ? null : String(leg.requested_stake),
      stakeCurrency: leg.stake_currency ?? null,
      retrievedAt: leg.retrieved_at ?? null,
      quoteAgeMs: leg.quote_age_ms ?? null,
      slotWaitMs: leg.timing_match === "native_id" ? (leg.slot_wait_ms ?? null) : null,
      ioMs: leg.timing_match === "native_id" ? (leg.io_ms ?? null) : null,
      timingMatch: leg.timing_match === "native_id" ? "native_id" : null,
      nativeMarketId: leg.native_market_id ?? null,
      nativeRunnerId: leg.native_runner_id ?? null,
      nativeFrozen: typeof leg.native_frozen === "boolean" ? leg.native_frozen : null,
      freezeStatus: leg.freeze_status ?? "details_not_recorded",
      freezeReason: leg.freeze_reason ?? null,
      observedTickSize: leg.observed_tick_size ?? null,
      observedMinimumShares: leg.observed_minimum_shares ?? null,
      intendedNativeStake: leg.intended_native_stake ?? null,
      intendedNativeShares: leg.intended_native_shares ?? null,
      intendedLimitPrice: leg.intended_limit_price ?? null,
    })),
    venueTimings: (item.venue_timings ?? []).map((timing) => ({
      venue: timing.venue,
      slotWaitMs: timing.slot_wait_ms ?? null,
      ioMs: timing.io_ms ?? null,
      callCount: timing.call_count ?? 0,
    })),
  };
  return {
    id: item.observation_id,
    provenance: "LIVE_PAPER",
    at: item.occurred_at,
    kind: price2Kind(item.status),
    title: price2Title(item.status),
    subject,
    detail: price2CompactDetail(item),
    opportunityId: item.opportunity_id,
    eventType: `price2_${item.status}`,
    missedTriggerEventId: null,
    fixtureLabel: item.fixture_label ?? null,
    marketFamily: item.market_family ?? null,
    canonicalEventId: item.canonical_event_id ?? null,
    canonicalMarketId: item.canonical_market_id ?? null,
    attemptId: item.snapshot_id ?? null,
    price2,
  };
}

export function mergeOperatorActivity(
  lifecycle: ActivityEvent[],
  price2: Price2ActivityObservation[],
): ActivityEvent[] {
  const extra = price2.map(activityFromPrice2);
  return [...lifecycle, ...extra].sort((left, right) => {
    const delta = Date.parse(right.at) - Date.parse(left.at);
    if (delta !== 0) return delta;
    return left.id < right.id ? 1 : left.id > right.id ? -1 : 0;
  });
}
