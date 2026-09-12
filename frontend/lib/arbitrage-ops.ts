import { PaperScanRecord, PaperScanSummary, Venue } from "./api";
import { number } from "./format";

export type DataProvenance = "LIVE_PAPER" | "DEMO_FIXTURE";

export type OpportunityStatus =
  | "WATCHING"
  | "TRIGGERED"
  | "PAPER_FILLING"
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
  eventLabel: string;
  competition?: string | null;
  marketLabel: string;
  settlement: string;
  venues: Venue[] | string[];
  netArb: number | null;
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
};

export type ActivityEvent = {
  id: string;
  provenance: DataProvenance;
  at: string;
  kind: ActivityKind;
  title: string;
  detail: string;
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
    trigger: 0.01,
    distanceToTriggerPp: 0.2,
    movement: "up",
    capitalRequiredGbp: 740,
    expectedLock: "until FT + settlement (~2h 40m)",
    quoteFreshness: "1.4s · both legs",
    executableDepth: "£740 limited by Polymarket home",
    limitingLeg: "Polymarket · Home",
    riskFlags: ["quote_age_ok", "mapping 99.1%"],
    currencies: ["GBP", "USD"],
    status: "WATCHING",
    executable: false,
    scannedAt: "2026-04-12T14:02:11Z",
    guaranteedProfitGbp: 5.92,
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
    trigger: 0.01,
    distanceToTriggerPp: 0.29,
    movement: "flat",
    capitalRequiredGbp: 410,
    expectedLock: "until FT (~3h 05m)",
    quoteFreshness: "2.8s · Matchbook lagging",
    executableDepth: "£410 limited by Matchbook Yes",
    limitingLeg: "Matchbook · Yes",
    riskFlags: ["thin_away_depth"],
    currencies: ["GBP", "USD"],
    status: "WATCHING",
    executable: false,
    scannedAt: "2026-04-12T14:01:44Z",
    guaranteedProfitGbp: 2.91,
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
    trigger: 0.01,
    distanceToTriggerPp: 0,
    movement: "up",
    capitalRequiredGbp: 520,
    expectedLock: "until FT (~1h 50m)",
    quoteFreshness: "0.9s · both legs",
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
  trigger: 0.01,
  distanceToTriggerPp: 0,
  movement: "up",
  capitalRequiredGbp: 380,
  expectedLock: "awaiting external confirmation",
  quoteFreshness: "1.1s Matchbook · Polymarket not auto-executable",
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

export function settlementLabel(item: PaperScanRecord): string {
  const period = item.period.replaceAll("_", " ");
  return `${period} · canonical ${item.canonical_market_id.slice(0, 12)}`;
}

function triggerFromDecision(item: PaperScanRecord, fallback: number): number {
  if (!item.decision_json) return fallback;
  try {
    const parsed = JSON.parse(item.decision_json) as { minimum_net_edge?: string | number };
    const value = number(parsed.minimum_net_edge);
    return value === null ? fallback : value;
  } catch {
    return fallback;
  }
}

function currenciesFromDecision(item: PaperScanRecord): string[] {
  if (!item.decision_json) return item.venues.includes("polymarket") ? ["GBP", "USD"] : ["GBP"];
  try {
    const parsed = JSON.parse(item.decision_json) as {
      fx_snapshots?: Array<{ currency?: string }>;
    };
    const currencies = (parsed.fx_snapshots ?? [])
      .map((snapshot) => snapshot.currency)
      .filter((value): value is string => Boolean(value));
    return currencies.length ? Array.from(new Set(currencies)) : ["GBP"];
  } catch {
    return ["GBP"];
  }
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

const NEAR_ARB_ALLOWED_REJECTIONS = new Set(["net_edge_below_threshold"]);

function isHardGateReason(reason: string): boolean {
  return !NEAR_ARB_ALLOWED_REJECTIONS.has(reason);
}

export function isNearArbCandidate(item: PaperScanRecord, trigger: number): boolean {
  if (item.eligible_for_paper_simulation) return false;
  const net = number(item.net_edge);
  if (net === null || net >= trigger) return false;
  const hard = item.rejection_reasons.filter(isHardGateReason);
  if (hard.length) return false;
  return item.rejection_reasons.includes("net_edge_below_threshold") || item.rejection_reasons.length === 0;
}

function statusForScan(item: PaperScanRecord, net: number | null, trigger: number): OpportunityStatus {
  if (item.eligible_for_paper_simulation) return "TRIGGERED";
  if (item.rejection_reasons.some(isHardGateReason)) return "REJECTED";
  if (net !== null && net < trigger) return "WATCHING";
  if (item.rejection_reasons.length) return "REJECTED";
  return "WATCHING";
}

export function opportunityFromScan(
  item: PaperScanRecord,
  assumptions: ScannerAssumptions,
): ArbitrageOpportunity {
  const net = number(item.net_edge);
  const trigger = triggerFromDecision(item, assumptions.minimumNetArb);
  const distance = net === null ? null : (trigger - net) * 100;
  const riskFlags = [
    ...item.rejection_reasons,
    item.execution_risk_band ? `risk_${item.execution_risk_band}` : null,
  ].filter((value): value is string => Boolean(value));

  return {
    id: item.record_id,
    provenance: "LIVE_PAPER",
    eventLabel: `${item.home_team} v ${item.away_team}`,
    competition: item.competition,
    marketLabel: marketLabel(item),
    settlement: settlementLabel(item),
    venues: item.venues,
    netArb: net,
    trigger,
    distanceToTriggerPp: distance,
    movement: null,
    capitalRequiredGbp: number(item.executable_stake_gbp),
    expectedLock: null,
    quoteFreshness: null,
    executableDepth: item.executable_stake_gbp != null ? `£${Number(item.executable_stake_gbp).toFixed(0)}` : null,
    limitingLeg: null,
    riskFlags,
    currencies: currenciesFromDecision(item),
    status: statusForScan(item, net, trigger),
    executable: item.eligible_for_paper_simulation,
    scannedAt: item.scanned_at,
    guaranteedProfitGbp: number(item.guaranteed_profit_gbp),
    executionRisk: item.execution_risk_score != null
      ? `${item.execution_risk_score}${item.execution_risk_band ? ` · ${item.execution_risk_band}` : ""}`
      : null,
  };
}

export function buildNearArbWatchlist(
  scans: PaperScanRecord[],
  assumptions: ScannerAssumptions,
): ArbitrageOpportunity[] {
  return latestByMarket(scans)
    .filter((item) => isNearArbCandidate(item, triggerFromDecision(item, assumptions.minimumNetArb)))
    .map((item) => opportunityFromScan(item, assumptions))
    .sort((a, b) => (a.distanceToTriggerPp ?? 99) - (b.distanceToTriggerPp ?? 99))
    .slice(0, 10);
}

export function buildExecutableOpportunities(
  scans: PaperScanRecord[],
  assumptions: ScannerAssumptions,
): ArbitrageOpportunity[] {
  return latestByMarket(scans)
    .filter((item) => item.eligible_for_paper_simulation)
    .map((item) => opportunityFromScan(item, assumptions));
}

export function buildActivityFeed(scans: PaperScanRecord[]): ActivityEvent[] {
  return scans.slice(0, 40).map((item) => {
    const event = `${item.home_team} v ${item.away_team}`;
    if (item.eligible_for_paper_simulation) {
      return {
        id: `${item.record_id}-triggered`,
        provenance: "LIVE_PAPER" as const,
        at: item.scanned_at,
        kind: "THRESHOLD_CROSSED" as const,
        title: "Threshold crossed",
        detail: `${event} · ${marketLabel(item)} · paper-eligible`,
      };
    }
    if (item.rejection_reasons.includes("net_edge_below_threshold")) {
      return {
        id: `${item.record_id}-watch`,
        provenance: "LIVE_PAPER" as const,
        at: item.scanned_at,
        kind: "WATCHLIST_ENTERED" as const,
        title: "Entered near-arb watchlist",
        detail: `${event} · ${marketLabel(item)} · below configured trigger`,
      };
    }
    if (item.rejection_reasons.length) {
      const reason = item.rejection_reasons[0].replaceAll("_", " ");
      const kindDetail = item.rejection_reasons.some((entry) => entry.includes("missing_") || entry.includes("stale"))
        ? item.rejection_reasons.join(" · ")
        : reason;
      return {
        id: `${item.record_id}-reject`,
        provenance: "LIVE_PAPER" as const,
        at: item.scanned_at,
        kind: "REJECTED" as const,
        title: "Rejected",
        detail: `${event} · ${kindDetail}`,
      };
    }
    return {
      id: `${item.record_id}-scan`,
      provenance: "LIVE_PAPER" as const,
      at: item.scanned_at,
      kind: "WATCHLIST_ENTERED" as const,
      title: "Scan recorded",
      detail: `${event} · ${marketLabel(item)}`,
    };
  });
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
      ? `${summary.eligible_count} paper-eligible scan${summary.eligible_count === 1 ? "" : "s"} in the current window. Realised P&L is not persisted.`
      : "Realised P&L is not persisted on the paper scan read model.",
  };
  return { live, fixture: DEMO_CAPITAL };
}

export function mergeActivityFeed(live: ActivityEvent[]): { items: ActivityEvent[]; usedFixture: boolean } {
  if (live.length === 0) {
    return { items: DEMO_ACTIVITY, usedFixture: true };
  }
  const fillKinds = new Set<ActivityKind>([
    "PAPER_FILL_ATTEMPTED",
    "PARTIAL_FILL",
    "PAPER_POSITION_COMPLETED",
    "SETTLED",
    "VOIDED",
    "CLOSED",
    "REALISED_PNL",
  ]);
  const hasFillLifecycle = live.some((item) => fillKinds.has(item.kind));
  if (hasFillLifecycle) return { items: live, usedFixture: false };
  return {
    items: [...live.slice(0, 12), ...DEMO_ACTIVITY],
    usedFixture: true,
  };
}
