export type VenueId = "matchbook" | "polymarket" | "kalshi" | "smarkets";
export type CurrencyCode = "GBP" | "USD";
export type AlertSeverity = "PRIORITY" | "HIGH_PRIORITY" | "CRITICAL";
export type FillConfidenceBand = "LOW" | "MEDIUM" | "HIGH";
export type AlertOperatorStatus = "OPEN" | "PREPARED" | "SNOOZED" | "DISMISSED" | "AWAITING_EXTERNAL_LEG_CONFIRMATION" | "EXTERNAL_LEG_CONFIRMED";
export type CapitalSource = "AUTO_POOL" | "MANUAL_OVERRIDE" | "MANUAL_EXTERNAL";
export type ExecutionPath = "PAPER_INTERNAL" | "MANUAL_EXTERNAL";

export type PriorityAlertLeg = {
  legId: string;
  venue: VenueId;
  currency: CurrencyCode;
  outcome: string;
  selectionLabel: string;
  decimalOdds: number;
  visibleDepthNative: number;
  visibleDepthGbp: number;
  quoteAgeMs: number;
  bookLevelsRequired: number;
  isLimiting: boolean;
  recommendedStakeNative: number;
  recommendedStakeGbp: number;
  capitalSource: CapitalSource;
  executionPath: ExecutionPath;
};

export type FillConfidenceFactor = {
  id: string;
  label: string;
  value: string;
  note: string;
  supportive: boolean;
};

export type AutoPoolSnapshot = {
  venue: VenueId;
  currency: CurrencyCode;
  autoPoolNative: number;
};

export type PriorityAlert = {
  alertId: string;
  opportunityId: string;
  severity: AlertSeverity;
  openedAt: string;
  event: {
    canonicalEventId: string;
    competition: string;
    homeTeam: string;
    awayTeam: string;
    kickoffUtc: string;
  };
  market: {
    canonicalMarketId: string;
    family: string;
    label: string;
    period: string;
    settlement: string;
    settlementKey: string;
  };
  netGuaranteedEdge: number;
  expectedProfitGbpAtRecommended: number;
  expectedRoiAtRecommended: number;
  recommendedSizeGbp: number;
  rawLimitingDepthGbp: number;
  safetyHaircut: number;
  maxValidatedSizeGbp: number;
  maxTheoreticalSizeGbp: number;
  limitingLegId: string;
  quoteAgeMs: number;
  fillConfidence: FillConfidenceBand;
  fillConfidenceScore: number;
  fillConfidenceFactors: FillConfidenceFactor[];
  executionRiskScore: number;
  executionRiskBand: string;
  legs: PriorityAlertLeg[];
  autoPools: AutoPoolSnapshot[];
  gbpPerUsd: number;
  whyExceptional: string;
  dataSource: "DEMO_FIXTURE";
  paperMode: true;
  executionPath: ExecutionPath;
  operatorLifecycle: AlertOperatorStatus;
};

export type ScaledLegCapital = {
  legId: string;
  venue: VenueId;
  currency: CurrencyCode;
  outcome: string;
  selectionLabel: string;
  decimalOdds: number;
  stakeNative: number;
  stakeGbp: number;
  autoPoolNative: number;
  autoPoolCoveredNative: number;
  manualOverrideNative: number;
  isLimiting: boolean;
  visibleDepthNative: number;
};

export type VenueCurrencyNeed = {
  venue: VenueId;
  currency: CurrencyCode;
  requiredNative: number;
  autoPoolNative: number;
  autoCoveredNative: number;
  additionalManualNative: number;
  capitalSource: CapitalSource;
};

export type ManualTicketQuote = {
  alertId: string;
  requestedSizeGbp: number;
  scale: number;
  withinValidatedMaximum: boolean;
  exceedsValidatedSize: boolean;
  limitingLegLabel: string;
  guaranteedReturnGbp: number;
  guaranteedProfitGbp: number;
  roi: number;
  totalCapitalGbp: number;
  legs: ScaledLegCapital[];
  capitalByVenueCurrency: VenueCurrencyNeed[];
  additionalManualGbp: number;
  additionalManualUsd: number;
};
