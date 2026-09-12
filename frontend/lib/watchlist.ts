import {
  NearOpportunity,
  OpportunityLifecycleEvent,
  WatchlistLifecycleEventType,
  WatchlistOpportunityStatus,
} from "./api";
import { ActivityEvent, ArbitrageOpportunity, OpportunityStatus } from "./arbitrage-ops";
import { money, number } from "./format";

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

function formatQuoteAge(ms: number | null | undefined): string | null {
  if (ms === null || ms === undefined) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function displayStatus(status: WatchlistOpportunityStatus): OpportunityStatus {
  return status;
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

  return {
    id: item.opportunity_id,
    provenance: "LIVE_PAPER",
    eventLabel: `${item.home_team ?? "Unknown"} v ${item.away_team ?? "Unknown"}`,
    competition: item.competition,
    marketLabel: family,
    settlement,
    venues: item.venues,
    netArb: number(item.current_net_edge),
    trigger: number(item.trigger_net_edge) ?? 0,
    distanceToTriggerPp: number(item.distance_to_trigger_pp),
    movement: null,
    capitalRequiredGbp: number(item.capital_required_gbp),
    expectedLock: formatLock(item.expected_lock_minutes),
    quoteFreshness: formatQuoteAge(item.quote_age_ms),
    executableDepth: depth === null ? null : money(depth),
    limitingLeg: item.limiting_leg_outcome ?? null,
    riskFlags: [...item.insufficiency_reasons, ...item.rejection_reasons],
    currencies,
    status: displayStatus(item.status),
    executable,
    scannedAt: item.last_seen_at,
    guaranteedProfitGbp: executable ? number(item.guaranteed_profit_gbp) : null,
    executionRisk: item.execution_risk_score != null ? String(item.execution_risk_score) : null,
  };
}

export function nearOpportunitiesFromWatchlist(items: NearOpportunity[]): ArbitrageOpportunity[] {
  return items.filter((item) => NEAR_STATUSES.has(item.status)).map(opportunityFromWatchlist);
}

export function triggeredOpportunitiesFromWatchlist(items: NearOpportunity[]): ArbitrageOpportunity[] {
  return items.filter((item) => TRIGGERED_STATUSES.has(item.status)).map(opportunityFromWatchlist);
}

const ACTIVITY_TITLES: Record<WatchlistLifecycleEventType, string> = {
  candidate_first_seen: "Entered near-arb watchlist",
  moved_closer_to_trigger: "Moved closer to trigger",
  moved_further_from_trigger: "Moved further from trigger",
  trigger_crossed: "Threshold crossed",
  trigger_lost_before_fill: "Trigger lost before fill",
  paper_fill_attempted: "Paper fill attempted",
  paper_fill_partial: "Partial paper fill",
  paper_fill_complete: "Paper position completed",
  rejected_stale_quote: "Rejected · stale quote",
  rejected_insufficient_depth: "Rejected · insufficient depth",
  rejected_semantics: "Rejected · semantics",
  rejected_missing_costs: "Rejected · missing costs",
  rejected_execution_risk: "Rejected · execution risk",
  closed: "Closed",
  expired: "Expired",
};

function activityKind(eventType: WatchlistLifecycleEventType): string {
  if (eventType === "candidate_first_seen") return "WATCHLIST_ENTERED";
  if (eventType === "trigger_crossed") return "THRESHOLD_CROSSED";
  if (eventType === "paper_fill_attempted") return "PAPER_FILL_ATTEMPTED";
  if (eventType === "paper_fill_partial") return "PARTIAL_FILL";
  if (eventType === "paper_fill_complete") return "PAPER_POSITION_COMPLETED";
  if (eventType === "closed") return "CLOSED";
  if (eventType.startsWith("rejected_")) return "REJECTED";
  return eventType.toUpperCase();
}

export function activityFromWatchlist(events: OpportunityLifecycleEvent[]): ActivityEvent[] {
  return events.map((item) => ({
    id: item.event_id,
    provenance: "LIVE_PAPER",
    at: item.occurred_at,
    kind: activityKind(item.event_type),
    title: ACTIVITY_TITLES[item.event_type] ?? item.event_type.replaceAll("_", " "),
    detail: item.detail?.trim() ? item.detail : `opportunity ${item.opportunity_id}`,
  }));
}
