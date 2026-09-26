import { DiscoveredFixture, HotRosterEntry, LiveRefreshStatus } from "./api";
import {
  NOT_EVALUATED_MARKET_FETCH_LABEL,
  NOT_EVALUATED_SCAN_BUDGET_LABEL,
  NOT_EVALUATED_SINGLE_VENUE_LABEL,
  NOT_EVALUATED_CROSS_VENUE_UNAVAILABLE_LABEL,
  NOT_EVALUATED_UPPER_BOUND_BELOW_MIN_NET_LABEL,
  HOT_RELATIONSHIP_MISSING_LABEL,
  HOT_REVALIDATION_NEEDED_LABEL,
  fixtureHref,
  kickoffContextLines,
  lastRefreshLabel,
  venuePresent,
} from "./discovered-fixture-display";
import { percent } from "./format";

export const HOT_ZONE_KICKER = "HOT Zone";
export const HOT_PRICING_HEADING = "HOT PRICING";
export const DEFERRED_CROSS_VENUE_HEADING = "DEFERRED / AWAITING CROSS-VENUE";
export const POST_KICKOFF_PENDING_HEADING = "POST-KICKOFF PENDING";
export const DEFERRED_NOT_HOT_CAPACITY =
  "Retained by current state. Not consuming HOT pricing capacity.";
export const HOT_ROSTER_TITLE = "HOT pricing fixtures";

export const HOT_ROSTER_COPY =
  "Fixtures currently scheduled for high-frequency HOT pricing. Presence here is not an arbitrage. Opportunity Monitor remains the current/near qualifying surface.";

export const HOT_ROSTER_EMPTY =
  "No HOT fixtures. HOT pricing roster is empty. Opportunity Monitor is a separate current/near-opportunity surface.";

export const HOT_ROSTER_UNAVAILABLE =
  "HOT roster unavailable. No fabricated fixtures.";

export const HOT_REASON_IN_PLAY = "IN PLAY";
export const HOT_REASON_KICKOFF_HORIZON = "KICKOFF < 60M";
export const HOT_REASON_POST_KICKOFF_STATUS_PENDING = "POST-KICKOFF STATUS PENDING";
export const HOT_REASON_ARB_PROMOTION = "ARB PROMOTION";
export const HOT_REASON_NET_PROXIMITY_PREFIX = "NET PROXIMITY";

export const HOT_ROSTER_HEADERS = [
  "Fixture",
  "Kickoff / in-play",
  "Why HOT",
  "Evaluation",
  "Venues",
  "Last refresh",
  "Equivalent",
  "Net edge",
] as const;

export type HotFixtureRow = {
  id: string;
  href: string;
  name: string;
  competition: string;
  kickoffUtc: string;
  kickoffLines: string[];
  reasons: string[];
  evaluationLabel: string;
  venuesLabel: string;
  lastRefresh: string;
  equivalentLabel: string;
  netEdgeLabel: string;
  hasQualifyingOpportunity: boolean;
};

export function isHotScanLane(lane: string | null | undefined): boolean {
  return String(lane || "").trim().toLowerCase() === "hot";
}

const AWAITING_CROSS_VENUE_STATES = new Set([
  "single_venue_no_cross_venue_candidate",
  "cross_venue_unavailable",
  "hot_relationship_missing",
]);

export function isAwaitingCrossVenue(item: DiscoveredFixture): boolean {
  return AWAITING_CROSS_VENUE_STATES.has(String(item.market_evaluation_state || ""));
}

export function isPostKickoffPending(item: DiscoveredFixture): boolean {
  return (item.hot_reasons ?? []).includes(HOT_REASON_POST_KICKOFF_STATUS_PENDING);
}

export function hotFixtures(status: LiveRefreshStatus | null | undefined): DiscoveredFixture[] {
  const fixtures = status?.discovered_fixtures ?? [];
  return fixtures.filter((item) => isHotScanLane(item.scan_lane));
}

export function deferredAwaitingFixtures(
  status: LiveRefreshStatus | null | undefined,
): DiscoveredFixture[] {
  const fixtures = status?.discovered_fixtures ?? [];
  return fixtures.filter((item) => isAwaitingCrossVenue(item));
}

export function postKickoffPendingFixtures(
  status: LiveRefreshStatus | null | undefined,
): DiscoveredFixture[] {
  return hotFixtures(status).filter(
    (item) => isPostKickoffPending(item) && !isAwaitingCrossVenue(item),
  );
}

function finiteCount(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, Math.trunc(value));
  return null;
}

export function hotPricingCount(status: LiveRefreshStatus | null | undefined): number {
  const priced = finiteCount(status?.price_engine?.hot?.pricing_fixtures);
  if (priced != null) return priced;
  const worker = finiteCount(status?.hot?.fixture_count);
  const lane = hotFixtures(status);
  const deferredLane = lane.filter((item) => isAwaitingCrossVenue(item));
  if (worker != null) {
    if (lane.length > 0 && deferredLane.length === lane.length) return 0;
    return worker;
  }
  return lane.filter((item) => !isAwaitingCrossVenue(item) && !isPostKickoffPending(item)).length;
}

export function hotPricingFixtures(
  status: LiveRefreshStatus | null | undefined,
): DiscoveredFixture[] {
  if (hotPricingCount(status) <= 0) return [];
  return hotFixtures(status).filter(
    (item) => !isAwaitingCrossVenue(item) && !isPostKickoffPending(item),
  );
}

export function hotReasonLabels(item: DiscoveredFixture): string[] {
  if (Array.isArray(item.hot_reasons)) {
    return item.hot_reasons.filter((reason) => Boolean(reason && String(reason).trim()));
  }
  // Legacy payloads without the read-model field: only IN PLAY is a current-state
  // fact the UI can show without duplicating scheduler horizon / promotion logic.
  if (item.in_running === true) return [HOT_REASON_IN_PLAY];
  return [];
}

export function hotVenuePresenceLabel(item: DiscoveredFixture): string {
  return [
    venuePresent(item.matchbook_matched) ? "MB" : "—",
    venuePresent(item.polymarket_matched) ? "PM" : "—",
    venuePresent(item.kalshi_matched) ? "K" : "—",
  ].join(" / ");
}

export function hotEquivalentLabel(item: DiscoveredFixture): string {
  if (item.matched_equivalent_count == null) return "—";
  return String(item.matched_equivalent_count);
}

export function hotNetEdgeLabel(item: DiscoveredFixture): string {
  return percent(item.current_net_edge);
}

export function hotCompetitionLabel(item: DiscoveredFixture): string {
  const code = item.target_competition_code ? ` · ${item.target_competition_code}` : "";
  return `${item.competition}${code}`;
}

export function hotFixtureName(item: DiscoveredFixture): string {
  return `${item.home_team} v ${item.away_team}`;
}

export function hotRosterBadgeLabel(
  available: boolean,
  status: LiveRefreshStatus | null,
): string {
  if (!available || !status) return "UNAVAILABLE";
  const count = hotPricingCount(status);
  return count ? `${count} HOT PRICING` : "HOT PRICING 0";
}

function diagnosticCount(diagnostics: Record<string, unknown> | null | undefined, key: string): number | null {
  const value = diagnostics?.[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

export function hotPaperDecisionCount(status: LiveRefreshStatus | null): number | null {
  if (!status) return null;
  const fromHot = diagnosticCount(status.hot?.last_diagnostics ?? null, "paper_decision_count");
  if (fromHot != null) return fromHot;
  const hotCompleted = status.hot?.last_completed_at;
  if (hotCompleted && status.last_completed_at === hotCompleted && status.last_paper_decisions != null) {
    return status.last_paper_decisions;
  }
  if (hotCompleted && !status.universe?.last_completed_at && status.last_paper_decisions != null) {
    return status.last_paper_decisions;
  }
  return null;
}

export function fastScanRosterSummary(status: LiveRefreshStatus | null): string {
  const hotCount = hotPricingCount(status);
  const evaluated = status?.hot?.evaluated_count ?? 0;
  const decisions = hotPaperDecisionCount(status);
  const decisionLabel = decisions == null ? "— paper decisions" : `${decisions} paper decisions`;
  const cadence = status?.hot?.cadence_seconds;
  const cadenceLabel = cadence ? ` · ${cadence}s` : "";
  return `HOT PRICING ${hotCount}${cadenceLabel} · ${evaluated} evaluated · ${decisionLabel}`;
}

export function deferredAwaitingCount(status: LiveRefreshStatus | null | undefined): number {
  if (typeof status?.deferred_awaiting_count === "number" && Number.isFinite(status.deferred_awaiting_count)) {
    return Math.max(0, Math.trunc(status.deferred_awaiting_count));
  }
  return deferredAwaitingFixtures(status).length;
}

export function deferredRosterSummary(status: LiveRefreshStatus | null): string {
  const count = deferredAwaitingCount(status);
  return `${DEFERRED_CROSS_VENUE_HEADING} ${count} · ${DEFERRED_NOT_HOT_CAPACITY}`;
}

export function hotEvaluationLabel(item: DiscoveredFixture): string {
  if (item.solver_is_arbitrage === true) return "qualifying";
  if (item.market_evaluation_state === "not_evaluated_scan_deadline") {
    return item.market_evaluation_reason
      ? `${NOT_EVALUATED_SCAN_BUDGET_LABEL} · ${item.market_evaluation_reason}`
      : NOT_EVALUATED_SCAN_BUDGET_LABEL;
  }
  if (item.market_evaluation_state === "market_fetch_unavailable") {
    return item.market_evaluation_reason
      ? `${NOT_EVALUATED_MARKET_FETCH_LABEL} · ${item.market_evaluation_reason}`
      : NOT_EVALUATED_MARKET_FETCH_LABEL;
  }
  if (item.market_evaluation_state === "hot_relationship_missing") {
    return item.market_evaluation_reason
      ? `${HOT_RELATIONSHIP_MISSING_LABEL} · ${item.market_evaluation_reason}`
      : HOT_RELATIONSHIP_MISSING_LABEL;
  }
  if (item.market_evaluation_state === "single_venue_no_cross_venue_candidate") {
    return item.market_evaluation_reason
      ? `${NOT_EVALUATED_SINGLE_VENUE_LABEL} · ${item.market_evaluation_reason}`
      : NOT_EVALUATED_SINGLE_VENUE_LABEL;
  }
  if (item.market_evaluation_state === "cross_venue_unavailable") {
    return item.market_evaluation_reason
      ? `${NOT_EVALUATED_CROSS_VENUE_UNAVAILABLE_LABEL} · ${item.market_evaluation_reason}`
      : NOT_EVALUATED_CROSS_VENUE_UNAVAILABLE_LABEL;
  }
  if (item.market_evaluation_state === "upper_bound_below_min_net") {
    return item.market_evaluation_reason
      ? `${NOT_EVALUATED_UPPER_BOUND_BELOW_MIN_NET_LABEL} · ${item.market_evaluation_reason}`
      : NOT_EVALUATED_UPPER_BOUND_BELOW_MIN_NET_LABEL;
  }
  const reason = item.market_evaluation_reason || item.no_comparison_reason;
  if (reason === "hot_revalidation_needed") {
    return HOT_REVALIDATION_NEEDED_LABEL;
  }
  if (item.market_evaluation_state === "evaluated") {
    return reason ? `evaluated · ${reason}` : "evaluated · no qualifying opportunity";
  }
  if (reason) return reason.replaceAll("_", " ");
  return "—";
}

export function hotFixtureRow(item: DiscoveredFixture, nowMs: number | null = null): HotFixtureRow {
  return {
    id: item.canonical_event_id,
    href: fixtureHref(item),
    name: hotFixtureName(item),
    competition: hotCompetitionLabel(item),
    kickoffUtc: item.kickoff_utc,
    kickoffLines: kickoffContextLines(item, nowMs),
    reasons: hotReasonLabels(item),
    evaluationLabel: hotEvaluationLabel(item),
    venuesLabel: hotVenuePresenceLabel(item),
    lastRefresh: lastRefreshLabel(item, nowMs),
    equivalentLabel: hotEquivalentLabel(item),
    netEdgeLabel: hotNetEdgeLabel(item),
    hasQualifyingOpportunity: item.solver_is_arbitrage === true,
  };
}

function rosterFixture(entry: HotRosterEntry): DiscoveredFixture {
  return {
    source: "matchbook",
    source_event_id: entry.canonical_event_id,
    canonical_event_id: entry.canonical_event_id,
    home_team: entry.home_team,
    away_team: entry.away_team,
    competition: entry.competition,
    sport: entry.sport,
    target_competition_code: entry.target_competition_code,
    kickoff_utc: entry.kickoff_utc,
    matchbook_matched: entry.matchbook_matched,
    polymarket_matched: Boolean(entry.polymarket_matched),
    kalshi_matched: entry.kalshi_matched,
    in_running: entry.in_running,
    fixture_status: entry.fixture_status,
    live_score_supported: Boolean(entry.live_score_supported),
    home_score: entry.home_score,
    away_score: entry.away_score,
    last_seen_at: entry.last_seen_at || entry.last_scanned_at || entry.kickoff_utc,
    last_scanned_at: entry.last_scanned_at,
    matched_market_count: entry.matched_equivalent_count ?? 0,
    matched_equivalent_count: entry.matched_equivalent_count,
    solver_is_arbitrage: Boolean(entry.solver_is_arbitrage),
    current_net_edge: entry.current_net_edge,
    hot_reasons: entry.hot_reasons,
    scan_lane: entry.scan_lane ?? "hot",
    market_evaluation_state: entry.market_evaluation_state,
    market_evaluation_reason: entry.market_evaluation_reason,
  };
}

export function hotFixtureRows(
  status: LiveRefreshStatus | null,
  nowMs: number | null = null,
): HotFixtureRow[] {
  if (Array.isArray(status?.hot_roster)) {
    return status.hot_roster
      .map(rosterFixture)
      .filter((item) => !isAwaitingCrossVenue(item) && !isPostKickoffPending(item))
      .map((item) => hotFixtureRow(item, nowMs));
  }
  return hotPricingFixtures(status).map((item) => hotFixtureRow(item, nowMs));
}

export function deferredFixtureRows(
  status: LiveRefreshStatus | null,
  nowMs: number | null = null,
): HotFixtureRow[] {
  return deferredAwaitingFixtures(status).map((item) => hotFixtureRow(item, nowMs));
}

export function postKickoffPendingRows(
  status: LiveRefreshStatus | null,
  nowMs: number | null = null,
): HotFixtureRow[] {
  if (Array.isArray(status?.hot_roster)) {
    return status.hot_roster
      .map(rosterFixture)
      .filter((item) => isPostKickoffPending(item) && !isAwaitingCrossVenue(item))
      .map((item) => hotFixtureRow(item, nowMs));
  }
  return postKickoffPendingFixtures(status).map((item) => hotFixtureRow(item, nowMs));
}
