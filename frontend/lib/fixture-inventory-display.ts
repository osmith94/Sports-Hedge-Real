import { FixtureMarketInventoryRow, VenueMarketFacts, VenueQuoteFact } from "./api";
import { percent, percentPoints } from "./format";

export function coverageLabel(row: FixtureMarketInventoryRow): string {
  const venues: string[] = [];
  if (row.matchbook) venues.push("Matchbook");
  if (row.polymarket) venues.push("Polymarket");
  if (venues.length === 2) return "Matchbook + Polymarket";
  if (venues.length === 1) return `${venues[0]} only`;
  return "no venue payload";
}

export function comparisonLabel(status: string): string {
  return status.replaceAll("_", " ");
}

export const INVENTORY_STATUS_LABELS = [
  "matched_equivalent",
  "venue_only",
  "settlement_mismatch",
  "unsupported_outcome_model",
] as const;

function decimalText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function quoteSummary(facts: VenueMarketFacts | null | undefined): string {
  if (!facts) return "—";
  const backs = facts.best_backs.filter((quote) => quote.decimal_odds != null);
  if (!backs.length) return "unknown price";
  return backs.map((quote) => formatQuote(quote)).join(" · ");
}

function formatQuote(quote: VenueQuoteFact): string {
  const odds = decimalText(quote.decimal_odds);
  const size = quote.size_at_touch == null ? "" : ` / ${quote.size_at_touch}`;
  return `${quote.outcome} ${odds}${size}`;
}

export function economicsSummary(facts: VenueMarketFacts | null | undefined): string {
  if (!facts) return "—";
  const fee = facts.fee_status ? `fee ${facts.fee_status}` : "fee unknown";
  const fx = facts.fx_status ? `fx ${facts.fx_status}` : "fx n/a";
  const depth =
    facts.usable_depth_at_touch == null ? "depth unknown" : `touch ${facts.usable_depth_at_touch}`;
  return `${fee} · ${fx} · ${depth}`;
}

export function solverFacts(row: FixtureMarketInventoryRow): string {
  const model = row.solver_model ? row.solver_model.replaceAll("_", " ") : null;
  if (!row.entered_solver) {
    const reason = row.reason ? ` · ${row.reason}` : "";
    if (model) return `${model}${reason}`;
    return row.reason ? `not in solver · ${row.reason}` : "not in solver";
  }
  const prefix = model ? `${model} · ` : "";
  if (!row.solver_is_arbitrage && row.reason) {
    return `${prefix}evaluated · ${row.reason}`;
  }
  return `${prefix}net ${percent(row.current_net_edge)} · trigger ${percent(row.trigger_net_edge)} · ${percentPoints(row.distance_to_trigger_pp)}`;
}
