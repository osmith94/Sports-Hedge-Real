/**
 * Demo effective-economics helper.
 *
 * Fee snapshots are DEMO ASSUMPTIONS. Unknown costs never become zero.
 * Headline odds are never used as the ranking key.
 */

import type { EffectiveQuote, FeeSnapshot, ValueState } from "./types";

/** DEMO ASSUMPTION — not a live venue SLA. Quotes older than this cannot rank VALUE. */
export const DEMO_MAX_QUOTE_AGE_MINUTES = 10;

export const DEMO_QUOTE_FRESHNESS_ASSUMPTION =
  "DEMO ASSUMPTION: max quote age for VALUE is 10 minutes on the snapshot clock, not a live venue SLA.";

export function impliedFromDecimal(decimalPrice: number): number {
  return 1 / decimalPrice;
}

export function netBackOdds(headline: number, fee: FeeSnapshot): number | null {
  if (!fee.known || fee.feeBasis === "UNKNOWN" || fee.rate === null) return null;
  if (fee.feeBasis === "NONE_CONFIRMED") return headline;
  if (fee.feeBasis === "PROFIT_COMMISSION") {
    return 1 + (headline - 1) * (1 - fee.rate);
  }
  if (fee.feeBasis === "STAKE_OR_NOTIONAL") {
    return headline * (1 - fee.rate);
  }
  return null;
}

export function withEffectiveEconomics(quote: Omit<EffectiveQuote, "impliedHeadline" | "netDecimal" | "costAdjustedImplied">): EffectiveQuote {
  const net = netBackOdds(quote.headlineDecimal, quote.fee);
  return {
    ...quote,
    impliedHeadline: impliedFromDecimal(quote.headlineDecimal),
    netDecimal: net,
    costAdjustedImplied: net === null ? null : impliedFromDecimal(net),
  };
}

export function bestNetQuote(quotes: EffectiveQuote[]): EffectiveQuote | undefined {
  const usable = quotes.filter((quote) => quote.settlementEquivalent && quote.netDecimal !== null);
  if (usable.length === 0) return undefined;
  return [...usable].sort((a, b) => (b.netDecimal ?? 0) - (a.netDecimal ?? 0))[0];
}

export function referenceQuote(quotes: EffectiveQuote[]): EffectiveQuote | undefined {
  return quotes.find((quote) => quote.role === "reference") ?? quotes[0];
}

export function evPerPound(modelProbability: number, netDecimal: number): number {
  return modelProbability * netDecimal - 1;
}

export function probabilityEdgePp(modelProbability: number, marketProbability: number): number {
  return (modelProbability - marketProbability) * 100;
}

export function valueState(args: {
  quotes: EffectiveQuote[];
  modelProbability: number;
  sampleSize: number;
  confidence: "high" | "medium" | "low";
  minEv?: number;
}): ValueState {
  const comparable = args.quotes.filter((quote) => quote.settlementEquivalent);
  if (comparable.length === 0) return "NO_COMPARABLE_MARKET";
  if (comparable.some((quote) => !quote.fee.known || quote.fee.feeBasis === "UNKNOWN")) {
    const known = comparable.filter((quote) => quote.fee.known && quote.netDecimal !== null);
    if (known.length === 0) return "MISSING_COSTS";
  }
  if (args.sampleSize < 12 || args.confidence === "low") return "INSUFFICIENT_EVIDENCE";
  const best = bestNetQuote(args.quotes);
  if (!best || best.netDecimal === null || best.costAdjustedImplied === null) return "MISSING_COSTS";
  if (best.quoteAgeMinutes > DEMO_MAX_QUOTE_AGE_MINUTES) return "STALE_QUOTE";
  const ev = evPerPound(args.modelProbability, best.netDecimal);
  if (ev < (args.minEv ?? 0.02)) return "NO_VALUE";
  return "VALUE";
}

export function feeAssumption(partial: Omit<FeeSnapshot, "assumption">): FeeSnapshot {
  return { ...partial, assumption: true };
}
