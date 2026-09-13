import { DiscoveredFixture } from "./api";
import { kickoffLocalLabel, kickoffRelativeLabel, percent, percentPoints } from "./format";

export const DISCOVERY_TABLE_HEADERS = [
  "Fixture",
  "Kickoff",
  "Matchbook",
  "Polymarket",
  "Kalshi",
  "Equivalent",
  "Best arb market",
  "Net edge",
  "Edge vs trigger",
  "Risk",
  "Last refresh",
] as const;

export const EQUIVALENT_MARKETS_HELP =
  "Count of settlement-equivalent market comparisons for this fixture after canonical matching. The row shows the best executable opportunity; expand the fixture to see every comparison, including rejected or non-executable books.";

export const BEST_ARB_MARKET_HELP =
  "The market family and selection/line producing the best currently executable net edge for this fixture. Venue prices on this row are for that same market only.";

export const RISK_HELP =
  "Execution-risk score and band for the same best executable opportunity. Hard freshness and taker-liquidity gates run first; a non-executable book is not scored as a valid arb.";

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

export function venueStatusLabel(present: boolean | null | undefined): string {
  return present ? "matched" : "—";
}

export function venuePriceLabel(price: string | number | null | undefined, present: boolean): string {
  if (!present) return "—";
  if (price === null || price === undefined || price === "") return "matched";
  return decimalText(price);
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

export function kickoffContextLines(item: DiscoveredFixture, now = Date.now()): string[] {
  const relative = kickoffRelativeLabel(item.kickoff_utc, now);
  const live = fixturePhaseLabel(item) === "LIVE";
  const finished = fixturePhaseLabel(item) === "FT";
  if (live) {
    const score = liveScoreLabel(item);
    return score ? [`LIVE · ${score}`] : ["LIVE"];
  }
  if (finished) {
    const score = liveScoreLabel(item);
    return score ? [`FT · ${score}`] : ["FT"];
  }
  const start = relative && !relative.endsWith("ago") ? `Starts ${relative}` : relative;
  return start ? ["Pre-match", start] : ["Pre-match"];
}

export function liveScoreLabel(item: DiscoveredFixture): string | null {
  if (!item.live_score_supported) return null;
  if (item.home_score == null || item.away_score == null) return null;
  return `${item.home_score}–${item.away_score}`;
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
  const equivalent = item.matched_equivalent_count ?? item.matched_market_count ?? 0;
  const near = item.near_executable_market_count;
  const qualifying = item.qualifying_market_count;
  const extras: string[] = [];
  if (qualifying) extras.push(`${qualifying} qualifying`);
  if (near) extras.push(`${near} near`);
  return extras.length ? `${equivalent} · ${extras.join(" · ")}` : String(equivalent);
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

export function lastRefreshLabel(item: DiscoveredFixture, now = Date.now()): string {
  if (item.last_seen_at) {
    const relative = kickoffRelativeLabel(item.last_seen_at, now);
    if (relative?.endsWith("ago")) return relative;
    if (relative === "<1m ago" || relative === "in <1m") return relative;
  }
  if (item.quote_age_ms == null) return "—";
  if (item.quote_age_ms < 1000) return `${item.quote_age_ms}ms`;
  const seconds = Math.round(item.quote_age_ms / 1000);
  return `${seconds}s`;
}

export function netEdgeSummary(item: DiscoveredFixture): string {
  if (
    (item.current_net_edge == null || item.current_net_edge === "") &&
    (item.headline_band === "no_executable_arb" || item.no_comparison_reason === "no_executable_arb")
  ) {
    return "No executable arb";
  }
  return percent(item.current_net_edge);
}

export function signedEdgeVsTrigger(item: DiscoveredFixture): string {
  if (item.distance_to_trigger_pp == null || item.distance_to_trigger_pp === "") {
    return opportunityStateLabel(item);
  }
  const parsed = Number(item.distance_to_trigger_pp);
  if (!Number.isFinite(parsed)) return percentPoints(item.distance_to_trigger_pp);
  // Backend distance is (trigger - current). Edge vs trigger flips the sign so
  // qualifying is positive (+0.4pp) and below-trigger is negative (-0.6pp).
  const edgeVsTrigger = -parsed;
  const signed = `${edgeVsTrigger > 0 ? "+" : ""}${edgeVsTrigger.toFixed(1)}pp`;
  return `${signed} · ${opportunityStateLabel(item)}`;
}

export function edgeTone(item: DiscoveredFixture): "qualifying" | "near" | "none" {
  const state = opportunityStateLabel(item);
  if (item.headline_band === "qualifying" || state === "qualifying") return "qualifying";
  if (item.headline_band === "near_executable" || state === "near") return "near";
  return "none";
}

export function riskLabel(item: DiscoveredFixture): string {
  if (item.execution_risk_score == null) return "—";
  const band = item.execution_risk_band
    ? item.execution_risk_band.replaceAll("_", " ").replace(/\b\w/g, (ch) => ch.toUpperCase())
    : null;
  return band ? `${item.execution_risk_score} · ${band}` : String(item.execution_risk_score);
}

export function riskReasonsLabel(item: DiscoveredFixture): string {
  const reasons = item.execution_risk_reasons ?? [];
  if (!reasons.length) return "Hard executability gates apply before risk scoring.";
  return reasons.map((reason) => reason.replaceAll("_", " ")).join(" · ");
}

export function bestArbMarketLabel(item: DiscoveredFixture): string {
  if (item.best_arb_market) return item.best_arb_market;
  if (item.headline_band === "no_executable_arb" || item.no_comparison_reason === "no_executable_arb") {
    return "No executable arb";
  }
  return "—";
}

export function discoveredFixtureCells(
  item: DiscoveredFixture,
): Record<(typeof DISCOVERY_TABLE_HEADERS)[number], string> {
  const kickoffLines = kickoffContextLines(item);
  return {
    Fixture: `${item.home_team} v ${item.away_team}`,
    Kickoff: [kickoffLocalLabel(item.kickoff_utc), ...kickoffLines].join(" · "),
    Matchbook: venuePriceLabel(item.best_matchbook_price, venuePresent(item.matchbook_matched)),
    Polymarket: venuePriceLabel(item.best_polymarket_price, venuePresent(item.polymarket_matched)),
    Kalshi: venuePriceLabel(item.best_kalshi_price, venuePresent(item.kalshi_matched)),
    Equivalent: equivalentCountLabel(item),
    "Best arb market": bestArbMarketLabel(item),
    "Net edge": netEdgeSummary(item),
    "Edge vs trigger": signedEdgeVsTrigger(item),
    Risk: riskLabel(item),
    "Last refresh": lastRefreshLabel(item),
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
    item.quote_age_ms != null ? `quote age ${item.quote_age_ms}ms` : null,
    item.best_matchbook_price != null ? `MB ${decimalText(item.best_matchbook_price)}` : null,
    item.best_polymarket_price != null ? `PM ${decimalText(item.best_polymarket_price)}` : null,
    item.best_kalshi_price != null ? `K ${decimalText(item.best_kalshi_price)}` : null,
    arbClaimLabel(item),
  ].filter((line): line is string => Boolean(line));
}
