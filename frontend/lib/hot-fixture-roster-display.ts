import { DiscoveredFixture, LiveRefreshStatus } from "./api";
import {
  NOT_EVALUATED_MARKET_FETCH_LABEL,
  NOT_EVALUATED_SCAN_BUDGET_LABEL,
  HOT_RELATIONSHIP_MISSING_LABEL,
  HOT_REVALIDATION_NEEDED_LABEL,
  fixtureHref,
  kickoffContextLines,
  lastRefreshLabel,
  venuePresent,
} from "./discovered-fixture-display";
import { percent } from "./format";

export const HOT_ZONE_KICKER = "HOT Zone";
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

export function hotFixtures(status: LiveRefreshStatus | null | undefined): DiscoveredFixture[] {
  const fixtures = status?.discovered_fixtures ?? [];
  return fixtures.filter((item) => isHotScanLane(item.scan_lane));
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
  const count = hotFixtures(status).length;
  return count ? `${count} HOT` : "EMPTY";
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
  const hotCount = status?.hot?.fixture_count ?? hotFixtures(status).length;
  const evaluated = status?.hot?.evaluated_count ?? 0;
  const decisions = hotPaperDecisionCount(status);
  const decisionLabel = decisions == null ? "— paper decisions" : `${decisions} paper decisions`;
  return `${hotCount} HOT · ${evaluated} evaluated · ${decisionLabel}`;
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

export function hotFixtureRows(
  status: LiveRefreshStatus | null,
  nowMs: number | null = null,
): HotFixtureRow[] {
  return hotFixtures(status).map((item) => hotFixtureRow(item, nowMs));
}
