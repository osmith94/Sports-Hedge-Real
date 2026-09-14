import { ActivityFeed } from "../components/activity-feed";
import { CapitalSummary } from "../components/capital-summary";
import { FixtureDiscoverySection } from "../components/fixture-discovery-section";
import { LiquidityPools } from "../components/liquidity-pools";
import { OpportunityCard } from "../components/opportunity-card";
import { PaperTradeBook } from "../components/paper-trade-book";
import { RunPaperScan } from "../components/run-paper-scan";
import { TrackedMarketsBoard } from "../components/tracked-markets";
import { PriorityAlertsSeam } from "../components/arbitrage/priority-alerts/priority-alerts-seam";
import { ExternalLegWorkflow } from "../components/arbitrage/priority-alerts/external-leg-workflow";
import { getPriorityAlert } from "../lib/priority-alerts/provider";
import {
  getActivePaperTrades,
  getLivePriorityAlerts,
  getLiveRefreshStatus,
  getNearWatchlist,
  getPaperLiquidityPools,
  getPaperScanSummary,
  getPaperScans,
  getPaperTradeSummary,
  getPaperTreasury,
  getTrackedWatchlist,
  getTriggeredWatchlist,
  getWatchlistActivity,
  LiveRefreshStatus,
  NearOpportunity,
  OpportunityLifecycleEvent,
  PaperLiquiditySnapshot,
  PaperScanRecord,
  PaperTrade,
  PaperTradeBookSummary,
  PaperTreasurySnapshot,
} from "../lib/api";
import {
  DEFAULT_SCANNER_ASSUMPTIONS,
  DEMO_MANUAL_EXTERNAL,
  buildCapitalSnapshot,
  marketLabel,
} from "../lib/arbitrage-ops";
import { money, percent } from "../lib/format";
import {
  activityFromWatchlist,
  nearOpportunitiesFromWatchlist,
  trackedOpportunitiesFromWatchlist,
  triggeredOpportunitiesFromWatchlist,
} from "../lib/watchlist";

export const dynamic = "force-dynamic";

function statusText(item: PaperScanRecord): string {
  if (item.eligible_for_paper_simulation) return "Paper eligible";
  if (item.rejection_reasons.length) return item.rejection_reasons.join(", ").replaceAll("_", " ");
  return item.is_arbitrage ? "Filtered" : "No arbitrage";
}

async function settledValue<T>(promise: Promise<T>, fallback: T): Promise<{ value: T; available: boolean }> {
  try {
    return { value: await promise, available: true };
  } catch {
    return { value: fallback, available: false };
  }
}

export default async function ArbitragePage() {
  let scans: PaperScanRecord[] = [];
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

  const near = await settledValue(getNearWatchlist("limit=25"), [] as NearOpportunity[]);
  const triggered = await settledValue(getTriggeredWatchlist("limit=25"), [] as NearOpportunity[]);
  const tracked = await settledValue(getTrackedWatchlist("limit=100"), [] as NearOpportunity[]);
  const activityFetch = await settledValue(
    getWatchlistActivity("limit=100"),
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
  const liveWatchlist = near.available ? nearOpportunitiesFromWatchlist(near.value) : [];
  const liveExecutable = triggered.available ? triggeredOpportunitiesFromWatchlist(triggered.value) : [];
  const liveTracked = tracked.available ? trackedOpportunitiesFromWatchlist(tracked.value) : [];
  const liveConnected = apiAvailable && liveRefreshAvailable;
  const activity = {
    items: activityFetch.available ? activityFromWatchlist(activityFetch.value) : [],
    usedFixture: false,
  };
  const capital = buildCapitalSnapshot(apiAvailable ? scans : [], summary, assumptions);
  const externalAlert = getPriorityAlert("pa-ncl-ars-2026-04-12-mr");

  const metrics = [
    {
      label: "Tracked",
      value: tracked.available ? String(liveTracked.length) : "—",
      foot: tracked.available ? (liveTracked.length ? "Paper watchlist" : "0 / No tracked markets yet") : "Unavailable",
      positive: false,
    },
    {
      label: "Near arb",
      value: near.available ? String(liveWatchlist.length) : "—",
      foot: "Below trigger",
      positive: false,
    },
    {
      label: "Triggered",
      value: triggered.available ? String(liveExecutable.length) : "—",
      foot: "Solver-validated",
      positive: true,
    },
    {
      label: "Top net edge",
      value: summary ? percent(summary.top_net_edge) : "—",
      foot: "Today’s scan window",
      positive: true,
    },
  ];

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

      <RunPaperScan />

      <section className="metric-grid">
        {metrics.map((metric) => (
          <div className="metric-card" key={metric.label}>
            <div className="metric-label">{metric.label}</div>
            <div className={`metric-value ${metric.positive ? "metric-positive" : ""}`}>{metric.value}</div>
            <div className="metric-foot">{metric.foot}</div>
          </div>
        ))}
      </section>

      <PriorityAlertsSeam liveAvailable={livePriorityAvailable} liveCount={livePriorityCount} />

      <FixtureDiscoverySection status={liveRefresh} available={liveRefreshAvailable} />

      <section className={`ops-section ${liveTracked.length ? "" : "ops-section-compact"}`}>
        <div className="section-label">
          <span>Tracked</span>
          <span className={tracked.available ? "status-badge" : "demo-chip"}>
            {tracked.available ? (liveTracked.length ? "LIVE WATCHLIST · TRACKED" : "EMPTY") : "WATCHLIST UNAVAILABLE"}
          </span>
        </div>
        <TrackedMarketsBoard items={liveTracked} available={tracked.available} />
      </section>

      <section className={`ops-section ${liveWatchlist.length ? "" : "ops-section-compact"}`}>
        <div className="section-label">
          <span>Near-arb</span>
          <span className={near.available ? "status-badge" : "demo-chip"}>
            {near.available ? (liveWatchlist.length ? "NOT EXECUTABLE" : "EMPTY") : "WATCHLIST UNAVAILABLE"}
          </span>
        </div>
        {liveWatchlist.length === 0 ? (
          <div className="empty-live-compact">No near-threshold candidates.</div>
        ) : (
          <div className="opp-stack">
            {liveWatchlist.map((item) => (
              <OpportunityCard item={item} key={item.id} />
            ))}
          </div>
        )}
      </section>

      <section className={`ops-section ${liveExecutable.length ? "" : "ops-section-compact"}`}>
        <div className="section-label">
          <span>Triggered</span>
          <span className={liveExecutable.length ? "status-badge" : triggered.available ? "status-badge" : "demo-chip"}>
            {liveExecutable.length ? "TRIGGERED" : triggered.available ? "EMPTY" : "UNAVAILABLE"}
          </span>
        </div>
        {liveExecutable.length === 0 ? (
          <div className="empty-live-compact">No triggered paper arbs.</div>
        ) : (
          <div className="opp-stack">
            {liveExecutable.map((item) => (
              <OpportunityCard item={item} executable key={item.id} />
            ))}
          </div>
        )}
      </section>

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

      <section className="ops-section grid-2">
        <ActivityFeed items={activity.items} usedFixture={activity.usedFixture} />
        <div className="stack-gap">
          <CapitalSummary live={capital.live} tradeSummary={tradeSummary} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Paper scan history</div>
            <div className="panel-meta">
              Latest 100 audit observations · newest first. Not current scanner radar
              state.
              {apiAvailable && scans.length > 0
                ? ` Showing ${scans.length} loaded row${scans.length === 1 ? "" : "s"}.`
                : ""}
            </div>
          </div>
          <span className="status-badge">
            {apiAvailable ? "LATEST 100 AUDIT" : "NO API CONNECTION"}
          </span>
        </div>
        {apiAvailable && scans.length === 0 ? (
          <div className="empty-live-compact">No paper scans yet.</div>
        ) : null}
        {!apiAvailable ? (
          <div className="empty-live-compact">Paper API unreachable. No fabricated scan history.</div>
        ) : null}
        {scans.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Event</th><th>Market</th><th>Venues</th><th>Gross edge</th><th>Net edge</th>
                  <th>Executable</th><th>Guaranteed profit</th><th>Risk</th><th>Mapping</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {scans.map((item) => {
                  const net = item.net_edge == null ? null : Number(item.net_edge);
                  return (
                  <tr key={item.record_id}>
                    <td className="row-title">{item.home_team} v {item.away_team}</td>
                    <td>{marketLabel(item)}</td>
                    <td className="muted">{item.venues.join(" / ")}</td>
                    <td>{percent(item.gross_edge)}</td>
                    <td className={net !== null && net < 0 ? "edge-negative" : net !== null ? "edge" : ""}>
                      {percent(item.net_edge)}
                      {net !== null && net < 0 ? " · below break-even" : ""}
                    </td>
                    <td>{money(item.executable_stake_gbp)}</td>
                    <td className={item.guaranteed_profit_gbp !== null && item.guaranteed_profit_gbp !== undefined ? "edge" : ""}>{money(item.guaranteed_profit_gbp)}</td>
                    <td className={item.execution_risk_band === "low" ? "risk-low" : "risk-medium"}>
                      {item.execution_risk_score ?? "—"}{item.execution_risk_band ? ` · ${item.execution_risk_band}` : ""}
                    </td>
                    <td>{(item.mapping_confidence * 100).toFixed(1)}%</td>
                    <td>{statusText(item)}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <details className="demo-walkthrough" id="demo-walkthrough">
        <summary>Demo walkthrough · not live operations</summary>
        <p className="section-copy">
          Advanced / test fixture replay only. Labelled DEMO / FIXTURE training content, including the
          Newcastle United v Arsenal MANUAL_EXTERNAL ticket. Not mixed into discovery, watchlist,
          P&amp;L or standing pools. Open{" "}
          <a className="pool-link" href="/demo">/demo</a> for the labelled replay utility.
        </p>
        <OpportunityCard item={DEMO_MANUAL_EXTERNAL} />
        {externalAlert ? <ExternalLegWorkflow alert={externalAlert} /> : null}
      </details>
    </>
  );
}
