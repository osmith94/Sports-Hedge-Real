import {
  FixtureMarketInventoryRow,
  Venue,
  VenueMarketFacts,
  VenueQuoteFact,
} from "./api";
import { number, percent, percentPoints } from "./format";

export type InventoryPairResult = {
  left_venue: Venue;
  right_venue: Venue;
  entered_solver: boolean;
  solver_model?: string | null;
  current_net_edge?: string | number | null;
  rejection_reasons: string[];
  solver_is_arbitrage: boolean;
};

export type KalshiFixtureMarketInventoryRow = FixtureMarketInventoryRow & {
  kalshi?: VenueMarketFacts | null;
  pair_results?: InventoryPairResult[];
};

const COMPARISON_LABELS: Record<string, string> = {
  matched_equivalent: "Matched equivalent",
  venue_only: "Venue only",
  settlement_mismatch: "Settlement mismatch",
  unsupported_outcome_model: "Unsupported outcome model",
  unsupported_family: "Unsupported family",
  missing_costs: "Missing costs",
  missing_fx: "Missing FX",
  stale: "Stale",
  other: "Other",
};

const REASON_LABELS: Record<string, string> = {
  incomplete_outcome_set: "Not comparable — incomplete outcome set",
  settlement_mismatch: "Not comparable — settlement mismatch",
  incomplete_settlement: "Not comparable — incomplete settlement fingerprint",
  unknown_settlement_scope: "Not comparable — unknown settlement scope",
  venue_only: "Venue only — no equivalent market on the other side",
  unsupported_outcome_model: "Not comparable — unsupported outcome model",
  unsupported_family: "Not comparable — unsupported market family",
  market_not_equivalent: "Not comparable — markets are not economically equivalent",
  noncanonical_outcome_space: "Not comparable — non-canonical outcome space",
  missing_venue_cost: "Fail closed — required venue fee is missing",
  missing_costs: "Fail closed — required venue fee is missing",
  unknown_required_venue_cost: "Fail closed — required venue fee is unknown",
  unknown_costs: "Fail closed — required venue fee is unknown",
  missing_fx_rate: "Fail closed — required FX rate is missing",
  missing_fx: "Fail closed — required FX rate is missing",
  stale_quote: "Fail closed — stale quotes",
  unknown_quote_age: "Fail closed — quote age unknown",
  push_state_not_modelled: "Not comparable — push/void state is not modelled",
  unproven_settlement_semantics: "Not comparable — unproven settlement semantics",
  unproven_handicap_semantics: "Not comparable — unproven handicap semantics",
  generalized_split_line_not_modelled: "Not comparable — split line is not modelled",
  unknown_draw_void_semantics: "Not comparable — draw/void semantics unknown",
  unsupported_state_payoff_fee_basis: "Fail closed — unsupported state-payoff fee basis",
  no_positive_edge: "Evaluated — no positive net edge",
  no_arbitrage: "Evaluated — no arbitrage after costs and depth",
  missing_executable_outcome_depth: "Fail closed — missing executable outcome depth",
};

export const INVENTORY_STATUS_LABELS = [
  "matched_equivalent",
  "venue_only",
  "settlement_mismatch",
  "unsupported_outcome_model",
] as const;

export function coverageLabel(row: KalshiFixtureMarketInventoryRow): string {
  const venues: string[] = [];
  if (row.matchbook) venues.push("Matchbook");
  if (row.polymarket) venues.push("Polymarket");
  if (row.kalshi) venues.push("Kalshi");
  if (venues.length === 3) return "Matchbook + Polymarket + Kalshi";
  if (venues.length === 2) return venues.join(" + ");
  if (venues.length === 1) return `${venues[0]} only`;
  return "no venue payload";
}

export function comparisonLabel(status: string): string {
  return COMPARISON_LABELS[status] ?? humanizeToken(status);
}

export function humanizeToken(value: string | null | undefined): string {
  if (!value) return "";
  return value.replaceAll("_", " ");
}

export function reasonLabel(reason: string): string {
  const mapped = REASON_LABELS[reason];
  if (mapped) return mapped;
  const [code, ...rest] = reason.split(":");
  const mappedCode = REASON_LABELS[code];
  if (mappedCode && rest.length) {
    return `${mappedCode} (${rest.join(":")})`;
  }
  if (mappedCode) return mappedCode;
  return humanizeToken(reason);
}

export function quoteSummary(facts: VenueMarketFacts | null | undefined): string {
  if (!facts) return "—";
  const backs = facts.best_backs.filter((quote) => quote.decimal_odds != null);
  if (!backs.length) return "unknown price";
  const currency = venueCurrency(facts);
  return backs.map((quote) => formatQuote(quote, currency)).join(" · ");
}

export function economicsSummary(
  facts: VenueMarketFacts | null | undefined,
  options?: { limiting?: boolean },
): string {
  if (!facts) return "—";
  const fee = feeLabel(facts);
  const fx = fxLabel(facts.fx_status);
  const depth = depthLabel(facts, options?.limiting === true);
  return `${fee} · ${fx} · ${depth}`;
}

export function solverFacts(row: KalshiFixtureMarketInventoryRow): string {
  const model = row.solver_model ? humanizeToken(row.solver_model) : null;
  if (!row.entered_solver) {
    const reason = row.reason ? ` · ${reasonLabel(row.reason)}` : "";
    if (model) return `${model}${reason}`;
    return row.reason ? `not in solver · ${reasonLabel(row.reason)}` : "not in solver";
  }
  const prefix = model ? `${model} · ` : "";
  if (!row.solver_is_arbitrage && row.reason) {
    return `${prefix}evaluated · ${reasonLabel(row.reason)}`;
  }
  return `${prefix}net ${percent(row.current_net_edge)} · trigger ${percent(row.trigger_net_edge)} · ${percentPoints(row.distance_to_trigger_pp)}`;
}

export function settlementLabel(facts: VenueMarketFacts): string {
  return facts.settlement_complete
    ? "Settlement fingerprint complete"
    : "Settlement fingerprint incomplete/unknown";
}

export function pairSummary(pair: InventoryPairResult): string {
  const label = `${venueTitle(pair.left_venue)}↔${venueTitle(pair.right_venue)}`;
  if (pair.entered_solver) {
    return `${label} ${pair.solver_model ? humanizeToken(pair.solver_model) : "solver"}`;
  }
  const reason = pair.rejection_reasons[0];
  return `${label} ${reason ? reasonLabel(reason) : "not entered"}`;
}

export function mismatchExplanation(row: KalshiFixtureMarketInventoryRow): string | null {
  const reasons = collectReasons(row);
  if (!reasons.some((reason) => reason === "incomplete_outcome_set")) {
    return null;
  }
  const spaces = [row.matchbook, row.polymarket, row.kalshi]
    .filter((facts): facts is VenueMarketFacts => Boolean(facts))
    .map((facts) => ({
      venue: venueTitle(facts.venue),
      space: outcomeSpaceLabel(facts),
    }))
    .filter((item) => item.space);
  const unique = [...new Map(spaces.map((item) => [`${item.venue}:${item.space}`, item])).values()];
  const distinctSpaces = [...new Set(unique.map((item) => item.space))];
  if (distinctSpaces.length >= 2) {
    const parts = unique.map((item) => `${item.venue} ${item.space}`);
    return `Outcome sets differ (${parts.join(" vs ")}), so the markets are not complete-set comparable.`;
  }
  return "The listed outcomes do not form a complete comparable set, so the comparison stays fail-closed.";
}

export function limitingVenues(row: KalshiFixtureMarketInventoryRow): Set<Venue> {
  const facts = [row.matchbook, row.polymarket, row.kalshi].filter(
    (item): item is VenueMarketFacts => Boolean(item),
  );
  const depths = facts
    .map((item) => ({ venue: item.venue, depth: number(item.usable_depth_at_touch) }))
    .filter((item): item is { venue: Venue; depth: number } => item.depth !== null);
  if (depths.length < 2) return new Set();
  const min = Math.min(...depths.map((item) => item.depth));
  return new Set(depths.filter((item) => item.depth === min).map((item) => item.venue));
}

export function provenanceLines(facts: VenueMarketFacts): string[] {
  const lines = [`Market ID ${facts.source_market_id}`, `Event ID ${facts.source_event_id}`];
  if (facts.raw_market_name) lines.push(`Raw name ${facts.raw_market_name}`);
  if (facts.raw_market_type) lines.push(`Raw type ${facts.raw_market_type}`);
  if (facts.raw_runner_labels?.length) {
    lines.push(`Raw runners ${facts.raw_runner_labels.join(" / ")}`);
  }
  if (facts.settlement_key) lines.push(`Settlement key ${facts.settlement_key}`);
  if (facts.fee_source) lines.push(`Fee source ${facts.fee_source}`);
  if (facts.fee_label) lines.push(`Fee rule ${facts.fee_label}`);
  if (facts.fee_account_assumption) lines.push("Fee is an operator/account assumption");
  if (facts.observed_at) lines.push(`Observed ${facts.observed_at}`);
  if (facts.quote_age_ms != null) {
    const basis = facts.quote_age_basis ? ` (${facts.quote_age_basis})` : "";
    lines.push(`Quote age ${facts.quote_age_ms}ms${basis}`);
  }
  return lines;
}

function formatQuote(quote: VenueQuoteFact, currency: "GBP" | "USD"): string {
  // Operator format examples: YES · 1.47 · £4,317 available / YES · 2.27 · $4,317 available
  const odds = formatOdds(quote.decimal_odds);
  const outcome = formatOutcome(quote.outcome);
  if (quote.size_at_touch == null || quote.size_at_touch === "") {
    return `${outcome} · ${odds}`;
  }
  return `${outcome} · ${odds} · ${formatDepth(quote.size_at_touch, currency)} available`;
}

function formatOutcome(outcome: string): string {
  return outcome.replaceAll("_", " ").toUpperCase();
}

function formatOdds(value: string | number | null | undefined): string {
  const parsed = number(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(parsed);
}

function formatDepth(value: string | number | null | undefined, currency: "GBP" | "USD"): string {
  const parsed = number(value);
  if (parsed === null) return "—";
  const fractionDigits = Math.abs(parsed) >= 1 ? 0 : 2;
  const locale = currency === "USD" ? "en-US" : "en-GB";
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(parsed);
}

export function venueCurrency(facts: VenueMarketFacts): "GBP" | "USD" {
  const native = (facts.native_currency || "").toUpperCase();
  if (native === "USD") return "USD";
  if (native === "GBP") return "GBP";
  if (facts.venue === "polymarket" || facts.venue === "kalshi") return "USD";
  return "GBP";
}

function feeLabel(facts: VenueMarketFacts): string {
  if (facts.fee_label) return facts.fee_label;
  const status = facts.fee_status;
  if (!status) return "fee unknown";
  if (status === "known") return "fee known";
  if (status === "missing") return "fee missing";
  if (status === "unknown") return "fee unknown";
  return `fee ${humanizeToken(status)}`;
}

function fxLabel(status: string | null | undefined): string {
  if (!status) return "fx n/a";
  if (status === "not_required") return "fx not required";
  if (status === "known") return "fx known";
  if (status === "missing") return "fx missing";
  return `fx ${humanizeToken(status)}`;
}

function depthLabel(facts: VenueMarketFacts, limiting: boolean): string {
  if (facts.usable_depth_at_touch == null) return "depth unknown";
  const amount = formatDepth(facts.usable_depth_at_touch, venueCurrency(facts));
  return limiting ? `Limiting best-price depth ${amount}` : `Best-price depth ${amount}`;
}

function venueTitle(venue: Venue): string {
  if (venue === "matchbook") return "Matchbook";
  if (venue === "polymarket") return "Polymarket";
  if (venue === "kalshi") return "Kalshi";
  return venue;
}

function outcomeSpaceLabel(facts: VenueMarketFacts): string | null {
  const outcomes = facts.best_backs.map((quote) => quote.outcome.toLowerCase());
  if (!outcomes.length) return null;
  const set = new Set(outcomes);
  if (set.has("yes") && set.has("no")) return "YES/NO";
  if (set.has("home") && set.has("draw") && set.has("away")) return "HOME/DRAW/AWAY";
  if (set.has("over") && set.has("under")) return "OVER/UNDER";
  return [...set].map((item) => item.replaceAll("_", " ").toUpperCase()).join("/");
}

function collectReasons(row: KalshiFixtureMarketInventoryRow): string[] {
  return [
    row.reason,
    ...row.rejection_reasons,
    ...(row.pair_results ?? []).flatMap((pair) => pair.rejection_reasons),
  ].filter((reason): reason is string => Boolean(reason));
}
