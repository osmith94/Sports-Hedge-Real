import { DiscoveredFixture } from "./api";
import { percent, percentPoints } from "./format";

export const DISCOVERY_TABLE_HEADERS = [
  "Fixture",
  "Kickoff",
  "Matchbook status",
  "Polymarket",
  "Matched markets",
  "Family / outcomes",
  "Best Matchbook",
  "Best Polymarket",
  "Net edge",
  "Trigger",
  "Distance",
  "Freshness",
  "Comparison",
  "Arb claim",
  "Score",
  "Last seen",
] as const;

function decimalText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function polymarketCoverageLabel(item: DiscoveredFixture): string {
  if (item.polymarket_matched) return "matched";
  return item.no_comparison_reason || "unmatched / no supported Polymarket coverage";
}

export function fixtureHref(item: DiscoveredFixture): string {
  return `/arbitrage/fixtures/${encodeURIComponent(item.canonical_event_id)}`;
}

export function fixturePhaseLabel(item: DiscoveredFixture): string {
  if (item.in_running) return "IN-PLAY";
  const status = (item.fixture_status || "").toLowerCase();
  if (status.includes("open") || status.includes("pre") || !status) return "PRE";
  return item.fixture_status?.toUpperCase() || "PRE";
}

export function kickoffClockLabel(kickoffUtc: string): string {
  const stamp = new Date(kickoffUtc);
  if (Number.isNaN(stamp.getTime())) return "—";
  return stamp.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    hour12: false,
  });
}

export function inventorySummaryLabel(item: DiscoveredFixture): string {
  const discovered = item.discovered_market_count ?? item.matched_market_count;
  const equivalent = item.matched_equivalent_count ?? item.matched_market_count;
  return `${discovered} discovered · ${equivalent} equivalent`;
}

export function freshnessLabel(item: DiscoveredFixture): string {
  if (item.quote_age_ms == null) {
    return item.quote_age_basis ? `unknown · ${item.quote_age_basis}` : "unavailable";
  }
  const basis = item.quote_age_basis ? ` · ${item.quote_age_basis}` : "";
  return `${item.quote_age_ms}ms${basis}`;
}

export function discoveredFixtureCells(item: DiscoveredFixture): Record<(typeof DISCOVERY_TABLE_HEADERS)[number], string> {
  return {
    Fixture: `${item.home_team} v ${item.away_team}`,
    Kickoff: item.kickoff_utc,
    "Matchbook status": item.fixture_status ?? "—",
    Polymarket: polymarketCoverageLabel(item),
    "Matched markets": inventorySummaryLabel(item),
    "Family / outcomes": [item.market_family, item.outcome_context].filter(Boolean).join(" · ") || "—",
    "Best Matchbook": decimalText(item.best_matchbook_price),
    "Best Polymarket": decimalText(item.best_polymarket_price),
    "Net edge": percent(item.current_net_edge),
    Trigger: percent(item.trigger_net_edge),
    Distance: percentPoints(item.distance_to_trigger_pp),
    Freshness: freshnessLabel(item),
    Comparison: item.no_comparison_reason ?? "backend comparison available",
    "Arb claim": arbClaimLabel(item),
    Score: "",
    "Last seen": item.last_seen_at,
  };
}
