export type Venue = "matchbook" | "polymarket" | "smarkets";

export type MarketFamily =
  | "match_result"
  | "draw_no_bet"
  | "double_chance"
  | "both_teams_to_score"
  | "total_goals"
  | "asian_handicap"
  | "team_total"
  | "correct_score"
  | "half_time_full_time"
  | "to_qualify"
  | "next_goal"
  | "corners"
  | "cards"
  | "player_props"
  | "unknown";

export type FootballPeriod = "full_time" | "first_half" | "second_half" | "extra_time" | "unknown";
export type SettlementScope =
  | "regulation_time"
  | "including_extra_time"
  | "including_penalties"
  | "period_only"
  | "unknown";

export type MarketSnapshot = {
  snapshot_id: string;
  observed_at: string;
  venue: Venue;
  canonical_event_id: string;
  canonical_market_id: string;
  canonical_outcome: string;
  market_family: MarketFamily;
  period: FootballPeriod;
  market_line?: string | number | null;
  settlement_scope: SettlementScope;
  settlement_key?: string | null;
  competition?: string | null;
  home_team?: string | null;
  away_team?: string | null;
  kickoff_utc?: string | null;
  decimal_odds: string | number;
  implied_probability: string | number;
  spread_decimal?: string | number | null;
  total_liquidity?: string | number | null;
};

export type PaperScanRecord = {
  record_id: string;
  scanned_at: string;
  canonical_event_id: string;
  canonical_market_id: string;
  competition: string;
  home_team: string;
  away_team: string;
  kickoff_utc: string;
  market_family: MarketFamily;
  period: FootballPeriod;
  line?: string | number | null;
  venues: Venue[];
  source_market_ids: string[];
  mapping_confidence: number;
  is_arbitrage: boolean;
  eligible_for_paper_simulation: boolean;
  gross_edge?: string | number | null;
  net_edge?: string | number | null;
  executable_stake_gbp?: string | number | null;
  guaranteed_profit_gbp?: string | number | null;
  execution_risk_score?: number | null;
  execution_risk_band?: string | null;
  rejection_reasons: string[];
  decision_json?: string;
};

export type PaperScanSummary = {
  since: string;
  scan_count: number;
  arbitrage_count: number;
  eligible_count: number;
  rejection_count: number;
  top_net_edge?: string | number | null;
  top_guaranteed_profit_gbp?: string | number | null;
  latest_scan_at?: string | null;
};

export type PaperCollectionIssue = {
  stage: string;
  venue?: Venue | null;
  source_id?: string | null;
  detail: string;
};

export type PaperCollectionDecision = {
  eligible_for_paper_simulation: boolean;
  canonical_event_id?: string | null;
  canonical_market_id?: string | null;
  rejection_reasons: string[];
};

export type PaperCollectionReport = {
  started_at: string;
  completed_at: string;
  discovery_source?: Venue;
  matching_venue?: Venue;
  raw_matchbook_events: number;
  raw_polymarket_events: number;
  normalized_matchbook_events: number;
  normalized_polymarket_events: number;
  matched_event_pairs: number;
  normalized_matchbook_markets: number;
  normalized_polymarket_markets: number;
  matched_market_pairs: number;
  order_books_fetched: number;
  paper_decisions: PaperCollectionDecision[];
  discovered_fixtures?: DiscoveredFixture[];
  issues: PaperCollectionIssue[];
};

export type DiscoveredFixture = {
  source: Venue;
  source_event_id: string;
  home_team: string;
  away_team: string;
  competition: string;
  kickoff_utc: string;
  polymarket_matched: boolean;
  fixture_status?: string | null;
  in_running?: boolean | null;
  live_score_supported: boolean;
  home_score?: number | null;
  away_score?: number | null;
  last_seen_at: string;
};

export type LiveRefreshStatus = {
  discovery_source: Venue;
  matching_venue: Venue;
  server_loop_enabled: boolean;
  interval_seconds: number;
  cycle_in_progress: boolean;
  last_started_at?: string | null;
  last_completed_at?: string | null;
  last_error?: string | null;
  last_matched_event_pairs?: number | null;
  last_matched_market_pairs?: number | null;
  last_paper_decisions?: number | null;
  last_issue_count?: number | null;
  live_scores: string;
  discovered_fixtures: DiscoveredFixture[];
};

export type ZeroRateBasis = "verified_zero" | "assumed_zero";

export type PaperFeeSnapshotRequest = {
  venue: Venue;
  profit_haircut_rate: string;
  zero_rate_basis?: ZeroRateBasis;
  source?: string;
  detail?: string;
};

export const DASHBOARD_ASSUMED_ZERO_DETAIL =
  "Dashboard-entered 0% is an operator assumption (assumed_zero), not a verified venue fee.";

export function dashboardFeeSnapshot(
  venue: Venue,
  profitHaircutRate: string,
): PaperFeeSnapshotRequest {
  const snapshot: PaperFeeSnapshotRequest = {
    venue,
    profit_haircut_rate: profitHaircutRate,
    source: "dashboard_input",
  };
  if (Number(profitHaircutRate) === 0) {
    snapshot.zero_rate_basis = "assumed_zero";
    snapshot.detail = DASHBOARD_ASSUMED_ZERO_DETAIL;
  }
  return snapshot;
}

export type PaperVenueCostRequest = {
  venue: Venue;
  action: "back" | "buy";
  fee_basis: "profit_commission";
  known_status: "known";
  source: string;
  rate: string;
  fee_scope: "per_quote";
  order_role: "not_applicable";
  currency: string;
  detail?: string;
};

export function dashboardVenueCost(
  venue: Venue,
  profitHaircutRate: string,
): PaperVenueCostRequest {
  const assumedZero = Number(profitHaircutRate) === 0;
  return {
    venue,
    action: venue === "polymarket" ? "buy" : "back",
    fee_basis: "profit_commission",
    known_status: "known",
    source: "dashboard_input",
    rate: profitHaircutRate,
    fee_scope: "per_quote",
    order_role: "not_applicable",
    currency: venue === "polymarket" ? "USD" : "GBP",
    detail: assumedZero ? DASHBOARD_ASSUMED_ZERO_DETAIL : undefined,
  };
}

export type PaperCollectionRequest = {
  fee_snapshots?: PaperFeeSnapshotRequest[];
  venue_costs?: PaperVenueCostRequest[];
  fx_snapshots?: Array<{
    currency: string;
    gbp_per_unit: string;
    source?: string;
  }>;
  capital_limit_gbp?: string;
  minimum_net_edge?: string;
  maximum_execution_risk?: number;
  max_event_pairs?: number;
  max_market_pairs_per_event?: number;
};

export type TrendSummary = {
  metric: string;
  sample_size: number;
  median: number | null;
  p25: number | null;
  p75: number | null;
  minimum: number | null;
  maximum: number | null;
  positive_rate: number | null;
  stability_iqr: number | null;
  sufficient_sample: boolean;
  observations: Array<{
    canonical_event_id: string;
    canonical_market_id: string;
    canonical_outcome: string;
    venue: Venue;
    market_family: MarketFamily;
    period: FootballPeriod;
    market_line?: string | number | null;
    settlement_scope: SettlementScope;
    value: number;
    start_at: string;
    end_at: string;
    snapshot_count: number;
  }>;
};

export type EventReaction = {
  annotation_id: string;
  canonical_event_id: string;
  category: string;
  occurred_at: string;
  expected_market_families: MarketFamily[];
  reactions: Array<{
    venue: Venue;
    market_family: MarketFamily;
    canonical_market_id: string;
    canonical_outcome: string;
    baseline_probability: string | number;
    first_response_seconds: number | null;
    peak_move_probability_points: string | number;
    time_to_peak_seconds: number | null;
    end_move_probability_points: string | number;
    retracement_fraction: number | null;
    observation_count: number;
  }>;
};

const API_BASE = process.env.NEXT_PUBLIC_SPORTS_HEDGE_API_URL ?? "http://localhost:8000";

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // Use the stable status fallback below when an upstream returned no JSON body.
  }
  return `Sports Hedge API request failed (${response.status})`;
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<T>;
}

export function getHistory(query = ""): Promise<MarketSnapshot[]> {
  return request(`/market-intelligence/history${query ? `?${query}` : ""}`);
}

export function getTrend(metric: string, query = ""): Promise<TrendSummary> {
  return request(`/market-intelligence/trends/${metric}${query ? `?${query}` : ""}`);
}

export function getEventReaction(eventId: string, annotationId: string): Promise<EventReaction> {
  return request(`/market-intelligence/events/${encodeURIComponent(eventId)}/reactions/${encodeURIComponent(annotationId)}`);
}

export function getPaperScans(query = "limit=100"): Promise<PaperScanRecord[]> {
  return request(`/paper/scans${query ? `?${query}` : ""}`);
}

export function getPaperScanSummary(query = ""): Promise<PaperScanSummary> {
  return request(`/paper/scans/summary${query ? `?${query}` : ""}`);
}

export type WatchlistOpportunityStatus =
  | "WATCHING"
  | "APPROACHING"
  | "TRIGGERED"
  | "PAPER_FILLING"
  | "PARTIAL"
  | "FILLED"
  | "CLOSED"
  | "EXPIRED"
  | "REJECTED";

export type WatchlistClassification =
  | "watch_candidate"
  | "near_opportunity"
  | "triggered_opportunity"
  | "rejected"
  | "paper_fill"
  | "closed"
  | "expired";

export type WatchlistLifecycleEventType =
  | "candidate_first_seen"
  | "moved_closer_to_trigger"
  | "moved_further_from_trigger"
  | "trigger_crossed"
  | "trigger_lost_before_fill"
  | "paper_fill_attempted"
  | "paper_fill_partial"
  | "paper_fill_complete"
  | "rejected_stale_quote"
  | "rejected_insufficient_depth"
  | "rejected_semantics"
  | "rejected_missing_costs"
  | "rejected_execution_risk"
  | "closed"
  | "expired";

export type WatchLeg = {
  outcome: string;
  venue: Venue;
  source_market_id: string;
  currency: string;
  native_stake?: string | number | null;
  gbp_per_unit?: string | number | null;
  gbp_stake?: string | number | null;
  net_decimal_odds?: string | number | null;
  cumulative_depth_gbp?: string | number | null;
};

export type NearOpportunity = {
  opportunity_id: string;
  canonical_event_id: string;
  canonical_market_id: string;
  settlement_key?: string | null;
  competition?: string | null;
  home_team?: string | null;
  away_team?: string | null;
  market_family?: MarketFamily | null;
  period?: FootballPeriod | null;
  venues: Venue[];
  legs: WatchLeg[];
  status: WatchlistOpportunityStatus;
  classification: WatchlistClassification;
  is_arbitrage?: boolean;
  trigger_net_edge: string | number;
  current_net_edge?: string | number | null;
  gross_edge?: string | number | null;
  distance_to_trigger_pp?: string | number | null;
  implied_probability_sum?: string | number | null;
  quote_age_ms?: number | null;
  quote_age_basis?: "source" | "retrieval" | "unknown" | string | null;
  limiting_depth_gbp?: string | number | null;
  limiting_leg_outcome?: string | null;
  capital_required_gbp?: string | number | null;
  guaranteed_profit_gbp?: string | number | null;
  execution_risk_score?: number | null;
  expected_lock_minutes?: string | number | null;
  first_seen_at: string;
  last_seen_at: string;
  rejection_reasons: string[];
  insufficiency_reasons: string[];
  fixture_discovery_source?: Venue | null;
  fixture_status?: string | null;
  in_running?: boolean | null;
  live_score_supported?: boolean;
  home_score?: number | null;
  away_score?: number | null;
  strike_narrative?: string | null;
  previous_net_edge?: string | number | null;
  previous_distance_to_trigger_pp?: string | number | null;
  observation_count?: number;
};

export type OpportunityLifecycleEvent = {
  event_id: string;
  opportunity_id: string;
  occurred_at: string;
  event_type: WatchlistLifecycleEventType;
  status: WatchlistOpportunityStatus;
  current_net_edge?: string | number | null;
  distance_to_trigger_pp?: string | number | null;
  detail?: string | null;
};

export async function runPaperCollection(
  payload: PaperCollectionRequest,
): Promise<PaperCollectionReport> {
  const response = await fetch(`${API_BASE}/paper/collect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperCollectionReport>;
}

export async function simulatePaperFill(payload: {
  opportunity_id: string;
  operator_note?: string;
  provenance?: "live_paper" | "fixture_demo";
}): Promise<unknown> {
  const response = await fetch(`${API_BASE}/paper/simulate-fill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      operator_note: "PAPER-ONLY explicit simulate fill",
      provenance: "live_paper",
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json();
}

export function getLiveRefreshStatus(): Promise<LiveRefreshStatus> {
  return request("/paper/live-refresh");
}

export function getNearWatchlist(query = "limit=25"): Promise<NearOpportunity[]> {
  return request(`/paper/watchlist/near${query ? `?${query}` : ""}`);
}

export function getTriggeredWatchlist(query = "limit=25"): Promise<NearOpportunity[]> {
  return request(`/paper/watchlist/triggered${query ? `?${query}` : ""}`);
}

export function getTrackedWatchlist(query = "limit=100"): Promise<NearOpportunity[]> {
  return request(`/paper/watchlist/tracked${query ? `?${query}` : ""}`);
}

export function getWatchlistActivity(query = "limit=100"): Promise<OpportunityLifecycleEvent[]> {
  return request(`/paper/watchlist/activity${query ? `?${query}` : ""}`);
}

export type LivePriorityAlertRow = {
  alert_id: string;
  opportunity_id: string;
  severity: string;
  lifecycle_state: string;
  capital_source?: string;
};

export function getLivePriorityAlerts(): Promise<LivePriorityAlertRow[]> {
  return request("/priority-alerts");
}

export type HistoricalCoverageReadModel = {
  data_class: "REAL_HISTORICAL" | "UNAVAILABLE" | string;
  facts_available: boolean;
  odds_available: boolean;
  analogue_model: string;
  analogue_note: string;
  movement_semantics: string;
  match_count?: number | null;
  stored_observation_count?: number | null;
  same_line_opening_closing_pairs?: number | null;
  asian_handicap_line_shifts?: number | null;
  competitions: { competition: string; season: string; matches: number }[];
  unavailable_reason?: string | null;
};

export function getHistoricalCoverage(): Promise<HistoricalCoverageReadModel> {
  return request("/research/historical/coverage");
}
