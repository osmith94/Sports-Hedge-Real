export type Venue = "matchbook" | "polymarket" | "smarkets" | "kalshi";

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
  | "first_team_to_score"
  | "corners"
  | "cards"
  | "player_props"
  | "game_winner"
  | "point_spread"
  | "total_points"
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

export type ActiveTradeTimelineItem = {
  event_id: string;
  occurred_at: string;
  event_type: string;
  reason_code: string;
  operator_copy: string;
  trade_id: string;
  cycle_id?: string | null;
  venue?: string | null;
  data_kind?: string;
};

export type ActiveTradeJournalEvent = ActiveTradeTimelineItem & {
  sequence?: number;
  schema_version?: string;
  serving_git_sha?: string | null;
  opportunity_id?: string | null;
  canonical_event_id?: string | null;
  canonical_market_id?: string | null;
  competition?: string | null;
  market_family?: string | null;
  line?: string | null;
  active_phase?: string | null;
  tranche_id?: string | null;
  fill_id?: string | null;
  payload?: Record<string, unknown>;
};

export type PaperScanCycleRecord = {
  cycle_id: string;
  started_at: string;
  completed_at: string;
  scan_lane: string;
  duration_ms: number;
  fixture_count: number;
  evaluated_count: number;
  not_evaluated_count: number;
  matched_event_pairs: number;
  matched_market_pairs: number;
  paper_decision_count: number;
  qualifying_arb_count: number;
  venue_health?: Record<string, string>;
  degraded?: boolean;
  last_error?: string | null;
  universe_generation_id?: number | null;
  resume_cursor?: string | null;
  completeness?: string | null;
  generation_resume?: boolean | null;
  generation_work_used_s?: number | null;
  operator_summary?: string | null;
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
  scan_lane?: "hot" | "universe" | string | null;
  discovery_source?: Venue;
  discovery_mode?: string;
  matching_venue?: Venue;
  matching_venues?: Venue[];
  enabled_venues?: Venue[];
  raw_matchbook_events: number;
  raw_polymarket_events: number;
  raw_kalshi_events?: number;
  normalized_matchbook_events: number;
  normalized_polymarket_events: number;
  normalized_kalshi_events?: number;
  matched_event_pairs: number;
  pair_counts?: Record<string, number>;
  normalized_matchbook_markets: number;
  normalized_polymarket_markets: number;
  normalized_kalshi_markets?: number;
  matched_market_pairs: number;
  order_books_fetched: number;
  skipped_out_of_scope?: number;
  skipped_by_reason?: Record<string, number>;
  rejected_competition_labels?: string[];
  target_coverage?: Record<string, Record<string, number>>;
  config_warnings?: string[];
  operator_summary?: string;
  venue_health?: Record<string, string>;
  qualifying_arbs?: number;
  paper_decisions: PaperCollectionDecision[];
  discovered_fixtures?: DiscoveredFixture[];
  issues: PaperCollectionIssue[];
  scan_diagnostics?: {
    collection_kind?: string | null;
    matching_coverage?: {
      fixtures?: number;
      single_venue_clusters?: number;
      cross_venue_clusters?: number;
      matched_event_pairs?: number;
      inventory_cross_venue_fixtures?: number;
      equivalent_markets?: number;
      qualifying_arbs?: number;
      matching_state?: string;
      catalogue_by_archetype?: Record<string, Record<string, number>>;
    };
    [key: string]: unknown;
  };
};

export type DiscoveredFixture = {
  source: Venue;
  source_event_id: string;
  canonical_event_id: string;
  home_team: string;
  away_team: string;
  competition: string;
  sport?: string | null;
  target_competition_code?: string | null;
  kickoff_utc: string;
  matchbook_matched?: boolean;
  polymarket_matched: boolean;
  kalshi_matched?: boolean;
  fixture_status?: string | null;
  fixture_status_source?: string | null;
  in_running?: boolean | null;
  live_score_supported: boolean;
  home_score?: number | null;
  away_score?: number | null;
  last_seen_at: string;
  matched_market_count: number;
  discovered_market_count?: number;
  matched_equivalent_count?: number | null;
  qualifying_market_count?: number;
  near_executable_market_count?: number;
  market_family?: string | null;
  outcome_context?: string | null;
  best_arb_market?: string | null;
  headline_band?: string | null;
  best_matchbook_price?: string | number | null;
  best_polymarket_price?: string | number | null;
  best_kalshi_price?: string | number | null;
  current_net_edge?: string | number | null;
  trigger_net_edge?: string | number | null;
  distance_to_trigger_pp?: string | number | null;
  quote_age_ms?: number | null;
  quote_age_basis?: string | null;
  execution_risk_score?: number | null;
  execution_risk_band?: string | null;
  execution_risk_reasons?: string[];
  no_comparison_reason?: string | null;
  solver_is_arbitrage: boolean;
  opportunity_state?: string;
  market_evaluation_state?: string | null;
  market_evaluation_reason?: string | null;
  scan_lane?: string | null;
  last_scanned_at?: string | null;
  next_due_at?: string | null;
  hot_reasons?: string[] | null;
  catalogue_coverage?: FixtureCatalogueCoverage | null;
};

export type FixtureArchetypeCoverage = {
  archetype: string;
  display_label: string;
  state: string;
  reason: string;
  operational_approved?: boolean;
  line?: string | null;
  matchbook_present?: boolean;
  kalshi_present?: boolean;
  polymarket_present?: boolean;
};

export type FixtureCatalogueCoverage = {
  venue_pair?: string | null;
  rows: FixtureArchetypeCoverage[];
  approved_equivalent?: number;
  paper_assumed_equivalent?: number;
  review_required?: number;
  unsupported?: number;
  venue_unavailable?: number;
  not_listed?: number;
  registry_version?: string;
};

export type InventoryComparisonStatus =
  | "matched_equivalent"
  | "paper_assumed_equivalent"
  | "venue_only"
  | "settlement_mismatch"
  | "unsupported_outcome_model"
  | "unsupported_family"
  | "missing_costs"
  | "missing_fx"
  | "stale"
  | "other";

export type VenueQuoteFact = {
  outcome: string;
  decimal_odds?: string | number | null;
  size_at_touch?: string | number | null;
};

export type VenueMarketFacts = {
  venue: Venue;
  source_event_id: string;
  source_market_id: string;
  family?: string | null;
  period?: string | null;
  line?: string | number | null;
  settlement_key?: string | null;
  settlement_complete?: boolean | null;
  best_backs: VenueQuoteFact[];
  usable_depth_at_touch?: string | number | null;
  observed_at?: string | null;
  quote_age_ms?: number | null;
  quote_age_basis?: string | null;
  native_currency?: string | null;
  fee_status?: string | null;
  fee_source?: string | null;
  fee_label?: string | null;
  fee_basis?: string | null;
  fee_rate?: string | number | null;
  fee_formula_name?: string | null;
  fee_account_assumption?: boolean;
  fx_status?: string | null;
  raw_market_name?: string | null;
  raw_market_type?: string | null;
  raw_runner_labels?: string[];
};

export type FixtureMarketInventoryRow = {
  display_name: string;
  family?: string | null;
  period?: string | null;
  line?: string | number | null;
  comparison_status: InventoryComparisonStatus;
  reason?: string | null;
  rejection_reasons: string[];
  match_reasons: string[];
  entered_solver: boolean;
  solver_model?: string | null;
  current_net_edge?: string | number | null;
  trigger_net_edge?: string | number | null;
  distance_to_trigger_pp?: string | number | null;
  solver_is_arbitrage: boolean;
  scan_lane?: string | null;
  last_scanned_at?: string | null;
  radar_freshness?: "executable" | "radar_current" | "expired" | null;
  matchbook?: VenueMarketFacts | null;
  polymarket?: VenueMarketFacts | null;
};

export type FixturePaperEntry = {
  opportunity_id: string;
  trade_id: string;
  state: string;
  solver_model?: string | null;
  fill_kinds: string[];
  guaranteed_profit_gbp_at_open?: string | number | null;
  paper_only?: boolean;
  places_orders?: boolean;
  rejection_reason?: string | null;
};

export type PreparablePaperOpportunity = {
  opportunity_id: string;
  canonical_event_id?: string | null;
  canonical_market_id?: string | null;
  solver_model?: string | null;
  eligible_for_paper_simulation: boolean;
  settlement_equivalent: boolean;
  bet_actionable?: boolean;
  bet_blocked_reason?: string | null;
  recommended_size_gbp?: string | number | null;
  maximum_validated_size_gbp?: string | number | null;
  market_label?: string | null;
  settlement_definition?: string | null;
};

export type BetTicketExecutionSeam = {
  mode: "paper";
  execution_enabled: boolean;
  places_orders: boolean;
  paper_confirm_cta: string;
  live_cta: string;
  live_cta_available: boolean;
  live_blocked_reason: string;
};

export type PreparedPaperLeg = {
  venue: Venue;
  native_currency: string;
  outcome: string;
  source_market_id: string;
  source_runner_id?: string | null;
  displayed_odds?: string | number | null;
  stake_native: string | number;
  stake_reporting: string | number;
  capital_native: string | number;
  capital_reporting: string | number;
  venue_fee?: string | number | null;
  net_payoff?: string | number | null;
  fee_basis?: string | null;
  cost_status: string;
  capital_source: string;
  execution_mode: string;
  action?: string | null;
  displayed_depth_native?: string | number | null;
  depth_consumed_pct?: string | number | null;
  fx_gbp_per_unit?: string | number | null;
  fx_source?: string | null;
  data_kind: "modelled";
};

export type PreparedPaperDeployment = {
  prepared_deployment_id?: string | null;
  opportunity_id: string;
  accepted: boolean;
  requested_size_gbp: string | number;
  operator_entered_size_gbp?: string | number | null;
  recommended_size_gbp?: string | number;
  applied_size_gbp: string | number;
  maximum_validated_size_gbp: string | number;
  resized: boolean;
  rejection_reason?: string | null;
  limiting_constraint?: string | null;
  limiting_constraint_detail?: string | null;
  legs: PreparedPaperLeg[];
  capital_required: Array<{
    venue: Venue;
    currency: string;
    amount: string | number;
    capital_source?: string;
  }>;
  native_requirements_reconciled: boolean;
  guaranteed_profit_gbp: string | number;
  guaranteed_roi: string | number;
  gross_edge?: string | number | null;
  net_edge?: string | number | null;
  market_label?: string | null;
  settlement_definition?: string | null;
  venue_pair?: string[];
  quote_age_ms?: number | null;
  quote_age_basis?: string | null;
  execution_risk_score?: number | null;
  execution_risk_band?: string | null;
  execution_risk_reasons?: string[];
  survivability?: {
    available: boolean;
    survivability_score?: number | null;
    low_survivability_warning?: boolean | null;
    volatility_regime?: string | null;
    estimate_not_guarantee?: boolean;
    note?: string;
  };
  fx_assumptions?: Array<{
    currency: string;
    gbp_per_unit: string | number;
    source: string;
    source_date?: string | null;
    valuation_date?: string | null;
    check_status?: string | null;
  }>;
  treasury_remaining?: Array<{
    venue: string;
    currency: string;
    free_balance: string | number;
    reserve_remaining: string | number;
    locked: string | number;
    allocated_native: string | number;
  }>;
  solver_model?: string | null;
  settlement_equivalent: boolean;
  paper_only: boolean;
  places_orders: boolean;
  opens_trade: boolean;
  locks_treasury: boolean;
  execution_seam?: BetTicketExecutionSeam;
  data_kind: "modelled";
  operator_note?: string;
};

export type RecommendedPaperDeployment = {
  opportunity_id: string;
  accepted: boolean;
  bet_actionable: boolean;
  bet_blocked_reason?: string | null;
  recommended_size_gbp: string | number;
  maximum_validated_size_gbp: string | number;
  limiting_constraint?: string | null;
  limiting_constraint_detail?: string | null;
  reduction_factors?: string[];
  paper_only: boolean;
  places_orders: boolean;
  opens_trade: boolean;
  locks_treasury: boolean;
  execution_seam?: BetTicketExecutionSeam;
  data_kind: "modelled";
};

export type FixtureDetailReadModel = {
  fixture: DiscoveredFixture;
  markets: FixtureMarketInventoryRow[];
  paper_entries?: FixturePaperEntry[];
  preparable_opportunities?: PreparablePaperOpportunity[];
  data_class: string;
  paper_mode: string;
  execution_enabled: boolean;
};

export type LaneRefreshStatus = {
  cadence_seconds: number;
  scan_interval_seconds?: number | null;
  reprice_after_seconds?: number | null;
  discovery_refresh_seconds?: number | null;
  cycle_timeout_seconds?: number | null;
  generation_budget_seconds?: number | null;
  generation_work_used_s?: number;
  chunk_last_duration_ms?: number | null;
  cycle_in_progress?: boolean;
  worker_state?: string | null;
  last_started_at?: string | null;
  last_completed_at?: string | null;
  last_duration_ms?: number | null;
  next_due_at?: string | null;
  fixture_count?: number;
  lifecycle_hot_count?: number;
  promoted_hot_count?: number;
  evaluated_count?: number;
  not_evaluated_count?: number;
  discovered_total?: number;
  remaining?: number;
  matched_fixtures?: number;
  equivalent_markets?: number;
  near_count?: number;
  positive_count?: number;
  qualifying_count?: number;
  hot_promotions?: number;
  last_successful_fixture?: string | null;
  current_fixture?: string | null;
  sweep_id?: string | null;
  last_error?: string | null;
  last_diagnostics?: Record<string, unknown> | null;
  last_persist_error?: string | null;
  persist_ok?: boolean | null;
  last_heartbeat_at?: string | null;
  last_plan_reason?: string | null;
  degraded?: boolean;
  resume_cursor?: string | null;
  operator_summary?: string | null;
  active_venues?: Venue[];
  pending_venues?: Venue[];
  comparison_ready?: boolean;
  venue_warning?: string | null;
  applies_next_cycle?: boolean;
  venue_health?: Record<string, string>;
  operation_health?: Record<string, unknown>;
  raw_events_discovered_by_venue?: Record<string, number>;
  canonical_work_total?: number;
  canonical_evaluated?: number;
  canonical_retryable?: number;
  canonical_final_failed?: number;
  canonical_remaining?: number;
  series_work_total?: number;
  series_ok?: number;
  series_retryable?: number;
  series_final_failed?: number;
  series_skipped?: number;
};

export type PriceEngineTierStatus = {
  working_set?: number;
  pricing_fixtures?: number;
  due?: number;
  queued?: number;
  in_flight?: number;
  evaluated?: number;
  evaluated_definition?: string;
  retry_wait?: number;
  deferred?: number;
  provider_capacity_saturated?: number;
  not_started_this_cadence?: number;
  revalidation_needed?: number;
  persist_failures?: number;
  last_error?: string | null;
  operation_health?: Record<string, unknown>;
  venue_health?: Record<string, string>;
};

export type PriceEnginePublicStatus = {
  hot?: PriceEngineTierStatus;
  background?: PriceEngineTierStatus;
  durable_queue?: boolean;
  observability_lag?: number;
  observability_dropped?: number;
  observability_error?: string | null;
};

export type ProviderSlotLoad = {
  inflight?: number;
  limit?: number;
  waiting?: number;
  wait_ms?: number;
  latency_ms?: number;
  deadline_misses?: number;
  saturated?: boolean;
};

export type HotLoad = {
  fixtures?: number;
  pricing_fixtures?: number;
  working_set?: number;
  due?: number;
  in_flight?: number;
  retry_wait?: number;
  deferred?: number;
  last_cycle_ms?: number | null;
  cadence_seconds?: number;
  cadence_utilisation?: number | null;
  health?: string;
};

export type BackgroundLoad = {
  working_set?: number;
  pricing_fixtures?: number;
  due?: number;
  cadence_seconds?: number;
  health?: string;
};

export type UniverseLoad = {
  evaluated?: number;
  total?: number;
  remaining?: number;
  cadence_seconds?: number;
  generation_work_used_s?: number | null;
  generation_budget_seconds?: number | null;
  selected_competition_count?: number;
  scope_version?: number | null;
  generation_scope_version?: number | null;
  worker_state?: string;
  health?: string;
};

export type ActiveTradeLoad = {
  open_trades?: number;
  due?: number;
  overdue?: number;
  last_cycle_ms?: number | null;
  cadence_seconds?: number;
  capital_locked_gbp?: string | number | null;
  health?: string;
};

export type SystemLoadSummary = {
  active_trade?: ActiveTradeLoad;
  hot?: HotLoad;
  background?: BackgroundLoad;
  matchbook?: ProviderSlotLoad;
  kalshi?: ProviderSlotLoad;
  universe?: UniverseLoad;
  catalogue_items?: number;
};

export type LaneVenueFlags = {
  matchbook: boolean;
  polymarket: boolean;
  kalshi: boolean;
};

export type LaneVenueParticipation = {
  hot: Venue[];
  universe: Venue[];
  source?: "operator" | "env_default";
  updated_at?: string | null;
  hot_warning?: string | null;
  universe_warning?: string | null;
  config_diagnostic?: string | null;
};

export type LaneVenueParticipationUpdate = {
  hot: LaneVenueFlags;
  universe: LaneVenueFlags;
};

export type VenueDegradationIncidentRef = {
  available: boolean;
  captured_at: string;
  incident_id: string;
};

export type VenueDegradationIncident = {
  schema?: string;
  data_kind?: string;
  incident_id?: string;
  captured_at: string;
  build?: {
    git_sha?: string | null;
    git_branch?: string | null;
    source?: string | null;
  };
  affected_venue: string;
  transition?: {
    previous_health?: string | null;
    new_health?: string | null;
    reason?: string;
  };
  venue_health?: Record<string, string>;
  hot?: Record<string, unknown>;
  background?: Record<string, unknown>;
  universe?: Record<string, unknown>;
  price_engine?: Record<string, unknown>;
  provider_access?: Record<string, unknown>;
  recent_scan_cycles?: Array<Record<string, unknown>>;
  active_catalogue_count?: number | null;
  classification?: Record<string, unknown>;
  [key: string]: unknown;
};

export type OperatorScannerSettings = {
  min_net_edge: string;
  outright_min_net_edge?: string | null;
  max_execution_risk: number;
  hot_scan_interval_seconds?: number;
  hot_reprice_after_seconds?: number;
  background_scan_interval_seconds?: number;
  background_reprice_after_seconds?: number;
  universe_discovery_refresh_seconds?: number;
  hot_cadence_seconds?: number;
  background_cadence_seconds?: number;
  universe_cadence_seconds?: number;
  max_allocated_per_trade_gbp?: string;
  scanner_stopped: boolean;
  universe_scans_paused?: boolean;
  source?: "operator" | "env_default";
  updated_at?: string | null;
  restart_semantics?: string;
};

export type OperatorScannerSettingsUpdate = {
  min_net_edge: string;
  outright_min_net_edge?: string | null;
  max_execution_risk: number;
  hot_scan_interval_seconds?: number;
  hot_reprice_after_seconds?: number;
  background_scan_interval_seconds?: number;
  background_reprice_after_seconds?: number;
  universe_discovery_refresh_seconds?: number;
  hot_cadence_seconds?: number;
  background_cadence_seconds?: number;
  universe_cadence_seconds?: number;
  max_allocated_per_trade_gbp?: string;
};

export type OperatorCompetitionOption = {
  code: string;
  display_name: string;
  selector_label: string;
  group_id: string;
  group_label: string;
  default_selected: boolean;
  selectable: boolean;
  verification_status?: string;
  unavailable_reason?: string | null;
  market_scope?: "FIXTURE_MATCH" | "COMPETITION_SEASON";
  observation_only?: boolean;
  paper_executable?: boolean;
};

export type OperatorUniverseScope = {
  sport: string;
  selected_competition_codes: string[];
  selected_season_scope_codes?: string[];
  selected_count: number;
  selected_season_scope_count?: number;
  saved_default_competition_codes?: string[];
  saved_default_season_scope_codes?: string[];
  saved_default_count?: number;
  saved_default_season_scope_count?: number;
  is_session_override?: boolean;
  scope_version: number;
  registry_version: number;
  source: "operator" | "env_default";
  updated_at?: string | null;
  needs_first_run_confirmation: boolean;
  new_competitions_available: boolean;
  catalog: OperatorCompetitionOption[];
  generation_scope_version?: number | null;
  generation_selected_competition_codes?: string[];
  generation_selected_season_scope_codes?: string[];
  manual_universe_state?: "idle" | "running" | "pending";
  manual_background_busy?: boolean;
};

export type OperatorUniverseScopeUpdate = {
  selected_competition_codes: string[];
  selected_season_scope_codes?: string[];
  sport?: string;
  run_universe_now?: boolean;
  save_as_default?: boolean;
  restore_saved_default?: boolean;
};

export type LiveRefreshStatus = {
  discovery_source: Venue;
  discovery_mode?: string;
  matching_venue?: Venue | null;
  matching_venues?: Venue[];
  server_loop_enabled: boolean;
  paper_autofill_enabled?: boolean;
  scanner_stopped?: boolean;
  universe_scans_paused?: boolean;
  operator_settings?: OperatorScannerSettings | null;
  universe_scope?: OperatorUniverseScope | null;
  interval_seconds: number;
  cycle_in_progress: boolean;
  last_started_at?: string | null;
  last_completed_at?: string | null;
  last_duration_ms?: number | null;
  last_error?: string | null;
  last_matched_event_pairs?: number | null;
  last_matched_market_pairs?: number | null;
  last_paper_decisions?: number | null;
  last_issue_count?: number | null;
  skipped_out_of_scope?: number | null;
  operator_summary?: string | null;
  config_warnings?: string[];
  venue_health?: Record<string, string>;
  live_scores: string;
  discovered_fixtures: DiscoveredFixture[];
  hot?: LaneRefreshStatus;
  universe?: LaneRefreshStatus;
  background?: LaneRefreshStatus;
  active_trade?: LaneRefreshStatus;
  price_engine?: PriceEnginePublicStatus;
  venue_participation?: LaneVenueParticipation | null;
  recent_scan_cycles?: PaperScanCycleRecord[];
  provider_access?: Record<string, unknown>;
  venue_degradation_incidents?: Record<string, VenueDegradationIncidentRef>;
  active_trade_timeline?: ActiveTradeTimelineItem[];
  system_load?: SystemLoadSummary;
};

export type VenueHealth = {
  venue: Venue;
  ok: boolean;
  authenticated: boolean;
  checked_at: string;
  detail?: string | null;
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
  capital_limit_gbp?: string;
  minimum_net_edge?: string;
  maximum_execution_risk?: number;
  max_event_pairs?: number; // backend collect default 60, max 100
  max_market_pairs_per_event?: number;
};

export type EconomicsFxRow = {
  currency: string;
  gbp_per_unit: string;
  source_date: string;
  valuation_date: string;
  retrieved_at?: string | null;
  status: string;
  check_status?: string;
  carried_forward?: boolean;
  primary_source: string;
  variance_bps?: string | null;
  check_source?: string | null;
  check_gbp_per_unit?: string | null;
};

export type EconomicsVenueCostRow = {
  venue: Venue;
  market_class?: string | null;
  action: string;
  fee_basis: string;
  known_status: string;
  rate?: string | null;
  source: string;
  effective_from?: string | null;
  snapshot_id?: string | null;
  detail?: string | null;
};

export type MatchbookFeeStatus = {
  provider_default_rate: string;
  override_rate?: string | null;
  effective_rate: string;
  fee_basis: string;
  account_assumption: boolean;
  label: string;
  source: string;
  detail: string;
  updated_at?: string | null;
};

export type PolymarketFeePolicy = {
  resolution: string;
  catalog_seeded: boolean;
  detail: string;
};

export type EconomicsStatus = {
  as_of: string;
  data_kind: string;
  fx: EconomicsFxRow[];
  venue_costs: EconomicsVenueCostRow[];
  matchbook_fee?: MatchbookFeeStatus | null;
  polymarket_fee_policy?: PolymarketFeePolicy | null;
  issues: string[];
  fx_schedule?: {
    enabled: boolean;
    last_ingest_source_date?: string | null;
    last_daily_ingest_london_date?: string | null;
    last_bootstrap_source_date?: string | null;
    last_error?: string | null;
    scanner_usd?: Record<string, unknown>;
  };
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

export const API_BASE = process.env.NEXT_PUBLIC_SPORTS_HEDGE_API_URL ?? "http://localhost:8000";
/** Browser abort for the bounded manual diagnostic. Do not raise this to wait out UNIVERSE discovery. */
export const PAPER_COLLECTION_TIMEOUT_MS = 60_000;
/** Slightly above the shared 25s HOT collector + 5s coordinator envelope. */
export const PAPER_HOT_REFRESH_TIMEOUT_MS = 35_000;
export const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
    if (Array.isArray(body.detail)) {
      const parts = body.detail
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item === "object" && "msg" in item) {
            const msg = (item as { msg?: unknown }).msg;
            return typeof msg === "string" ? msg : "";
          }
          return "";
        })
        .filter((item) => item.trim());
      if (parts.length) return parts.join("; ");
    }
    if (body.detail && typeof body.detail === "object") {
      const record = body.detail as { message?: unknown; code?: unknown };
      if (typeof record.message === "string" && record.message.trim()) return record.message;
      if (typeof record.code === "string" && record.code.trim()) return record.code;
    }
  } catch {
    // Use the stable status fallback below when an upstream returned no JSON body.
  }
  return `Sports Hedge API request failed (${response.status})`;
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  timeoutMessage?: string,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    const name = error instanceof Error ? error.name : "";
    if (name === "AbortError" || name === "TimeoutError") {
      throw new Error(
        timeoutMessage ??
          `Sports Hedge API request timed out after ${Math.round(timeoutMs / 1000)}s`,
      );
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(path: string, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, { cache: "no-store" }, timeoutMs);
  if (!response.ok) {
    throw new ApiRequestError(await errorDetail(response), response.status);
  }
  return response.json() as Promise<T>;
}

export class ApiRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

export function isNotFoundApiError(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 404;
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

export function getPaperScanCycles(query = "limit=100"): Promise<PaperScanCycleRecord[]> {
  return request(`/paper/scan-cycles${query ? `?${query}` : ""}`);
}

export type ScanCycleDiagnosticStage = {
  venue?: string;
  stage?: string;
  count?: number;
  total_ms?: number;
  avg_ms?: number;
  p50_ms?: number | null;
  p95_ms?: number | null;
  max_ms?: number | null;
  success?: number;
  timeout?: number;
  rate_limit?: number;
  capacity_deferred?: number;
  not_started?: number;
  error?: number;
};

export type ScanCycleDiagnosticReport = {
  data_kind?: string;
  lane?: string;
  note?: string;
  wall_ms?: number;
  due?: number;
  considered?: number;
  terminals?: Record<string, number>;
  leftover_collapsed?: number;
  decisions?: number;
  qualifying?: number;
  promoted_hot?: number;
  evaluations_per_second?: number;
  provider_io_ms_sum?: number;
  slot_wait_ms_sum?: number;
  local_evaluate_ms_sum?: number;
  timing_note?: string;
  saved_provider_calls?: number;
  coalesced_provider_calls?: number;
  repeated_exact_id_calls?: number;
  distinct_exact_ids?: number;
  call_shape?: {
    sequential_within_item?: boolean;
    worker_limit?: number;
    explicit_slice_wall?: boolean;
    slice_wall_seconds?: number | null;
    provider_limits?: Record<string, number>;
    provider_calls?: number;
  };
  stages?: ScanCycleDiagnosticStage[];
  slowest?: Array<Record<string, unknown>>;
  samples?: Record<string, Array<{ row_id?: string; reason?: string }>>;
  worker_errors?: Array<{ type?: string; message?: string }>;
};

export type ScanCycleDiagnosticResponse = {
  available: boolean;
  paper_only?: boolean;
  places_orders?: boolean;
  data_kind?: string;
  cycle_id: string;
  report?: ScanCycleDiagnosticReport | null;
  note?: string;
};

export function getPaperScanCycleReport(cycleId: string): Promise<ScanCycleDiagnosticResponse> {
  const query = new URLSearchParams({ cycle_id: cycleId });
  return request(`/paper/scan-cycle-report?${query.toString()}`);
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
  | "promoted_to_hot"
  | "paper_eligible"
  | "paper_fill_attempted"
  | "paper_fill_partial"
  | "paper_fill_complete"
  | "paper_fill_rejected"
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
  source_runner_id?: string | null;
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
  line?: string | number | null;
  venues: Venue[];
  legs: WatchLeg[];
  status: WatchlistOpportunityStatus;
  classification: WatchlistClassification;
  is_arbitrage?: boolean;
  trigger_net_edge?: string | number | null;
  min_net_edge_scope?: string | null;
  min_net_edge_source?: string | null;
  min_net_edge_configured?: boolean | null;
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
  bet_actionable?: boolean;
  bet_blocked_reason?: string | null;
  scan_lane?: string | null;
  last_scanned_at?: string | null;
  next_due_at?: string | null;
  freshness_class?: "executable" | "radar_current" | "expired" | string | null;
  mapping_confidence?: number | null;
  mapping_matched?: boolean | null;
  mapping_reasons?: string[];
  mapping_provenance?: {
    mapping_source?: "native_deterministic" | "operator_verified" | null;
    rule_id?: string | null;
    rule_version?: number | null;
    rule_type?: string | null;
    applied_rule_ids?: string[];
  } | null;
  mapping_review_candidate?: import("./mapping-verification").MappingReviewCandidate | null;
};

export type OpportunityLifecycleEvent = {
  event_id: string;
  opportunity_id: string;
  occurred_at: string;
  event_type: WatchlistLifecycleEventType;
  status: WatchlistOpportunityStatus;
  current_net_edge?: string | number | null;
  distance_to_trigger_pp?: string | number | null;
  trigger_net_edge?: string | number | null;
  min_net_edge_scope?: string | null;
  min_net_edge_source?: string | null;
  detail?: string | null;
  fixture_label?: string | null;
  market_family?: string | null;
  canonical_event_id?: string | null;
  canonical_market_id?: string | null;
  capture_eligible?: boolean | null;
  attempt_id?: string | null;
  append_seq?: number | null;
};

export async function runPaperCollection(
  payload: PaperCollectionRequest,
): Promise<PaperCollectionReport> {
  const response = await fetchWithTimeout(
    `${API_BASE}/paper/collect`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    },
    PAPER_COLLECTION_TIMEOUT_MS,
    `Manual diagnostic timed out after ${Math.round(PAPER_COLLECTION_TIMEOUT_MS / 1000)}s. HOT pricing, BACKGROUND pricing and UNIVERSE discovery are separate server-owned lanes; check those timings before retrying the diagnostic.`,
  );
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperCollectionReport>;
}

export async function runPaperHotRefresh(
  payload: PaperCollectionRequest,
): Promise<PaperCollectionReport> {
  const response = await fetchWithTimeout(
    `${API_BASE}/paper/collect/hot`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    },
    PAPER_HOT_REFRESH_TIMEOUT_MS,
    `HOT pricing timed out after ${Math.round(PAPER_HOT_REFRESH_TIMEOUT_MS / 1000)}s. Check venue health and retry.`,
  );
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperCollectionReport>;
}

export async function runPaperBackgroundRefresh(): Promise<LiveRefreshStatus> {
  const response = await fetchWithTimeout(
    `${API_BASE}/paper/collect/background`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
      cache: "no-store",
    },
    PAPER_HOT_REFRESH_TIMEOUT_MS,
    `BACKGROUND pricing timed out after ${Math.round(PAPER_HOT_REFRESH_TIMEOUT_MS / 1000)}s. Check venue health and retry.`,
  );
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export type UniverseRunMode = "update" | "clear_update";

export const UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY =
  "Clears the live UNIVERSE working set only. History, catalogue, PAPER trades and Treasury are preserved.";

export async function runPaperUniverseNow(): Promise<LiveRefreshStatus> {
  return runPaperUniverse({ mode: "update" });
}

/** Backward-compatible Update existing path. Prefer runPaperUniverse. */
export async function runPaperUniverseCollectNow(): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/collect/universe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function runPaperUniverse(payload: {
  mode: UniverseRunMode;
}): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/universe/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function clearPaperUniverse(): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/universe/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export function getUniverseScope(): Promise<OperatorUniverseScope> {
  return request("/paper/universe-scope");
}

export async function saveUniverseScope(
  update: OperatorUniverseScopeUpdate,
): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/universe-scope`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function simulatePaperFill(payload: {
  opportunity_id: string;
  operator_note?: string;
  provenance?: "live_paper" | "fixture_demo";
  prepared_deployment_id?: string | null;
  requested_size_gbp?: string;
  simulate_external?: boolean;
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

export async function preparePaperDeployment(payload: {
  opportunity_id: string;
  requested_size_gbp: string;
  operator_note?: string;
}): Promise<PreparedPaperDeployment> {
  const response = await fetch(`${API_BASE}/paper/prepare-deployment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      operator_note: "PAPER-ONLY fixed-size preparation; does not OPEN or lock",
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PreparedPaperDeployment>;
}

export async function recommendPaperDeployment(payload: {
  opportunity_id: string;
  operator_note?: string;
}): Promise<RecommendedPaperDeployment> {
  const response = await fetch(`${API_BASE}/paper/recommend-deployment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      operator_note:
        "PAPER-ONLY recommended size from existing allocator constraints; does not OPEN or lock",
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<RecommendedPaperDeployment>;
}

export function getLiveRefreshStatus(): Promise<LiveRefreshStatus> {
  return request("/paper/live-refresh");
}

export function getVenueDegradationIncident(venue: string): Promise<VenueDegradationIncident> {
  return request(`/paper/venue-degradation-incident/${encodeURIComponent(venue)}`);
}

export async function saveVenueParticipation(
  update: LaneVenueParticipationUpdate,
): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/venue-participation`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function saveOperatorScannerSettings(
  update: OperatorScannerSettingsUpdate,
): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/operator-scanner-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function stopPaperScanner(): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/scanner/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function resumePaperScanner(): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/scanner/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function pauseUniverseSchedule(): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/scanner/universe-schedule/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export async function resumeUniverseSchedule(): Promise<LiveRefreshStatus> {
  const response = await fetch(`${API_BASE}/paper/scanner/universe-schedule/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<LiveRefreshStatus>;
}

export function getVenueHealth(): Promise<VenueHealth[]> {
  return request("/venues/health");
}

export function getFixtureDetail(canonicalEventId: string): Promise<FixtureDetailReadModel> {
  return request(`/operations/fixtures/${encodeURIComponent(canonicalEventId)}`);
}

export function getEconomicsStatus(): Promise<EconomicsStatus> {
  return request("/paper/economics-status");
}

export async function saveMatchbookFee(commissionRate: string): Promise<MatchbookFeeStatus> {
  const response = await fetch(`${API_BASE}/paper/matchbook-fee`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ commission_rate: commissionRate }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<MatchbookFeeStatus>;
}

export async function resetMatchbookFee(): Promise<MatchbookFeeStatus> {
  const response = await fetch(`${API_BASE}/paper/matchbook-fee/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<MatchbookFeeStatus>;
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

export type MappingPromptBundle = {
  prompt_text: string;
  candidate: NonNullable<NearOpportunity["mapping_review_candidate"]>;
  excluded_secret_fields?: string[];
};

export type MappingReviewProposal = {
  review_id: string;
  verdict: "verified" | "not_verified" | "ambiguous";
  operator_confirmed: boolean;
  proposed_rule: import("./mapping-verification").MappingProposedRule | null;
  prompt_text: string;
  chatgpt_text?: string | null;
  activation_blocked_reason?: string | null;
};

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetchWithTimeout(
    `${API_BASE}${path}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    },
    DEFAULT_REQUEST_TIMEOUT_MS,
  );
  if (!response.ok) {
    throw new ApiRequestError(await errorDetail(response), response.status);
  }
  return response.json() as Promise<T>;
}

export function buildMappingReviewPrompt(
  candidate: NonNullable<NearOpportunity["mapping_review_candidate"]>,
): Promise<MappingPromptBundle> {
  return postJson<MappingPromptBundle>("/paper/mapping-reviews/prompt", { candidate });
}

export function interpretMappingReview(payload: {
  candidate: NonNullable<NearOpportunity["mapping_review_candidate"]>;
  operator?: string;
  chatgpt_text?: string | null;
  manual_verdict?: MappingReviewProposal["verdict"] | null;
}): Promise<MappingReviewProposal> {
  return postJson<MappingReviewProposal>("/paper/mapping-reviews/interpret", {
    operator: "operator",
    ...payload,
  });
}

export function confirmMappingReview(payload: {
  candidate: NonNullable<NearOpportunity["mapping_review_candidate"]>;
  operator?: string;
  chatgpt_text?: string | null;
  manual_verdict?: MappingReviewProposal["verdict"] | null;
  operator_confirmed: boolean;
  review_id?: string | null;
}): Promise<MappingReviewProposal> {
  return postJson<MappingReviewProposal>("/paper/mapping-reviews/confirm", {
    operator: "operator",
    ...payload,
  });
}

export type PaperLiquidityPool = {
  venue: Venue;
  native_currency: "GBP" | "USD";
  available: string | number;
  locked: string | number;
  transit: string | number;
  included_in_solver: boolean;
  connection_status: "connected" | "not_connected";
  capital_kind: "paper_hypothetical";
  gbp_carrying_value?: string | number | null;
  gbp_carrying_status: "identity" | "fx_converted" | "fx_unavailable";
  gbp_fx_source?: string | null;
  updated_at?: string | null;
};

export type PaperLiquiditySnapshot = {
  capital_kind: "paper_hypothetical";
  data_kind: "paper_config";
  pools: PaperLiquidityPool[];
  updated_at: string;
};

export function getPaperLiquidityPools(): Promise<PaperLiquiditySnapshot> {
  return request("/paper/liquidity-pools");
}

export type PaperTreasuryPool = {
  pool_id: string;
  session_id: string;
  venue: Venue;
  native_currency: string;
  identity: string;
  seed_native: string | number;
  available_cash: string | number;
  locked_capital: string | number;
  realised_pnl_native: string | number;
  cumulative_fees_native: string | number;
  gbp_carrying_value?: string | number | null;
  gbp_carrying_status: string;
  fx_rate_gbp_per_unit?: string | number | null;
  fx_source?: string | null;
  fx_as_of?: string | null;
};

export type PaperTreasuryEvent = {
  event_id: string;
  session_id: string;
  venue: Venue;
  native_currency: string;
  event_type: string;
  native_amount: string | number;
  occurred_at: string;
  trade_id?: string | null;
  opportunity_id?: string | null;
  lock_id?: string | null;
  source: string;
  source_id: string;
  reason: string;
  fx_source?: string | null;
};

export type PaperTreasurySnapshot = {
  data_kind: string;
  capital_kind: string;
  execution_enabled: boolean;
  mode: string;
  session?: {
    session_id: string;
    opened_at: string;
    seed_gbp: string | number;
    fx_rate_usd_gbp: string | number;
    fx_source: string;
    fx_as_of: string;
    include_kalshi: boolean;
    reason: string;
  } | null;
  pools: PaperTreasuryPool[];
  events: PaperTreasuryEvent[];
  note: string;
};

export function getPaperTreasury(): Promise<PaperTreasurySnapshot> {
  return request("/paper/treasury");
}

export async function resetPaperTreasury(reason = "explicit paper treasury demo reset"): Promise<PaperTreasurySnapshot> {
  const response = await fetch(`${API_BASE}/paper/treasury/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperTreasurySnapshot>;
}

export async function resetPaperSession(
  reason = "explicit operator paper session reset",
): Promise<DemoWalkthroughSnapshot> {
  return resetDemoWalkthrough({
    reinitialize_store: true,
    reason,
  });
}

export async function savePaperTreasuryPools(
  pools: Array<{ venue: Venue; available: string }>,
  reason = "operator paper treasury edit",
): Promise<PaperTreasurySnapshot> {
  const response = await fetch(`${API_BASE}/paper/treasury/pools`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pools, reason }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperTreasurySnapshot>;
}

export async function savePaperLiquidityPools(
  pools: Array<{ venue: Venue; available: string; locked?: string; transit?: string }>,
): Promise<PaperLiquiditySnapshot> {
  const response = await fetch(`${API_BASE}/paper/liquidity-pools`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pools }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperLiquiditySnapshot>;
}

export async function resetPaperLiquidityPools(): Promise<PaperLiquiditySnapshot> {
  const response = await fetch(`${API_BASE}/paper/liquidity-pools/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperLiquiditySnapshot>;
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

export type PaperTradeState =
  | "PENDING"
  | "PARTIAL"
  | "OPEN"
  | "AWAITING_MANUAL_EXTERNAL"
  | "CLOSED";

export type PaperLegFillKind =
  | "INTERNAL_SIMULATED"
  | "PAPER_SIMULATED_EXTERNAL"
  | "MANUAL_EXTERNAL"
  | "UNFILLED";

export type PaperTradeLeg = {
  venue: Venue;
  outcome: string;
  currency: string;
  requested_stake: string | number;
  filled_stake: string | number;
  displayed_odds?: string | number | null;
  filled_odds?: string | number | null;
  source_market_id: string;
  fill_id?: string | null;
  fill_kind: PaperLegFillKind;
  capital_source: string;
  execution_mode: string;
};

export type PaperCloseFill = {
  fill_id: string;
  opening_fill_id: string;
  venue: Venue;
  outcome: string;
  native_currency: string;
  close_action: string;
  filled_close_quantity: string | number;
  weighted_closing_price?: string | number | null;
  proceeds_native: string | number;
  closing_fee_native: string | number;
  native_close_pnl: string | number;
  gbp_close_pnl: string | number;
  fx_rate_gbp_per_unit: string | number;
  lock_id: string;
  fee_snapshot_id?: string | null;
  quote_age_ms?: number | null;
  paper_only?: boolean;
};

export type PaperTradeAuditEvent = {
  event_id: string;
  occurred_at: string;
  event_type: string;
  detail?: string | null;
};

export type PaperExecutionRiskSnapshot = {
  kind: "entry" | "unwind" | "close" | string;
  recorded_at: string;
  opportunity_id?: string | null;
  trade_id?: string | null;
  score?: number | null;
  band?: string | null;
  reasons?: string[];
  maximum_execution_risk?: number | null;
  quote_age_ms?: number | null;
  quote_age_basis?: string | null;
  size_to_depth_ratio?: number | null;
  hedge_liquidity_ratio?: number | null;
  spread_bps?: number | null;
  recent_volatility_bps?: number | null;
  assumed_latency_ms?: number | null;
  fill_confidence_score?: number | null;
  fill_confidence_reasons?: string[];
  net_edge?: string | number | null;
  trigger_net_edge?: string | number | null;
  solver_model?: string | null;
  eligible_for_paper_simulation?: boolean | null;
};

export type UnwindRecommendation = "HOLD" | "UNWIND_ELIGIBLE" | "UNWIND_NOT_SAFE";

export type PositionManagementSnapshot = {
  trade_id: string;
  recommendation: UnwindRecommendation;
  decision_reason: string;
  evaluated_at: string;
  hold_pnl_gbp?: string | number | null;
  validated_exit_pnl_gbp?: string | number | null;
  unwind_cost_gbp?: string | number | null;
  closing_fees?: Array<{
    venue: string;
    native_currency: string;
    closing_fee_native: string | number;
    fee_snapshot_id?: string | null;
    deferred_profit_commission?: boolean;
  }>;
  releasable_native?: Record<string, string | number>;
  capital_pressure?: string;
  opportunity_cost_gbp?: string | number | null;
  opportunity_cost_detail?: string | null;
  close_executable?: boolean;
  close_execution_risk_score?: number | null;
  quote_age_ms?: number | null;
  quote_age_basis?: string | null;
  auto_action?: string;
  pending_confirmation?: {
    captured_at: string;
    quotes?: Array<{
      venue: string;
      source_market_id: string;
      source_runner_id: string;
      canonical_outcome: string;
      quoted_at: string;
    }>;
    validated_exit_pnl_gbp?: string | number | null;
  } | null;
  auto_unwind_enabled?: boolean;
  auto_close_allowed?: boolean;
  remaining_lock_minutes?: string | number | null;
  remaining_lock_basis?: string | null;
  remaining_lock_source_class?: string | null;
  remaining_lock_confidence?: string | number | null;
  remaining_lock_detail?: string | null;
  expected_settlement_at?: string | null;
  remaining_lock_advisory?: boolean;
  normal_release_context?: string;
  exit_margin_gbp?: string | number | null;
  exit_threshold_gbp?: string | number | null;
  exit_margin_basis?: string | null;
  exit_margin_actionable?: boolean;
  close_blocker?: string | null;
  paper_only?: boolean;
  places_orders?: boolean;
  spendable?: boolean;
  data_kind?: string;
};

export type PaperTrade = {
  trade_id: string;
  opportunity_id: string;
  canonical_event_id?: string | null;
  canonical_market_id?: string | null;
  solver_model?: string | null;
  market_family?: MarketFamily | null;
  period?: FootballPeriod | null;
  competition?: string | null;
  home_team?: string | null;
  away_team?: string | null;
  fixture_label?: string | null;
  market_label?: string | null;
  line?: string | number | null;
  state: PaperTradeState;
  opened_at: string;
  last_updated_at: string;
  settled_at?: string | null;
  guaranteed_profit_gbp_at_open?: string | number | null;
  realised_pnl_gbp?: string | number | null;
  capital_locked_native: Record<string, string | number>;
  capital_locked_gbp?: string | number | null;
  entry_net_edge?: string | number | null;
  current_exit_pct?: string | number | null;
  current_exit_delta_pp?: string | number | null;
  current_exit_checked_at?: string | null;
  current_exit_block_reason?: string | null;
  settlement_outcome?: string | null;
  settlement_source?: string | null;
  settlement_source_id?: string | null;
  settlement_detail?: string | null;
  last_settlement_check_at?: string | null;
  settlement_reconciliation_status?: "unchecked" | "ready" | "blocked" | "settled" | string | null;
  settlement_blocker?: string | null;
  settlement_blocker_detail?: string | null;
  provenance: "live_paper" | "fixture_demo" | "unavailable" | string;
  paper_only?: boolean;
  places_orders?: boolean;
  legs: PaperTradeLeg[];
  close_fills?: PaperCloseFill[];
  position_management?: PositionManagementSnapshot | null;
  entry_risk?: PaperExecutionRiskSnapshot | null;
  close_risks?: PaperExecutionRiskSnapshot[];
  audit: PaperTradeAuditEvent[];
};

export type PaperJournalPosting = {
  account_code: string;
  side: string;
  amount_native: string | number;
  amount_gbp: string | number;
  dimensions: { currency: string; capital_source?: string; venue?: string | null };
};

export type PaperJournalEntry = {
  journal_id: string;
  source: string;
  source_id: string;
  occurred_at: string;
  description: string;
  opportunity_id: string;
  provenance: string;
  postings: PaperJournalPosting[];
};

export type PaperTradeDetail = PaperTrade & {
  journals: PaperJournalEntry[];
};

export type PaperTradeBookSummary = {
  data_kind: string;
  paper_only: boolean;
  open_count: number;
  closed_count: number;
  awaiting_manual_external_count: number;
  capital_locked_native: Record<string, string | number>;
  capital_locked_gbp?: string | number | null;
  realised_pnl_gbp?: string | number | null;
  gbp_unavailable_reason?: string | null;
};

export function getPaperTradeSummary(): Promise<PaperTradeBookSummary> {
  return request("/paper/trades/summary");
}

export function getActivePaperTrades(): Promise<PaperTrade[]> {
  return request("/paper/trades/active");
}

export function getClosedPaperTrades(): Promise<PaperTrade[]> {
  return request("/paper/trades/closed");
}

export function getPaperTrade(tradeId: string): Promise<PaperTradeDetail> {
  return request(`/paper/trades/${encodeURIComponent(tradeId)}`);
}

export function getActiveTradeEvents(query = ""): Promise<ActiveTradeJournalEvent[]> {
  const suffix = query ? `?${query}` : "";
  return request(`/paper/active-trade-events${suffix}`);
}

export function getPaperPositionManagement(): Promise<PositionManagementSnapshot[]> {
  return request("/paper/trades/position-management");
}

export function getTradePositionManagement(
  tradeId: string,
): Promise<PositionManagementSnapshot> {
  return request(`/paper/trades/${encodeURIComponent(tradeId)}/position-management`);
}

export async function settlePaperTrade(
  tradeId: string,
  payload: {
    winning_outcome: string;
    source: string;
    source_id: string;
    detail?: string;
    provenance?: "live_paper" | "fixture_demo";
  },
): Promise<PaperTradeDetail> {
  const response = await fetch(`${API_BASE}/paper/trades/${encodeURIComponent(tradeId)}/settle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provenance: "fixture_demo",
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperTradeDetail>;
}

export type CanonicalSettlementChoice = {
  value: string;
  label: string;
  realised_pnl_gbp?: string | number | null;
};

export type PaperSettlementOptions = {
  trade_id: string;
  fixture_label?: string | null;
  market_label?: string | null;
  market_family?: string | null;
  line?: string | number | null;
  home_team?: string | null;
  away_team?: string | null;
  paper_only?: boolean;
  places_orders?: boolean;
  provenance: string;
  state: PaperTradeState;
  legs: Array<{
    venue: string;
    outcome: string;
    currency: string;
    filled_stake: string | number;
    filled_odds?: string | number | null;
    displayed_odds?: string | number | null;
    fill_kind: string;
    opening_action?: string | null;
    canonical_state?: string | null;
  }>;
  choices: CanonicalSettlementChoice[];
  unsupported_reason?: string | null;
  reconciliation: {
    status: string;
    last_checked_at?: string | null;
    blocker?: string | null;
    detail?: string | null;
  };
};

export function getPaperSettlementOptions(tradeId: string): Promise<PaperSettlementOptions> {
  return request(`/paper/trades/${encodeURIComponent(tradeId)}/settlement-options`);
}

export async function manualSettlePaperTrade(
  tradeId: string,
  payload: { winning_outcome: string; operator_note?: string },
): Promise<PaperTradeDetail> {
  const response = await fetch(
    `${API_BASE}/paper/trades/${encodeURIComponent(tradeId)}/manual-settle`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    },
  );
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<PaperTradeDetail>;
}

export type EstimatedTimeToRelease = {
  remaining_lock_minutes?: string | number | null;
  expected_settlement_at?: string | null;
  basis: string;
  source_class: string;
  confidence?: string | number | null;
  detail?: string | null;
  advisory: boolean;
  settles_or_releases_capital: boolean;
};

export type UnwindDecision = {
  trade_id: string;
  recommendation: UnwindRecommendation;
  decision_reason: string;
  hold_pnl_gbp?: string | number | null;
  validated_exit_pnl_gbp?: string | number | null;
  unwind_cost_gbp?: string | number | null;
  opportunity_cost_gbp?: string | number | null;
  remaining_lock_minutes?: string | number | null;
  estimated_time_to_release: EstimatedTimeToRelease;
  duration_decision_role: string;
  capital_turnover_hint?: string | null;
  spendable_release_requires: string;
  conditionally_releasable_by_venue_currency: Record<string, string | number>;
  close_plan: {
    fully_executable: boolean;
    rejection_reasons: string[];
  };
  paper_only: boolean;
  places_orders: boolean;
  spendable: boolean;
  data_kind: string;
};

export type DemoPoolCheck = {
  venue: Venue;
  native_currency: string;
  seed_native: string | number;
  available_cash: string | number;
  locked_capital: string | number;
  gbp_carrying_value?: string | number | null;
  gbp_carrying_status: string;
  fx_source?: string | null;
};

export type DemoWalkthroughSnapshot = {
  data_kind: string;
  label: string;
  paper_only: boolean;
  execution_enabled: boolean;
  mode: string;
  treasury: PaperTreasurySnapshot;
  pools: DemoPoolCheck[];
  book: PaperTradeBookSummary;
  active_trades: PaperTrade[];
  closed_trades: PaperTrade[];
  live_triggered: NearOpportunity[];
  live_near: NearOpportunity[];
  discovery?: LiveRefreshStatus | null;
  hold_vs_unwind?: UnwindDecision | null;
  replay?: FixtureReplayResult | null;
  notes: string[];
};

export type FixtureReplayResult = {
  data_kind: string;
  label: string;
  paper_only: boolean;
  execution_enabled: boolean;
  venue_pair: "matchbook_polymarket" | "matchbook_kalshi" | "polymarket_kalshi";
  solver: "simple" | "generalized";
  close_via: "hold" | "unwind" | "settlement";
  venues: Venue[];
  fill_kinds: string[];
  trade?: PaperTradeDetail | null;
  unwind?: UnwindDecision | null;
  treasury: PaperTreasurySnapshot;
  journal_balanced: boolean;
  opportunity_id?: string | null;
  qualify_only?: boolean;
  preparable_opportunities?: PreparablePaperOpportunity[];
  notes: string[];
};

export function getDemoWalkthrough(): Promise<DemoWalkthroughSnapshot> {
  return request("/paper/demo/walkthrough");
}

export async function resetDemoWalkthrough(payload?: {
  reinitialize_store?: boolean;
  reason?: string;
}): Promise<DemoWalkthroughSnapshot> {
  const response = await fetch(`${API_BASE}/paper/demo/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      reinitialize_store: false,
      reason: "explicit operator demo reset",
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<DemoWalkthroughSnapshot>;
}

export async function runFixtureReplay(payload: {
  venue_pair?: FixtureReplayResult["venue_pair"];
  solver?: FixtureReplayResult["solver"];
  close_via?: FixtureReplayResult["close_via"];
  winning_outcome?: string;
  qualify_only?: boolean;
}): Promise<FixtureReplayResult> {
  const response = await fetch(`${API_BASE}/paper/demo/fixture-replay`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      venue_pair: "matchbook_polymarket",
      solver: "simple",
      close_via: "hold",
      paper_only: true,
      places_orders: false,
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<FixtureReplayResult>;
}

export async function closeDemoTrade(
  tradeId: string,
  payload: { close_via: "unwind" | "settlement"; winning_outcome?: string },
): Promise<FixtureReplayResult> {
  const response = await fetch(
    `${API_BASE}/paper/demo/trades/${encodeURIComponent(tradeId)}/close`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        paper_only: true,
        places_orders: false,
        ...payload,
      }),
      cache: "no-store",
    },
  );
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<FixtureReplayResult>;
}

export async function evaluatePaperClosePlan(
  tradeId: string,
  payload: { quotes: unknown[]; fx?: unknown[]; places_orders?: boolean },
): Promise<UnwindDecision> {
  const response = await fetch(`${API_BASE}/paper/trades/${encodeURIComponent(tradeId)}/close-plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      places_orders: false,
      ...payload,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response.json() as Promise<UnwindDecision>;
}

export type PaperLedgerReconciliation = {
  ok: boolean;
  paper_only: boolean;
  execution_enabled: boolean;
  journal_count: number;
  treasury_event_count: number;
  unique_journal_source_ids: boolean;
  unique_treasury_source_ids: boolean;
  gbp_journals_balanced: boolean;
  native_available: Record<string, string>;
  native_locked: Record<string, string>;
  native_realised_pnl: Record<string, string>;
  native_fees: Record<string, string>;
  mismatches: string[];
  deferred: string[];
  data_kind: string;
};

export function getPaperLedgerReconciliation(): Promise<PaperLedgerReconciliation> {
  return request<PaperLedgerReconciliation>("/paper/ledger/reconciliation");
}
