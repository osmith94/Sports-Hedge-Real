import { ActivityFeed } from "../components/activity-feed";
import { CapitalSummary } from "../components/capital-summary";
import { DiscoveredFixturesPanel } from "../components/discovered-fixtures";
import { LiquidityPools } from "../components/liquidity-pools";
import { OpportunityCard } from "../components/opportunity-card";
import { RunPaperScan } from "../components/run-paper-scan";
import { TrackedMarketsBoard } from "../components/tracked-markets";
import { PriorityAlertsSeam } from "../components/arbitrage/priority-alerts/priority-alerts-seam";
import { ExternalLegWorkflow } from "../components/arbitrage/priority-alerts/external-leg-workflow";
import { getPriorityAlert } from "../lib/priority-alerts/provider";
import {
  getLivePriorityAlerts,
  getLiveRefreshStatus,
  getNearWatchlist,
  getPaperLiquidityPools,
  getPaperScans,
  getPaperScanSummary,
  getTrackedWatchlist,
  getTriggeredWatchlist,
  getWatchlistActivity,
  LiveRefreshStatus,
  NearOpportunity,
  OpportunityLifecycleEvent,
  PaperLiquiditySnapshot,
  PaperScanRecord,
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

export default async function ArbitragePage() {
  let scans: PaperScanRecord[] = [];
  let summary: Awaited<ReturnType<typeof getPaperScanSummary>> | null = null;
  let apiAvailable = true;
  let watchlistAvailable = true;
  let livePriorityAvailable = true;
  let livePriorityCount = 0;
  let nearRows: NearOpportunity[] = [];
  let triggeredRows: NearOpportunity[] = [];
  let trackedRows: NearOpportunity[] = [];
  let activityRows: OpportunityLifecycleEvent[] = [];
  let liveRefresh: LiveRefreshStatus | null = null;
  let liveRefreshAvailable = true;
  let liquidity: PaperLiquiditySnapshot | null = null;
  let liquidityAvailable = true;

  try {
    [scans, summary] = await Promise.all([getPaperScans("limit=100"), getPaperScanSummary()]);
  } catch {
    apiAvailable = false;
  }

  try {
    [nearRows, triggeredRows, trackedRows, activityRows] = await Promise.all([
      getNearWatchlist("limit=25"),
      getTriggeredWatchlist("limit=25"),
      getTrackedWatchlist("limit=100"),
      getWatchlistActivity("limit=100"),
    ]);
  } catch {
    watchlistAvailable = false;
  }

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

  const assumptions = DEFAULT_SCANNER_ASSUMPTIONS;
  const liveWatchlist = watchlistAvailable ? nearOpportunitiesFromWatchlist(nearRows) : [];
  const liveExecutable = watchlistAvailable ? triggeredOpportunitiesFromWatchlist(triggeredRows) : [];
  const liveTracked = watchlistAvailable ? trackedOpportunitiesFromWatchlist(trackedRows) : [];
  const liveConnected = apiAvailable && liveRefreshAvailable;
  const activity = {
    items: watchlistAvailable ? activityFromWatchlist(activityRows) : [],
    usedFixture: false,
  };
  const capital = buildCapitalSnapshot(apiAvailable ? scans : [], summary, assumptions);
  const externalAlert = getPriorityAlert("pa-ncl-ars-2026-04-12-mr");

  const metrics = [
    { label: "Tracked markets", value: watchlistAvailable ? String(liveTracked.length) : "—", foot: "Paper watchlist", positive: false },
    { label: "Near-arb", value: watchlistAvailable ? String(liveWatchlist.length) : "—", foot: "Below trigger", positive: false },
    { label: "Triggered", value: watchlistAvailable ? String(liveExecutable.length) : "—", foot: "Solver-validated", positive: true },
    { label: "Top net edge", value: summary ? percent(summary.top_net_edge) : "—", foot: "Today’s scan window", positive: true },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Arbitrage operations</div>
          <h1>Operations console</h1>
          <p className="page-subtitle">
            Live paper scan, discovery, watchlist and native standing capital. PAPER MODE · no execution.
          </p>
        </div>
        <div className="heading-actions">
          <div className="demo-label">{apiAvailable ? "LIVE PAPER READ MODEL" : "PAPER API OFFLINE"}</div>
          <a className="pool-link" href="#demo-walkthrough">Demo walkthrough · not live operations</a>
        </div>
      </div>

      <section className="metric-grid">
        {metrics.map((metric) => (
          <div className="metric-card" key={metric.label}>
            <div className="metric-label">{metric.label}</div>
            <div className={`metric-value ${metric.positive ? "metric-positive" : ""}`}>{metric.value}</div>
            <div className="metric-foot">{metric.foot}</div>
          </div>
        ))}
      </section>

      <RunPaperScan />
      <PriorityAlertsSeam liveAvailable={livePriorityAvailable} liveCount={livePriorityCount} />

      <section className="ops-section">
        <div className="section-label">
          <span>Fixture discovery</span>
          <span className={liveRefreshAvailable ? "status-badge" : "demo-chip"}>
            {liveRefreshAvailable ? "LIVE PAPER · MATCHBOOK PRIMARY" : "DISCOVERY STATUS UNAVAILABLE"}
          </span>
        </div>
        <DiscoveredFixturesPanel status={liveRefresh} available={liveRefreshAvailable} />
      </section>

      <section className={`ops-section ${liveTracked.length ? "" : "ops-section-compact"}`}>
        <div className="section-label">
          <span>Tracked</span>
          <span className={watchlistAvailable ? "status-badge" : "demo-chip"}>
            {watchlistAvailable
              ? liveTracked.length
                ? "LIVE WATCHLIST · TRACKED"
                : "EMPTY"
              : "WATCHLIST UNAVAILABLE"}
          </span>
        </div>
        {liveTracked.length ? (
          <p className="section-copy">Backend net margin, trigger and distance. Empty stays empty.</p>
        ) : null}
        <TrackedMarketsBoard items={liveTracked} available={watchlistAvailable} />
      </section>

      <section className={`ops-section ${liveWatchlist.length ? "" : "ops-section-compact"}`}>
        <div className="section-label">
          <span>Near-arb</span>
          <span className={watchlistAvailable ? "status-badge" : "demo-chip"}>
            {watchlistAvailable
              ? liveWatchlist.length
                ? "NOT EXECUTABLE"
                : "EMPTY"
              : "WATCHLIST UNAVAILABLE"}
          </span>
        </div>
        {liveWatchlist.length ? (
          <p className="section-copy">Below backend trigger — not guaranteed arbitrage.</p>
        ) : null}
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
          <span className={watchlistAvailable ? "status-badge" : "demo-chip"}>
            {liveExecutable.length ? "TRIGGERED" : watchlistAvailable ? "EMPTY" : "UNAVAILABLE"}
          </span>
        </div>
        {liveExecutable.length ? (
          <p className="section-copy">Solver-validated complete-set only. Simulation only.</p>
        ) : null}
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

      <section className="ops-section ops-section-compact">
        <div className="section-label">
          <span>MANUAL_EXTERNAL</span>
          <span className={liveConnected && livePriorityCount === 0 ? "status-badge" : "demo-chip"}>
            {liveConnected ? (livePriorityCount ? "LIVE TICKETS" : "EMPTY") : "API OFFLINE"}
          </span>
        </div>
        <div className="empty-live-compact">
          {livePriorityCount
            ? `${livePriorityCount} live priority alert${livePriorityCount === 1 ? "" : "s"} — open Priority Alerts.`
            : "No live MANUAL_EXTERNAL ticket. Does not consume standing pools."}
        </div>
      </section>

      <section className="ops-section grid-2">
        <ActivityFeed items={activity.items} usedFixture={activity.usedFixture} />
        <div className="stack-gap">
          <CapitalSummary live={capital.live} />
        </div>
      </section>

      <div className="ops-section">
        <LiquidityPools snapshot={liquidity} available={liquidityAvailable} compact />
      </div>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Paper scan history</div>
            <div className="panel-meta">Newest matched-market decisions.</div>
          </div>
          <span className="status-badge">{apiAvailable ? "SCANNER DATA" : "NO API CONNECTION"}</span>
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
          Labelled DEMO / FIXTURE training content, including the Newcastle United v Arsenal MANUAL_EXTERNAL ticket.
          Not mixed into discovery, watchlist, P&amp;L or standing pools.
        </p>
        <OpportunityCard item={DEMO_MANUAL_EXTERNAL} />
        {externalAlert ? <ExternalLegWorkflow alert={externalAlert} /> : null}
      </details>
    </>
  );
}
