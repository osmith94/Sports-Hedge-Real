import { LiveRefreshStatus, Venue } from "./api";
import { isOperatorDisabledHealth, isProviderHealthFailure } from "./venue-health-display";

export const OPERATOR_SCAN_VENUES: Venue[] = ["matchbook", "polymarket", "kalshi"];

export const VENUE_SHORT: Record<string, string> = {
  matchbook: "MB",
  polymarket: "PM",
  kalshi: "K",
};

export type VenueTruth = {
  venue: Venue;
  short: string;
  configured: boolean;
  participated: boolean;
  providerFailed: boolean;
  operatorDisabled: boolean;
  health: string | undefined;
};

function asVenueList(values: readonly string[] | undefined): Venue[] {
  if (!values) return [];
  const allowed = new Set<string>(OPERATOR_SCAN_VENUES);
  const seen = new Set<string>();
  const ordered: Venue[] = [];
  for (const raw of values) {
    const venue = String(raw || "").trim().toLowerCase();
    if (!allowed.has(venue) || seen.has(venue)) continue;
    seen.add(venue);
    ordered.push(venue as Venue);
  }
  return ordered;
}

export function laneConfiguredVenues(
  status: LiveRefreshStatus | null | undefined,
  lane: "hot" | "universe",
): Venue[] {
  const fromPending = lane === "hot" ? status?.hot?.pending_venues : status?.universe?.pending_venues;
  const fromParticipation =
    lane === "hot" ? status?.venue_participation?.hot : status?.venue_participation?.universe;
  const fromActive = lane === "hot" ? status?.hot?.active_venues : status?.universe?.active_venues;
  const configured = asVenueList(fromPending ?? fromParticipation ?? fromActive);
  return configured;
}

export function laneVenueTruths(
  status: LiveRefreshStatus | null | undefined,
  lane: "hot" | "universe",
): VenueTruth[] {
  const configured = new Set(laneConfiguredVenues(status, lane));
  const health = status?.venue_health ?? {};
  return OPERATOR_SCAN_VENUES.map((venue) => {
    const value = health[venue];
    const providerFailed = isProviderHealthFailure(value);
    const operatorDisabled = isOperatorDisabledHealth(value) || !configured.has(venue);
    return {
      venue,
      short: VENUE_SHORT[venue] ?? venue,
      configured: configured.has(venue),
      participated: configured.has(venue) && value === "ok",
      providerFailed,
      operatorDisabled,
      health: value,
    };
  });
}

export function shortVenueList(venues: readonly string[]): string {
  if (!venues.length) return "no venues";
  return venues.map((venue) => VENUE_SHORT[venue] ?? venue).join("·");
}

export function lastScanVenueClause(
  status: LiveRefreshStatus | null | undefined,
  lane: "hot" | "universe",
): string | null {
  const truths = laneVenueTruths(status, lane);
  const health = status?.venue_health;
  const configured = truths.filter((item) => item.configured);
  if (!configured.length && (health == null || Object.keys(health).length === 0)) {
    return null;
  }
  if (health == null || Object.keys(health).length === 0) {
    return `configured ${shortVenueList(configured.map((item) => item.venue))} (availability not in this snapshot)`;
  }
  const participated = truths.filter((item) => item.participated).map((item) => item.venue);
  const failed = truths.filter((item) => item.configured && item.providerFailed);
  const bits: string[] = [];
  bits.push(participated.length ? `last scan ${shortVenueList(participated)}` : "last scan no venue data");
  for (const item of failed) {
    bits.push(`${item.short} ${item.health}`);
  }
  return bits.join(" · ");
}

export function discoveryParticipatedVenues(status: LiveRefreshStatus | null | undefined): Venue[] {
  const health = status?.venue_health ?? {};
  const hot = new Set(asVenueList(status?.hot?.active_venues ?? status?.venue_participation?.hot));
  const universe = new Set(
    asVenueList(status?.universe?.active_venues ?? status?.venue_participation?.universe),
  );
  const configured = new Set<Venue>([...hot, ...universe]);
  if (configured.size === 0) {
    for (const venue of OPERATOR_SCAN_VENUES) configured.add(venue);
  }
  return OPERATOR_SCAN_VENUES.filter((venue) => configured.has(venue) && health[venue] === "ok");
}

export function discoveryFailedVenues(status: LiveRefreshStatus | null | undefined): VenueTruth[] {
  const health = status?.venue_health ?? {};
  return OPERATOR_SCAN_VENUES.filter((venue) => isProviderHealthFailure(health[venue])).map((venue) => ({
    venue,
    short: VENUE_SHORT[venue] ?? venue,
    configured: true,
    participated: false,
    providerFailed: true,
    operatorDisabled: false,
    health: health[venue],
  }));
}

export function venueChipLabel(truth: VenueTruth): string {
  if (!truth.configured) return `${truth.short} OFF`;
  if (truth.providerFailed) return `${truth.short} ON · ${String(truth.health).toUpperCase()}`;
  return `${truth.short} ON`;
}

export function venueChipTitle(truth: VenueTruth, laneLabel: string): string {
  const configured = truth.configured
    ? "configured ON — operator will call this provider on the next cycle"
    : "configured OFF — no discovery/market/book calls for this lane";
  if (!truth.configured) {
    return `${laneLabel} ${truth.short}: ${configured}`;
  }
  if (truth.providerFailed) {
    return `${laneLabel} ${truth.short}: ${configured}. Last scan did not receive ${truth.short} data (${truth.health}).`;
  }
  if (truth.participated) {
    return `${laneLabel} ${truth.short}: ${configured}. Last scan received ${truth.short} data.`;
  }
  if (truth.health) {
    return `${laneLabel} ${truth.short}: ${configured}. Last scan health ${truth.health}.`;
  }
  return `${laneLabel} ${truth.short}: ${configured}. Last-scan availability is not in this snapshot.`;
}

export function lastScanVenuesLabel(status: LiveRefreshStatus | null, available: boolean): string {
  if (!available || !status) return "—";
  const health = status.venue_health;
  const hot = status.hot?.active_venues;
  const universe = status.universe?.active_venues;
  if (health == null || Object.keys(health).length === 0) {
    if (hot == null && universe == null) return "—";
    const unique = [...new Set([...(hot ?? []), ...(universe ?? [])])];
    return unique.length
      ? `configured ${unique.join(" · ")} (availability not in this snapshot)`
      : "no venues";
  }
  const participated = discoveryParticipatedVenues(status);
  const failed = discoveryFailedVenues(status);
  const bits: string[] = [];
  bits.push(participated.length ? participated.join(" · ") : "no venue data");
  for (const item of failed) {
    bits.push(`${item.venue} ${item.health}`);
  }
  return bits.join(" · ");
}
