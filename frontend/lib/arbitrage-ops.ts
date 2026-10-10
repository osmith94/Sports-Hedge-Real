import { PaperScanRecord, PaperScanSummary, Venue } from "./api";
import { number } from "./format";

export type DataProvenance = "LIVE_PAPER" | "DEMO_FIXTURE";

export type OpportunityStatus =
  | "WATCHING"
  | "APPROACHING"
  | "TRIGGERED"
  | "PAPER_FILLING"
  | "PARTIAL"
  | "FILLED"
  | "CLOSED"
  | "EXPIRED"
  | "REJECTED";

export type ActivityKind =
  | "WATCHLIST_ENTERED"
  | "THRESHOLD_CROSSED"
  | "PAPER_FILL_ATTEMPTED"
  | "PARTIAL_FILL"
  | "PAPER_POSITION_COMPLETED"
  | "SETTLED"
  | "VOIDED"
  | "CLOSED"
  | "REALISED_PNL"
  | "REJECTED";

export type ScannerAssumptions = {
  paperMode: true;
  minimumNetArb: number;
  capitalLimitGbp: number | null;
  maximumExecutionRisk: number;
  feeSource: string;
  fxSource: string;
};

export type ArbitrageOpportunity = {
  id: string;
  provenance: DataProvenance;
  canonicalEventId?: string | null;
  eventLabel: string;
  competition?: string | null;
  marketLabel: string;
  settlement: string;
  venues: Venue[] | string[];
  netArb: number | null;
  grossArb: number | null;
  trigger: number;
  distanceToTriggerPp: number | null;
  movement: "up" | "down" | "flat" | null;
  capitalRequiredGbp: number | null;
  expectedLock: string | null;
  quoteFreshness: string | null;
  executableDepth: string | null;
  limitingLeg: string | null;
  riskFlags: string[];
  currencies: string[];
  status: OpportunityStatus;
  executable: boolean;
  scannedAt?: string | null;
  guaranteedProfitGbp: number | null;
  executionRisk?: string | null;
  discoverySource?: string | null;
  fixtureStatus?: string | null;
  inRunning?: boolean | null;
  liveScoreLabel?: string | null;
    strikeNarrative?: string | null;
    observationCount?: number | null;
    betActionable?: boolean;
    betBlockedReason?: string | null;
};

export type ActivityPrice2Leg = {
  venue: string | null;
  outcome: string | null;
  displayedOdds: string | null;
  requestedStake: string | null;
  stakeCurrency: string | null;
  retrievedAt: string | null;
  quoteAgeMs: number | null;
  slotWaitMs: number | null;
  ioMs: number | null;
  timingMatch: "native_id" | null;
  nativeMarketId: string | null;
  nativeRunnerId: string | null;
  nativeFrozen: boolean | null;
  freezeStatus: "frozen" | "not_frozen" | "details_not_recorded" | null;
  freezeReason: string | null;
  observedTickSize: string | null;
  observedMinimumShares: string | null;
  intendedNativeStake: string | null;
  intendedNativeShares: string | null;
  intendedLimitPrice: string | null;
};

export type ActivityPrice2VenueTiming = {
  venue: string;
  slotWaitMs: number | null;
  ioMs: number | null;
  callCount: number;
};

export type ActivityPrice2 = {
  snapshotId: string | null;
  executionCycle: number | null;
  cycleOutcome: string | null;
  tradeId: string | null;
  status: "accepted" | "rejected" | "incomplete_unavailable";
  filled: boolean;
  tradeLinked: boolean;
  startedAt: string | null;
  finishedAt: string | null;
  elapsedMs: number | null;
  netEdge: string | number | null;
  guaranteedProfit: string | number | null;
  executionSize: string | number | null;
  executionSizeCurrency: string | null;
  oldestQuoteAgeMs: number | null;
  skewMs: number | null;
  rejectionReason: string | null;
  minimumNetEdge: string | number | null;
  economicsVsThreshold:
    | "below_configured_threshold"
    | "meets_or_exceeds_configured_threshold"
    | "threshold_not_recorded"
    | "net_edge_not_recorded"
    | null;
  nativeOrderFreezeRecorded: boolean;
  source: "execution_snapshot_audit" | "lifecycle_rejection";
  dataKind: "historical_recorded";
  legs: ActivityPrice2Leg[];
  venueTimings: ActivityPrice2VenueTiming[];
};

export type ActivityEvent = {
  id: string;
  provenance: DataProvenance;
  at: string;
  kind: string;
  title: string;
  subject?: string | null;
  detail: string;
  opportunityId?: string;
  eventType?: string;
  missedTriggerEventId?: string | null;
  fixtureLabel?: string | null;
  marketFamily?: string | null;
  canonicalEventId?: string | null;
  canonicalMarketId?: string | null;
  attemptId?: string | null;
  price2?: ActivityPrice2 | null;
};

export type CapitalSnapshot = {
  provenance: DataProvenance;
  realisedPnlGbp: number | null;
  lockedCapitalGbp: number | null;
  availableCapitalGbp: number | null;
  todayPnlGbp: number | null;
  mtdPnlGbp: number | null;
  allTimePnlGbp: number | null;
  note: string;
};

export type LiquidityPool = {
  provenance: DataProvenance;
  venue: string;
  nativeCurrency: "USD" | "GBP";
  available: number | null;
  locked: number | null;
  transit: number | null;
  gbpCarryingValue: number | null;
  supported: boolean;
};

export const DEFAULT_SCANNER_ASSUMPTIONS: ScannerAssumptions = {
  paperMode: true,
  minimumNetArb: 0.01,
  capitalLimitGbp: null,
  maximumExecutionRisk: 60,
  feeSource: "explicit dashboard input or fail-closed missing-fee rejection",
  fxSource: "explicit USD→GBP input or fail-closed missing-FX rejection",
};

export const DEMO_NEAR_ARB: ArbitrageOpportunity[] = [
  {
    id: "demo-near-1",
    provenance: "DEMO_FIXTURE",
    eventLabel: "Brighton v Fulham",
    competition: "Premier League",
    marketLabel: "Match result",
    settlement: "regulation time · 1X2",
    venues: ["matchbook", "polymarket"],
    netArb: 0.008,
    grossArb: 0.009,
    trigger: 0.01,
    distanceToTriggerPp: 0.2,
    movement: "up",
    capitalRequiredGbp: 740,
    expectedLock: "until FT + settlement (~2h 40m)",
    quoteFreshness: "1.4s · both legs · at last evaluation",
    executableDepth: "£740 limited by Polymarket home",
    limitingLeg: "Polymarket · Home",
    riskFlags: ["quote_age_ok", "mapping 99.1%"],
    currencies: ["GBP", "USD"],
    status: "WATCHING",
    executable: false,
    scannedAt: "2026-04-12T14:02:11Z",
    guaranteedProfitGbp: null,
    executionRisk: "28 · low",
  },
  {
    id: "demo-near-2",
    provenance: "DEMO_FIXTURE",
    eventLabel: "Roma v Lazio",
    competition: "Serie A",
    marketLabel: "Both teams to score",
    settlement: "regulation time · yes/no",
    venues: ["matchbook", "polymarket"],
    netArb: 0.0071,
    grossArb: 0.008,
    trigger: 0.01,
    distanceToTriggerPp: 0.29,
    movement: "flat",
    capitalRequiredGbp: 410,
    expectedLock: "until FT (~3h 05m)",
    quoteFreshness: "2.8s · Matchbook lagging · at last evaluation",
    executableDepth: "£410 limited by Matchbook Yes",
    limitingLeg: "Matchbook · Yes",
    riskFlags: ["thin_away_depth"],
    currencies: ["GBP", "USD"],
    status: "WATCHING",
    executable: false,
    scannedAt: "2026-04-12T14:01:44Z",
    guaranteedProfitGbp: null,
    executionRisk: "41 · medium",
  },
];

export const DEMO_EXECUTABLE: ArbitrageOpportunity[] = [
  {
    id: "demo-exec-1",
    provenance: "DEMO_FIXTURE",
    eventLabel: "Celtic v Rangers",
    competition: "Scottish Premiership",
    marketLabel: "Draw no bet",
    settlement: "regulation time · two-way",
    venues: ["matchbook", "polymarket"],
    netArb: 0.0124,
    grossArb: 0.014,
    trigger: 0.01,
    distanceToTriggerPp: 0,
    movement: "up",
    capitalRequiredGbp: 520,
    expectedLock: "until FT (~1h 50m)",
    quoteFreshness: "0.9s · both legs · at last evaluation",
    executableDepth: "£520 · 62% of quoted size",
    limitingLeg: "Polymarket · Away",
    riskFlags: [],
    currencies: ["GBP", "USD"],
    status: "TRIGGERED",
    executable: true,
    scannedAt: "2026-04-12T14:03:02Z",
    guaranteedProfitGbp: 6.45,
    executionRisk: "22 · low",
  },
];

export const DEMO_ACTIVITY: ActivityEvent[] = [
  {
    id: "demo-act-1",
    provenance: "DEMO_FIXTURE",
    at: "2026-04-12T14:03:08Z",
    kind: "PAPER_FILL_ATTEMPTED",
    title: "Paper fill attempted",
    detail: "Celtic v Rangers · draw no bet · both legs quoted",
  },
  {
    id: "demo-act-2",
    provenance: "DEMO_FIXTURE",
    at: "2026-04-12T13:41:22Z",
    kind: "PARTIAL_FILL",
    title: "Partial paper fill",
    detail: "Limiting leg 71% filled · remaining size expired",
  },
  {
    id: "demo-act-3",
    provenance: "DEMO_FIXTURE",
    at: "2026-04-12T12:18:09Z",
    kind: "REALISED_PNL",
    title: "Settled · realised paper P&L",
    detail: "Aston Villa v Wolves · +£4.10 · closed",
  },
];

export const DEMO_CAPITAL: CapitalSnapshot = {
  provenance: "DEMO_FIXTURE",
  realisedPnlGbp: 18.4,
  lockedCapitalGbp: 520,
  availableCapitalGbp: 14480,
  todayPnlGbp: 4.1,
  mtdPnlGbp: 18.4,
  allTimePnlGbp: 18.4,
  note: "No treasury/P&L ledger is persisted yet. Figures are labelled DEMO/FIXTURE for layout review.",
};

export const DEMO_LIQUIDITY_POOLS: LiquidityPool[] = [
  {
    provenance: "DEMO_FIXTURE",
    venue: "Polymarket",
    nativeCurrency: "USD",
    available: 8200,
    locked: 410,
    transit: 0,
    gbpCarryingValue: 6457.5,
    supported: true,
  },
  {
    provenance: "DEMO_FIXTURE",
    venue: "Matchbook",
    nativeCurrency: "GBP",
    available: 6400,
    locked: 310,
    transit: 80,
    gbpCarryingValue: 6790,
    supported: true,
  },
  {
    provenance: "DEMO_FIXTURE",
    venue: "Smarkets",
    nativeCurrency: "GBP",
    available: 2100,
    locked: 0,
    transit: 40,
    gbpCarryingValue: 2140,
    supported: true,
  },
];

export const DEMO_MANUAL_EXTERNAL: ArbitrageOpportunity = {
  id: "demo-external-1",
  provenance: "DEMO_FIXTURE",
  eventLabel: "Arsenal v Fulham",
  competition: "Premier League",
  marketLabel: "Match result",
  settlement: "regulation time · 1X2 · Polymarket leg is external-manual",
  venues: ["matchbook", "polymarket"],
  netArb: 0.018,
  grossArb: 0.021,
  trigger: 0.01,
  distanceToTriggerPp: 0,
  movement: "up",
  capitalRequiredGbp: 380,
    expectedLock: "awaiting external confirmation",
    quoteFreshness: "1.1s Matchbook · Polymarket not auto-executable · at last evaluation",
    executableDepth: "Matchbook hedge held · Polymarket not drawn from AUTO_POOL",
  limitingLeg: "Polymarket · Away (MANUAL_EXTERNAL)",
  riskFlags: ["AWAITING_EXTERNAL_LEG_CONFIRMATION", "MANUAL_EXTERNAL"],
  currencies: ["GBP", "USD"],
  status: "WATCHING",
  executable: false,
  scannedAt: "2026-09-12T10:28:00Z",
  guaranteedProfitGbp: null,
  executionRisk: "external confirmation required",
};

export function marketLabel(item: PaperScanRecord): string {
  const family = item.market_family.replaceAll("_", " ");
  const line = item.line === null || item.line === undefined ? "" : ` ${item.line}`;
  return `${family}${line}`;
}

function latestByMarket(scans: PaperScanRecord[]): PaperScanRecord[] {
  const seen = new Set<string>();
  const latest: PaperScanRecord[] = [];
  for (const scan of scans) {
    if (seen.has(scan.canonical_market_id)) continue;
    seen.add(scan.canonical_market_id);
    latest.push(scan);
  }
  return latest;
}

export function buildCapitalSnapshot(
  scans: PaperScanRecord[],
  summary: PaperScanSummary | null,
  assumptions: ScannerAssumptions,
): { live: CapitalSnapshot; fixture: CapitalSnapshot } {
  const eligible = latestByMarket(scans).filter((item) => item.eligible_for_paper_simulation);
  const locked = eligible.reduce((sum, item) => sum + (number(item.executable_stake_gbp) ?? 0), 0);
  const live: CapitalSnapshot = {
    provenance: "LIVE_PAPER",
    realisedPnlGbp: null,
    lockedCapitalGbp: eligible.length ? locked : null,
    availableCapitalGbp:
      assumptions.capitalLimitGbp !== null && eligible.length
        ? Math.max(assumptions.capitalLimitGbp - locked, 0)
        : assumptions.capitalLimitGbp,
    todayPnlGbp: null,
    mtdPnlGbp: null,
    allTimePnlGbp: null,
    note: summary
      ? `${summary.eligible_count} paper-eligible scan${summary.eligible_count === 1 ? "" : "s"} in the current window.`
      : "Paper P&L comes from closed trades when recorded.",
  };
  return { live, fixture: DEMO_CAPITAL };
}
