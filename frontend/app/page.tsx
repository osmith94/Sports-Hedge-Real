import { ActivityFeed } from "../components/activity-feed";
import { CapitalSummary } from "../components/capital-summary";
import { FixtureDiscoverySection } from "../components/fixture-discovery-section";
import { HotFixturesPanel } from "../components/hot-fixtures-panel";
import { LiquidityPools } from "../components/liquidity-pools";
import { GenerateMatchingReport } from "../components/generate-matching-report";
import { OpportunityMonitor } from "../components/opportunity-monitor";
import { PaperTradeBook } from "../components/paper-trade-book";
import { RunPaperScan } from "../components/run-paper-scan";
import { PaperScanHistoryTable } from "../components/paper-scan-history-table";
import { ScanCycleHistoryPanel } from "../components/scan-cycle-history-panel";
import { PriorityAlertsSeam } from "../components/arbitrage/priority-alerts/priority-alerts-seam";
import {
  getActivePaperTrades,
  getLivePriorityAlerts,
  getLiveRefreshStatus,
  getPaperLiquidityPools,
  getPaperScanCycles,
  getPaperScanSummary,
  getPaperScans,
  getPaperTradeSummary,
  getPaperTreasury,
  getTrackedWatchlist,
  getWatchlistActivity,
  LiveRefreshStatus,
  NearOpportunity,
  OpportunityLifecycleEvent,
  PaperLiquiditySnapshot,
  PaperScanCycleRecord,
  PaperScanRecord,
  PaperTrade,
  PaperTradeBookSummary,
  PaperTreasurySnapshot,
} from "../lib/api";
import { DEFAULT_SCANNER_ASSUMPTIONS, buildCapitalSnapshot } from "../lib/arbitrage-ops";
import { activityFromWatchlist } from "../lib/watchlist";

export const dynamic = "force-dynamic";

async function settledValue<T>(promise: Promise<T>, fallback: T): Promise<{ value: T; available: boolean }> {
  try {
    return { value: await promise, available: true };
  } catch {
    return { value: fallback, available: false };
  }
}

export default async function ArbitragePage() {
  let scans: PaperScanRecord[] = [];
  let scanCycles: PaperScanCycleRecord[] = [];
  let scanCyclesAvailable = true;
  let summary: Awaited<ReturnType<typeof getPaperScanSummary>> | null = null;
  let apiAvailable = true;
  let livePriorityAvailable = true;
  let livePriorityCount = 0;
  let liveRefresh: LiveRefreshStatus | null = null;
  let liveRefreshAvailable = true;
  let liquidity: PaperLiquiditySnapshot | null = null;
  let liquidityAvailable = true;
  let treasury: PaperTreasurySnapshot | null = null;
  let treasuryAvailable = true;
  let tradeSummary: PaperTradeBookSummary | null = null;
  let activeTrades: PaperTrade[] = [];
  let tradesAvailable = true;

  try {
    // Latest-100 paper scan *audit* window. Not FixtureCurrentStateStore radar.
    [scans, summary] = await Promise.all([getPaperScans("limit=100"), getPaperScanSummary()]);
  } catch {
    apiAvailable = false;
  }

  try {
    scanCycles = await getPaperScanCycles("limit=100");
  } catch {
    scanCyclesAvailable = false;
  }

  const tracked = await settledValue(getTrackedWatchlist("limit=100"), [] as NearOpportunity[]);
  const activityFetch = await settledValue(
    getWatchlistActivity("limit=100&operator_signal=true"),
    [] as OpportunityLifecycleEvent[],
  );

  try {
    liveRefresh = await getLiveRefreshStatus();
  } catch {
    liveRefreshAvailable = false;
  }

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

  const assumptions = DEFAULT_SCANNER_ASSUMPTIONS;
  const liveConnected = apiAvailable && liveRefreshAvailable;
  const activity = {
    items: activityFetch.available ? activityFromWatchlist(activityFetch.value) : [],
    usedFixture: false,
  };
  const capital = buildCapitalSnapshot(apiAvailable ? scans : [], summary, assumptions);
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Arbitrage operations</div>
          <h1>Operations console</h1>
          <p className="page-subtitle">
            Paper treasury, venue feeds, live pairwise discovery and positions. One operator surface.
          </p>
        </div>
        <div className="heading-actions">
          <div className="demo-label">
            {apiAvailable
              ? liveConnected
                ? "LIVE PAPER READ MODEL"
                : "LIVE PAPER · DISCOVERY STATUS DEGRADED"
              : "PAPER API OFFLINE"}
          </div>
        </div>
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

      <FixtureDiscoverySection status={liveRefresh} available={liveRefreshAvailable} />

      <HotFixturesPanel status={liveRefresh} available={liveRefreshAvailable} />

      <OpportunityMonitor
        items={tracked.available ? tracked.value : []}
        available={tracked.available}
        liveRefresh={liveRefresh}
        liveRefreshAvailable={liveRefreshAvailable}
      />

      <ScanCycleHistoryPanel
        cycles={
          liveRefreshAvailable && liveRefresh
            ? (liveRefresh.recent_scan_cycles ?? scanCycles)
            : scanCycles
        }
        available={scanCyclesAvailable || liveRefreshAvailable}
      />

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
          <CapitalSummary live={capital.live} tradeSummary={tradeSummary} />
        </div>
      </section>

      <details className="audit-disclosure">
        <summary className="audit-summary">
          <span className="discovery-chevron" aria-hidden="true" />
          <span className="audit-summary-copy">
            Activity / market-decision audit · latest 100 append-only observations. Not
            current scanner radar. Scan-cycle history is a separate surface.
          </span>
          <span className={apiAvailable ? "status-badge" : "demo-chip"}>
            {apiAvailable ? "LATEST 100 AUDIT" : "NO API CONNECTION"}
          </span>
          <span className="discovery-toggle discovery-toggle-show">Show audit</span>
          <span className="discovery-toggle discovery-toggle-hide">Hide audit</span>
        </summary>
        <section className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Scan audit history</div>
              <div className="panel-meta">
                Latest 100 audit observations · newest first. Not current scanner radar
                state. Age uses each row&apos;s scanned_at. Sorting applies to this loaded
                window only, not the full audit store.
                {apiAvailable && scans.length > 0
                  ? ` Showing ${scans.length} loaded row${scans.length === 1 ? "" : "s"}.`
                  : ""}
              </div>
            </div>
            <span className="status-badge">HISTORICAL</span>
          </div>
          {apiAvailable && scans.length === 0 ? (
            <div className="empty-live-compact">No paper scans yet.</div>
          ) : null}
          {!apiAvailable ? (
            <div className="empty-live-compact">Paper API unreachable. No fabricated scan history.</div>
          ) : null}
          {scans.length > 0 ? <PaperScanHistoryTable scans={scans} /> : null}
        </section>
      </details>

      <GenerateMatchingReport />
    </>
  );
}
