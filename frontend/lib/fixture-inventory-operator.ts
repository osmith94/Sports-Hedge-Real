import { PreparablePaperOpportunity, Venue, VenueMarketFacts } from "./api";
import { number, percent } from "./format";
import {
  KalshiFixtureMarketInventoryRow,
  InventoryPairResult,
  compactQuoteLines,
  compactVenueMeta,
  failingVenueChecks,
  humanizeToken,
  limitingVenues,
  outcomeSpaceLabel,
  reasonLabel,
  sentenceCase,
  venueKindLabel,
  venueShortLabel,
  venueTitle,
} from "./fixture-inventory-display";

export const PAPER_DEPLOYMENT_ANCHOR = "paper-deployment";
export const PAPER_DEPLOYMENT_HREF = `#${PAPER_DEPLOYMENT_ANCHOR}`;

export type DecisionTone = "eligible" | "caution" | "rejected" | "neutral";

export type OperatorDecisionStatus =
  | "paper_eligible"
  | "near_trigger"
  | "partially_comparable"
  | "venue_only"
  | "rejected";

export type OperatorDecision = {
  status: OperatorDecisionStatus;
  label: string;
  tone: DecisionTone;
};

export type PairBadge = {
  text: string;
  tone: DecisionTone;
  comparable: boolean;
};

export type VenueMiniCard = {
  venue: Venue;
  name: string;
  kind: string;
  present: boolean;
  quotes: string[];
  meta: string | null;
  incompatibility: string | null;
  failingChecks: string[];
};

export type PaperAction = {
  eligible: boolean;
  label: string;
  href: string | null;
};

export type InventoryCardView = {
  eyebrow: string;
  title: string;
  decision: OperatorDecision;
  economics: { text: string; tone: DecisionTone } | null;
  comparableHeadline: string | null;
  pairBadges: PairBadge[];
  discoveredNotes: string[];
  venues: VenueMiniCard[];
  operatorReason: string | null;
  paperAction: PaperAction;
};

const ACTION_GATE_REASONS = new Set([
  "stale_quote",
  "unknown_quote_age",
  "missing_venue_cost",
  "missing_costs",
  "unknown_required_venue_cost",
  "unknown_costs",
  "missing_fx_rate",
  "missing_fx",
  "missing_executable_outcome_depth",
  "unsupported_state_payoff_fee_basis",
]);

const MAPPING_INCOMPATIBLE = new Set([
  "incomplete_outcome_set",
  "settlement_mismatch",
  "incomplete_settlement",
  "unknown_settlement_scope",
  "unsupported_outcome_model",
  "unsupported_family",
  "market_not_equivalent",
  "noncanonical_outcome_space",
  "push_state_not_modelled",
  "unproven_settlement_semantics",
  "unproven_handicap_semantics",
  "generalized_split_line_not_modelled",
  "unknown_draw_void_semantics",
]);

const VENUE_ORDER: Venue[] = ["matchbook", "polymarket", "kalshi"];

export function toneClass(tone: DecisionTone): string {
  if (tone === "eligible") return "is-eligible";
  if (tone === "caution") return "is-caution";
  if (tone === "rejected") return "is-rejected";
  return "is-neutral";
}

export function decisionBadgeClass(tone: DecisionTone): string {
  if (tone === "eligible") return "ops-status is-hot";
  if (tone === "caution") return "ops-status is-watch";
  if (tone === "rejected") return "ops-status is-reject";
  return "ops-status";
}

export function inventoryCardViewModel(
  row: KalshiFixtureMarketInventoryRow,
  preparable: PreparablePaperOpportunity[] = [],
): InventoryCardView {
  const pairs = classifyPairs(row);
  const decision = operatorDecision(row, pairs);
  const pairBadges =
    decision.status === "paper_eligible"
      ? pairs.badges
      : pairs.badges.map((badge) =>
          badge.comparable ? { ...badge, tone: "neutral" as const } : badge,
        );
  return {
    eyebrow: "Market Comparison",
    title: marketTitle(row),
    decision,
    economics: compactEconomics(row, decision),
    comparableHeadline: pairs.comparableHeadline,
    pairBadges,
    discoveredNotes: pairs.discoveredNotes,
    venues: venueMiniCards(row, pairs),
    operatorReason: primaryOperatorReason(row, decision, pairs),
    paperAction: paperActionForRow(row, preparable, decision),
  };
}

export function operatorDecision(
  row: KalshiFixtureMarketInventoryRow,
  pairs = classifyPairs(row),
): OperatorDecision {
  if (row.solver_is_arbitrage) {
    if (row.radar_freshness === "radar_current" || row.radar_freshness === "expired") {
      return { status: "near_trigger", label: "Radar current", tone: "caution" };
    }
    return { status: "paper_eligible", label: "Paper eligible", tone: "eligible" };
  }
  if (hasSolverDecision(row)) {
    if (isNearTrigger(row)) {
      return { status: "near_trigger", label: "Near trigger", tone: "caution" };
    }
    return { status: "rejected", label: "Rejected", tone: "rejected" };
  }
  const present = presentVenues(row);
  if (present.length <= 1 || row.comparison_status === "venue_only") {
    return { status: "venue_only", label: "Venue only", tone: "neutral" };
  }
  if (pairs.comparable.length && pairs.leftoverVenues.length) {
    return { status: "partially_comparable", label: "Partially comparable", tone: "caution" };
  }
  return { status: "rejected", label: "Rejected", tone: "rejected" };
}

export function paperActionForRow(
  row: KalshiFixtureMarketInventoryRow,
  preparable: PreparablePaperOpportunity[] = [],
  decision = operatorDecision(row),
): PaperAction {
  const matches = matchingPreparable(row, preparable);
  if (decision.status === "paper_eligible" && matches.length) {
    return {
      eligible: true,
      label: "Review paper deployment →",
      href: PAPER_DEPLOYMENT_HREF,
    };
  }
  return { eligible: false, label: "Not eligible for deployment", href: null };
}

export function classifyPairs(row: KalshiFixtureMarketInventoryRow): {
  comparable: InventoryPairResult[];
  incompatible: InventoryPairResult[];
  leftoverVenues: Venue[];
  comparableKind: string | null;
  comparableHeadline: string | null;
  badges: PairBadge[];
  discoveredNotes: string[];
} {
  const present = presentVenues(row);
  const pairs = row.pair_results ?? [];
  const comparable = pairs.filter(pairIsComparable);
  const incompatible = pairs.filter((pair) => !pairIsComparable(pair));
  const comparableVenues = new Set(comparable.flatMap((pair) => [pair.left_venue, pair.right_venue]));
  const leftoverVenues = comparable.length
    ? present.filter((venue) => !comparableVenues.has(venue))
    : [];
  const comparableKind = comparableKindFrom(row, comparableVenues);

  if (!pairs.length && present.length >= 2) {
    const mappingFailed = collectReasonCodes(row).some((reason) => MAPPING_INCOMPATIBLE.has(reason));
    if (row.comparison_status === "matched_equivalent" && !mappingFailed) {
      return {
        comparable: [],
        incompatible: [],
        leftoverVenues: [],
        comparableKind,
        comparableHeadline: `Comparable: ${present.map(venueTitle).join(" ↔ ")}`,
        badges: [
          {
            text: `${present.map(venueShortLabel).join(" ↔ ")} · equivalent`,
            tone: "eligible",
            comparable: true,
          },
        ],
        discoveredNotes: [],
      };
    }
  }

  const badges: PairBadge[] = comparable.map((pair) => ({
    text: `${venueShortLabel(pair.left_venue)} ↔ ${venueShortLabel(pair.right_venue)} · equivalent`,
    tone: "eligible",
    comparable: true,
  }));
  for (const venue of leftoverVenues) {
    badges.push({
      text: `${venueShortLabel(venue)} · incompatible outcome set`,
      tone: "rejected",
      comparable: false,
    });
  }
  if (!comparable.length) {
    for (const pair of incompatible) {
      badges.push({
        text: `${venueShortLabel(pair.left_venue)} ↔ ${venueShortLabel(pair.right_venue)} · ${incompatiblePairNote(pair)}`,
        tone: "rejected",
        comparable: false,
      });
    }
  }

  const comparableHeadline =
    comparable.length === 1
      ? `Comparable: ${venueTitle(comparable[0].left_venue)} ↔ ${venueTitle(comparable[0].right_venue)}`
      : comparable.length > 1
        ? `Comparable: ${comparable.map((pair) => `${venueTitle(pair.left_venue)} ↔ ${venueTitle(pair.right_venue)}`).join(" · ")}`
        : null;

  const discoveredNotes = leftoverVenues.map((venue) => leftoverNote(row, venue, comparableKind));

  return {
    comparable,
    incompatible,
    leftoverVenues,
    comparableKind,
    comparableHeadline,
    badges,
    discoveredNotes,
  };
}

function marketTitle(row: KalshiFixtureMarketInventoryRow): string {
  const period = row.period ? sentenceCase(row.period) : "";
  const parts = [row.display_name];
  if (period && !row.display_name.toLowerCase().includes(humanizeToken(row.period).toLowerCase())) {
    parts.push(period);
  }
  return parts.join(" · ");
}

function compactEconomics(
  row: KalshiFixtureMarketInventoryRow,
  decision: OperatorDecision,
): { text: string; tone: DecisionTone } | null {
  const hasEconomics =
    row.entered_solver || row.current_net_edge != null || row.trigger_net_edge != null;
  if (!hasEconomics) return null;
  const parts: string[] = [];
  if (row.current_net_edge != null) parts.push(`Net ${percent(row.current_net_edge)}`);
  if (row.trigger_net_edge != null) parts.push(`Trigger ${percent(row.trigger_net_edge)}`);
  parts.push(decision.label);
  const net = number(row.current_net_edge);
  let tone: DecisionTone = decision.tone;
  if (decision.status !== "paper_eligible") {
    tone = net !== null && net < 0 ? "rejected" : decision.tone === "eligible" ? "caution" : decision.tone;
  }
  return { text: parts.join(" | "), tone };
}

function primaryOperatorReason(
  row: KalshiFixtureMarketInventoryRow,
  decision: OperatorDecision,
  pairs: ReturnType<typeof classifyPairs>,
): string | null {
  if (decision.status === "paper_eligible") return null;
  if (pairs.discoveredNotes.length) return null;

  const codes = collectReasonCodes(row);
  if (codes.includes("missing_fx") || row.comparison_status === "missing_fx") {
    return reasonLabel("missing_fx");
  }
  if (codes.includes("missing_costs") || codes.includes("missing_venue_cost") || row.comparison_status === "missing_costs") {
    return reasonLabel("missing_costs");
  }
  if (codes.includes("stale_quote") || row.comparison_status === "stale") {
    return reasonLabel("stale_quote");
  }
  if (decision.status === "near_trigger") {
    return "Below trigger — watch, not paper eligible.";
  }
  if (decision.status === "venue_only") {
    return reasonLabel("venue_only");
  }
  const primary = row.reason || row.rejection_reasons[0];
  if (primary) return reasonLabel(primary);
  if (row.entered_solver && !row.solver_is_arbitrage) {
    return reasonLabel("no_arbitrage");
  }
  return null;
}

function venueMiniCards(
  row: KalshiFixtureMarketInventoryRow,
  pairs: ReturnType<typeof classifyPairs>,
): VenueMiniCard[] {
  const limiting = limitingVenues(row);
  return VENUE_ORDER.map((venue) => {
    const facts = factsFor(row, venue);
    if (!facts) {
      return {
        venue,
        name: venueTitle(venue).toUpperCase(),
        kind: "",
        present: false,
        quotes: [],
        meta: null,
        incompatibility: null,
        failingChecks: [],
      };
    }
    const leftover = pairs.leftoverVenues.includes(venue);
    const kind = venueKindLabel(facts);
    const incompatibility =
      leftover && pairs.comparableKind
        ? `Not comparable with ${pairs.comparableKind}`
        : leftover
          ? "Not comparable"
          : null;
    const failing = failingVenueChecks(facts).filter((check) => {
      if (check.startsWith("Fee ") && facts.fee_status !== "missing" && facts.fee_status !== "unknown") {
        return false;
      }
      return true;
    });
    return {
      venue,
      name: venueTitle(venue).toUpperCase(),
      kind,
      present: true,
      quotes: compactQuoteLines(facts),
      meta: compactVenueMeta(facts, { limiting: limiting.has(venue) }),
      incompatibility,
      failingChecks: leftover ? failing.filter((check) => !check.includes("Settlement")) : failing,
    };
  });
}

function pairIsComparable(pair: InventoryPairResult): boolean {
  if (pair.entered_solver) return true;
  if (!pair.rejection_reasons.length) return true;
  return !pair.rejection_reasons.some((reason) => MAPPING_INCOMPATIBLE.has(reasonCode(reason)));
}

function leftoverNote(
  row: KalshiFixtureMarketInventoryRow,
  venue: Venue,
  comparableKind: string | null,
): string {
  const facts = factsFor(row, venue);
  const space = facts ? outcomeSpaceLabel(facts) : null;
  const binary = space === "YES/NO" || kindLooksBinary(facts);
  if (binary) {
    return `${venueTitle(venue)} not comparable — binary market`;
  }
  if (space && comparableKind) {
    return `Also discovered: ${venueTitle(venue)} — ${space}, not comparable with ${comparableKind}`;
  }
  return `Also discovered: ${venueTitle(venue)} — not comparable`;
}

function kindLooksBinary(facts: VenueMarketFacts | null | undefined): boolean {
  return venueKindLabel(facts) === "Binary";
}

function comparableKindFrom(row: KalshiFixtureMarketInventoryRow, comparableVenues: Set<Venue>): string | null {
  for (const venue of comparableVenues) {
    const facts = factsFor(row, venue);
    const kind = venueKindLabel(facts);
    if (kind) return kind;
  }
  const any = presentFacts(row)[0];
  return venueKindLabel(any);
}

function hasSolverDecision(row: KalshiFixtureMarketInventoryRow): boolean {
  return row.entered_solver || row.current_net_edge != null;
}

function isNearTrigger(row: KalshiFixtureMarketInventoryRow): boolean {
  if (!row.entered_solver || row.solver_is_arbitrage) return false;
  if (hasActionGateRejection(row)) return false;
  const net = number(row.current_net_edge);
  const distance = number(row.distance_to_trigger_pp);
  return net !== null && net >= 0 && distance !== null && distance > 0;
}

function hasActionGateRejection(row: KalshiFixtureMarketInventoryRow): boolean {
  return collectReasonCodes(row).some((code) => ACTION_GATE_REASONS.has(code));
}

function matchingPreparable(
  row: KalshiFixtureMarketInventoryRow,
  preparable: PreparablePaperOpportunity[],
): PreparablePaperOpportunity[] {
  return preparable.filter((item) => {
    if (!item.eligible_for_paper_simulation || !item.settlement_equivalent) return false;
    if (item.bet_actionable === false) return false;
    if (item.solver_model && row.solver_model && item.solver_model !== row.solver_model) return false;
    return true;
  });
}

function presentVenues(row: KalshiFixtureMarketInventoryRow): Venue[] {
  return VENUE_ORDER.filter((venue) => Boolean(factsFor(row, venue)));
}

function presentFacts(row: KalshiFixtureMarketInventoryRow): VenueMarketFacts[] {
  return presentVenues(row)
    .map((venue) => factsFor(row, venue))
    .filter((facts): facts is VenueMarketFacts => Boolean(facts));
}

function factsFor(row: KalshiFixtureMarketInventoryRow, venue: Venue): VenueMarketFacts | null | undefined {
  if (venue === "matchbook") return row.matchbook;
  if (venue === "polymarket") return row.polymarket;
  if (venue === "kalshi") return row.kalshi;
  return null;
}

function collectReasonCodes(row: KalshiFixtureMarketInventoryRow): string[] {
  return [
    row.reason,
    ...row.rejection_reasons,
    ...(row.pair_results ?? []).flatMap((pair) => pair.rejection_reasons),
  ]
    .filter((reason): reason is string => Boolean(reason))
    .map(reasonCode);
}

function reasonCode(reason: string): string {
  return reason.split(":")[0];
}

function incompatiblePairNote(pair: InventoryPairResult): string {
  const code = pair.rejection_reasons[0] ? reasonCode(pair.rejection_reasons[0]) : "";
  if (code === "incomplete_outcome_set") return "incompatible outcome set";
  if (code) return humanizeToken(code);
  return "incompatible";
}
