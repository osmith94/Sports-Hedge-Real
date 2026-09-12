export type DataClass =
  | "LIVE"
  | "PERSISTED_PAPER"
  | "HISTORICAL"
  | "MODELLED"
  | "DEMO_FIXTURE"
  | "UNAVAILABLE";

export type QuoteVenue = "Matchbook" | "Smarkets" | "Polymarket";
export type QuoteSide = "BACK" | "LAY" | "BUY" | "SELL";
export type FeeBasis =
  | "PROFIT_COMMISSION"
  | "STAKE_OR_NOTIONAL"
  | "PAYOUT"
  | "TRANSACTION"
  | "FIXED"
  | "FORMULA"
  | "NONE_CONFIRMED"
  | "UNKNOWN";

export type ValueState =
  | "VALUE"
  | "NO_VALUE"
  | "INSUFFICIENT_EVIDENCE"
  | "STALE_QUOTE"
  | "INSUFFICIENT_LIQUIDITY"
  | "MISSING_COSTS"
  | "SEMANTICS_MISMATCH"
  | "NO_COMPARABLE_MARKET";

export type CapitalSource = "AUTO_POOL" | "MANUAL_OVERRIDE" | "MANUAL_EXTERNAL";

export type ArbClass = "NEAR_ARB" | "VALIDATED_ARB" | "PRIORITY_ALERT" | "MANUAL_EXTERNAL";

export type FeeSnapshot = {
  venue: QuoteVenue;
  side: QuoteSide;
  feeBasis: FeeBasis;
  rate: number | null;
  known: boolean;
  capturedAtLabel: string;
  provenance: string;
  assumption: true;
};

export type EffectiveQuote = {
  venue: QuoteVenue;
  side: QuoteSide;
  headlineDecimal: number;
  impliedHeadline: number;
  netDecimal: number | null;
  costAdjustedImplied: number | null;
  fee: FeeSnapshot;
  retrievedAtLabel: string;
  quoteAgeMinutes: number;
  role: "reference" | "available";
  settlementEquivalent: boolean;
  liquidityNote: string;
};
