import { DiscoveredFixture } from "./api";
import { kickoffLocalLabel, percent, percentPoints } from "./format";

export const DISCOVERY_TABLE_HEADERS = [
  "Fixture",
  "Kickoff",
  "Phase",
  "Venues",
  "Equivalent",
  "Net edge",
  "State",
] as const;

function decimalText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function venuePresent(flag: boolean | null | undefined): boolean {
  return Boolean(flag);
}

export function venueCoverageLabel(item: DiscoveredFixture): string {
  const bits = [
    venuePresent(item.matchbook_matched) ? "MB" : "MB · —",
    venuePresent(item.polymarket_matched) ? "PM" : "PM · —",
    venuePresent(item.kalshi_matched) ? "K" : "K · —",
  ];
  return bits.join(" / ");
}

export function polymarketCoverageLabel(item: DiscoveredFixture): string {
  if (item.polymarket_matched) return "matched";
  return item.no_comparison_reason || "unmatched / no supported Polymarket coverage";
}

export function fixtureHref(item: DiscoveredFixture): string {
  return `/arbitrage/fixtures/${encodeURIComponent(item.canonical_event_id)}`;
}

export function fixturePhaseLabel(item: DiscoveredFixture): string {
  if (item.in_running) return "LIVE";
  const status = (item.fixture_status || "").toLowerCase();
  if (
    status.includes("ft") ||
    status.includes("final") ||
    status.includes("complete") ||
    status.includes("settled") ||
    status.includes("closed")
  ) {
    return "FT";
  }
  if (status.includes("live") || status.includes("in-play") || status.includes("in_play")) {
    return "LIVE";
  }
  return "PRE";
}

export function kickoffClockLabel(kickoffUtc: string): string {
  return kickoffLocalLabel(kickoffUtc);
}

export function inventorySummaryLabel(item: DiscoveredFixture): string {
  const discovered = item.discovered_market_count ?? item.matched_market_count;
  const equivalent = item.matched_equivalent_count ?? item.matched_market_count;
  return `${discovered} discovered · ${equivalent} equivalent`;
}

export function equivalentCountLabel(item: DiscoveredFixture): string {
  return String(item.matched_equivalent_count ?? item.matched_market_count ?? 0);
}

export function opportunityStateLabel(item: DiscoveredFixture): string {
  if (item.opportunity_state) return item.opportunity_state.replaceAll("_", " ");
  if (item.solver_is_arbitrage) return "qualifying";
  return item.polymarket_matched || item.kalshi_matched || item.matchbook_matched
    ? "matched"
    : "unmatched";
}

export function arbClaimLabel(item: DiscoveredFixture): string {
  return item.solver_is_arbitrage ? "solver-validated paper arb" : "not arbitrage";
}

export function freshnessLabel(item: DiscoveredFixture): string {
  if (item.quote_age_ms == null) {
    return item.quote_age_basis ? `unknown · ${item.quote_age_basis}` : "unavailable";
  }
  const basis = item.quote_age_basis ? ` · ${item.quote_age_basis}` : "";
  return `${item.quote_age_ms}ms${basis}`;
}

export function netEdgeSummary(item: DiscoveredFixture): string {
  const edge = percent(item.current_net_edge);
  const distance = percentPoints(item.distance_to_trigger_pp);
  if (item.distance_to_trigger_pp == null || item.distance_to_trigger_pp === "") {
    return edge;
  }
  return `${edge} · ${distance} to trigger`;
}

export function discoveredFixtureCells(
  item: DiscoveredFixture,
): Record<(typeof DISCOVERY_TABLE_HEADERS)[number], string> {
  return {
    Fixture: `${item.home_team} v ${item.away_team}`,
    Kickoff: kickoffLocalLabel(item.kickoff_utc),
    Phase: fixturePhaseLabel(item),
    Venues: venueCoverageLabel(item),
    Equivalent: equivalentCountLabel(item),
    "Net edge": netEdgeSummary(item),
    State: opportunityStateLabel(item),
  };
}

export function technicalDetailLines(item: DiscoveredFixture): string[] {
  return [
    item.canonical_event_id ? `canonical ${item.canonical_event_id}` : null,
    item.source_event_id ? `${item.source} ${item.source_event_id}` : null,
    item.kickoff_utc ? `kickoff UTC ${item.kickoff_utc}` : null,
    item.market_family ? `family ${item.market_family}` : null,
    item.outcome_context ? `outcomes ${item.outcome_context}` : null,
    item.no_comparison_reason ? `reason ${item.no_comparison_reason}` : null,
    item.quote_age_basis ? `quote basis ${item.quote_age_basis}` : null,
    item.best_matchbook_price != null ? `MB ${decimalText(item.best_matchbook_price)}` : null,
    item.best_polymarket_price != null ? `PM ${decimalText(item.best_polymarket_price)}` : null,
    item.best_kalshi_price != null ? `K ${decimalText(item.best_kalshi_price)}` : null,
    arbClaimLabel(item),
  ].filter((line): line is string => Boolean(line));
}
