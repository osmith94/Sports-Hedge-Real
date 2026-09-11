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

export type MarketSnapshot = {
  snapshot_id: string;
  observed_at: string;
  venue: Venue;
  canonical_event_id: string;
  canonical_market_id: string;
  canonical_outcome: string;
  market_family: MarketFamily;
  competition?: string | null;
  home_team?: string | null;
  away_team?: string | null;
  kickoff_utc?: string | null;
  decimal_odds: string | number;
  implied_probability: string | number;
  spread_decimal?: string | number | null;
  total_liquidity?: string | number | null;
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

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Sports Hedge API request failed (${response.status})`);
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
