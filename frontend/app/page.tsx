import { ActivityFeed } from "../components/activity-feed";
import { CapitalSummary } from "../components/capital-summary";
import { FixtureDiscoverySection } from "../components/fixture-discovery-section";
import { HotFixturesPanel } from "../components/hot-fixtures-panel";
import { StreamPanel } from "../components/stream-panel";
import { LiquidityPools } from "../components/liquidity-pools";
import { GenerateMatchingReport } from "../components/generate-matching-report";
import { OpportunityMonitor } from "../components/opportunity-monitor";
import { PaperTradeBook } from "../components/paper-trade-book";
import { RunPaperScan } from "../components/run-paper-scan";
import { ScanCycleHistoryPanel } from "../components/scan-cycle-history-panel";
import { PriorityAlertsSeam } from "../components/arbitrage/priority-alerts/priority-alerts-seam";
import { OperationsConsoleIntro, SimulationOnlyChip } from "../components/runtime-mode-provider";
import {
  getActivePaperTrades,
  getLivePriorityAlerts,
  getPaperLiquidityPools,
  getPaperTradeSummary,
  getPaperTreasury,
  getTrackedWatchlist,
  getWatchlistActivity,
  getPrice2ActivityAttempts,
  opportunityMonitorTrackedQuery,
  NearOpportunity,
  OpportunityLifecycleEvent,
  Price2ActivityObservation,
  PaperLiquiditySnapshot,
  PaperTrade,
  PaperTradeBookSummary,
  PaperTreasurySnapshot,
} from "../lib/api";
import { CapitalSnapshot } from "../lib/arbitrage-ops";
import {
  activityFromWatchlist,
  mergeOperatorActivity,
  oldestVisibleOccurredAt,
  price2ActivityQuery,
  visibleOpportunityIds,
} from "../lib/watchlist";

export const dynamic = "force-dynamic";

async function settledValue<T>(promise: Promise<T>, fallback: T): Promise<{ value: T; available: boolean }> {
  try {
    return { value: await promise, available: true };
  } catch {
    return { value: fallback, available: false };
  }
}

const CAPITAL_FROM_TRADES: CapitalSnapshot = {
  provenance: "LIVE_PAPER",
  realisedPnlGbp: null,
  lockedCapitalGbp: null,
  availableCapitalGbp: null,
  todayPnlGbp: null,
  mtdPnlGbp: null,
  allTimePnlGbp: null,
  note: "Rendered capital cards use the paper trade book. Scan-audit rows are not loaded on this page.",
};

export default async function ArbitragePage() {
  let livePriorityAvailable = true;
  let livePriorityCount = 0;
  let liquidity: PaperLiquiditySnapshot | null = null;
  let liquidityAvailable = true;
  let treasury: PaperTreasurySnapshot | null = null;
  let treasuryAvailable = true;
  let tradeSummary: PaperTradeBookSummary | null = null;
  let activeTrades: PaperTrade[] = [];
  let tradesAvailable = true;

  const tracked = await settledValue(
    getTrackedWatchlist(opportunityMonitorTrackedQuery()),
    [] as NearOpportunity[],
  );
  const activityFetch = await settledValue(
    getWatchlistActivity("limit=100&operator_signal=true"),
    [] as OpportunityLifecycleEvent[],
  );
  const opportunityIds = activityFetch.available
    ? visibleOpportunityIds(activityFetch.value)
    : [];
  const oldest = activityFetch.available
    ? oldestVisibleOccurredAt(activityFetch.value)
    : null;
  const price2Fetch = await settledValue(
    getPrice2ActivityAttempts(price2ActivityQuery(opportunityIds, oldest)),
    [] as Price2ActivityObservation[],
  );

  try {
    livePriorityCount = (await getLivePriorityAlerts()).length;
  } catch {
    livePriorityAvailable = false;
  }

  try {
    liquidity = await getPaperLiquidityPools();
  } catch {
    liquidityAvailable = false;
  }

  try {
    treasury = await getPaperTreasury();
  } catch {
    treasuryAvailable = false;
  }

  try {
    [tradeSummary, activeTrades] = await Promise.all([getPaperTradeSummary(), getActivePaperTrades()]);
  } catch {
    tradesAvailable = false;
  }

  const liveConnected = tracked.available || tradesAvailable || liquidityAvailable;
  const activity = {
    items: activityFetch.available
      ? mergeOperatorActivity(
          activityFromWatchlist(activityFetch.value),
          price2Fetch.available ? price2Fetch.value : [],
        )
      : [],
    usedFixture: false,
  };
  return (
    <>
      <div className="page-heading">
        <OperationsConsoleIntro liveConnected={liveConnected} />
      </div>

      <LiquidityPools
        snapshot={liquidity}
        available={liquidityAvailable}
        treasury={treasury}
        treasuryAvailable={treasuryAvailable}
        compact
      />

      <section className="ops-section">
        <div className="section-label">
          <span>Open paper positions</span>
          <SimulationOnlyChip />
          <span className={tradesAvailable ? "status-badge" : "demo-chip"}>
            {tradesAvailable ? (activeTrades.length ? "LIVE PAPER" : "EMPTY") : "UNAVAILABLE"}
          </span>
        </div>
        <PaperTradeBook
          summary={tradeSummary}
          active={activeTrades}
          closed={[]}
          apiAvailable={tradesAvailable}
          compact
        />
      </section>

      <RunPaperScan />

      <PriorityAlertsSeam liveAvailable={livePriorityAvailable} liveCount={livePriorityCount} />

      <FixtureDiscoverySection />

      <HotFixturesPanel />

      <StreamPanel />

      <OpportunityMonitor
        items={tracked.available ? tracked.value : []}
        available={tracked.available}
      />

      <ScanCycleHistoryPanel />

      {livePriorityCount > 0 ? (
        <section className="ops-section ops-section-compact">
          <div className="section-label">
            <span>MANUAL_EXTERNAL</span>
            <span className="demo-chip">LIVE TICKETS</span>
          </div>
          <div className="empty-live-compact">
            {livePriorityCount} live priority alert{livePriorityCount === 1 ? "" : "s"} — open Priority Alerts.
          </div>
        </section>
      ) : null}

      <section className="ops-section grid-2">
        <ActivityFeed items={activity.items} usedFixture={activity.usedFixture} />
        <div className="stack-gap">
          <CapitalSummary live={CAPITAL_FROM_TRADES} tradeSummary={tradeSummary} />
        </div>
      </section>

      <GenerateMatchingReport />
    </>
  );
}
