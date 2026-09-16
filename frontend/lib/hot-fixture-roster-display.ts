import { DiscoveredFixture, LiveRefreshStatus } from "./api";
import {
  fixtureHref,
  kickoffContextLines,
  lastRefreshLabel,
  venuePresent,
} from "./discovered-fixture-display";
import { percent } from "./format";

export const HOT_ROSTER_TITLE = "HOT Fixtures / Fast Scan";

export const HOT_ROSTER_COPY =
  "Fixtures currently scheduled for high-frequency Fast Scan. Presence here is not an arbitrage. Opportunity Monitor remains the current/near qualifying surface.";

export const HOT_ROSTER_EMPTY =
  "No HOT fixtures. Fast Scan roster is empty. Opportunity Monitor is a separate current/near-opportunity surface.";

export const HOT_ROSTER_UNAVAILABLE =
  "HOT roster unavailable. No fabricated fixtures.";

export const HOT_REASON_IN_PLAY = "IN PLAY";
export const HOT_REASON_KICKOFF_HORIZON = "KICKOFF < 60M";
export const HOT_REASON_POST_KICKOFF_STATUS_PENDING = "POST-KICKOFF STATUS PENDING";
export const HOT_REASON_ARB_PROMOTION = "ARB PROMOTION";

export const HOT_ROSTER_HEADERS = [
  "Fixture",
  "Kickoff / in-play",
  "Why HOT",
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

export function hotFixtureRow(item: DiscoveredFixture, nowMs: number | null = null): HotFixtureRow {
  return {
    id: item.canonical_event_id,
    href: fixtureHref(item),
    name: hotFixtureName(item),
    competition: hotCompetitionLabel(item),
    kickoffUtc: item.kickoff_utc,
    kickoffLines: kickoffContextLines(item, nowMs),
    reasons: hotReasonLabels(item),
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
