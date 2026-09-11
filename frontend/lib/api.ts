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
  issues: PaperCollectionIssue[];
};

export type PaperCollectionRequest = {
  fee_snapshots?: Array<{
    venue: Venue;
    profit_haircut_rate: string;
    source?: string;
    detail?: string;
  }>;
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
