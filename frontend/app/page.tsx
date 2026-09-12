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
  getPaperScans,
  getPaperScanSummary,
  getTrackedWatchlist,
  getTriggeredWatchlist,
  getWatchlistActivity,
  LiveRefreshStatus,
  NearOpportunity,
  OpportunityLifecycleEvent,
  PaperScanRecord,
} from "../lib/api";
import {
  DEFAULT_SCANNER_ASSUMPTIONS,
  DEMO_ACTIVITY,
  DEMO_EXECUTABLE,
  DEMO_LIQUIDITY_POOLS,
  DEMO_MANUAL_EXTERNAL,
  DEMO_NEAR_ARB,
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

  const assumptions = DEFAULT_SCANNER_ASSUMPTIONS;
  const liveWatchlist = watchlistAvailable ? nearOpportunitiesFromWatchlist(nearRows) : [];
  const liveExecutable = watchlistAvailable ? triggeredOpportunitiesFromWatchlist(triggeredRows) : [];
  const liveTracked = watchlistAvailable ? trackedOpportunitiesFromWatchlist(trackedRows) : [];
  const liveConnected = apiAvailable && liveRefreshAvailable;
  const watchlist = watchlistAvailable ? liveWatchlist : DEMO_NEAR_ARB;
  const executable = watchlistAvailable ? liveExecutable : DEMO_EXECUTABLE;
  const activity = watchlistAvailable
    ? { items: activityFromWatchlist(activityRows), usedFixture: false }
    : { items: DEMO_ACTIVITY, usedFixture: true };
  const capital = buildCapitalSnapshot(apiAvailable ? scans : [], summary, assumptions);
  const externalAlert = getPriorityAlert("pa-ncl-ars-2026-04-12-mr");

  const metrics = [
    { label: "Tracked markets", value: watchlistAvailable ? String(liveTracked.length) : "—", foot: "Canonical markets on the paper watchlist", demo: false },
    { label: "Near-arb candidates", value: watchlistAvailable ? String(liveWatchlist.length) : "—", foot: "WATCHING / APPROACHING below backend trigger", demo: false },
    { label: "Triggered paper arbs", value: watchlistAvailable ? String(liveExecutable.length) : "—", foot: "Solver-validated complete-set only", positive: true, demo: false },
    { label: "Top net edge", value: summary ? percent(summary.top_net_edge) : "—", foot: "Best persisted scan in today’s window", positive: true, demo: false },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Arbitrage operations</div>
          <h1>Paper arbitrage operations console</h1>
          <p className="page-subtitle">
            Tracked markets, near-threshold watchlist, paper-eligible triggers, activity and native-currency capital.
            Matchbook is the live fixture-discovery source for Premier League, EFL Championship and La Liga only.
            Polymarket is matched onto the same canonical event when public coverage exists. Watchlist rows come
            from `/paper/watchlist/tracked`, `/near`, `/triggered` and `/activity`. The browser does not reclassify
            rejected scans or recompute arb economics. DEMO/FIXTURE walkthrough content is kept in a separate
            labelled section when the live console is connected.
          </p>
        </div>
        <div className="demo-label">{apiAvailable ? "LIVE PAPER READ MODEL" : "PAPER API OFFLINE"}</div>
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
          <span>0 · Matchbook fixture discovery</span>
          <span className={liveRefreshAvailable ? "status-badge" : "demo-chip"}>
            {liveRefreshAvailable ? "LIVE PAPER · MATCHBOOK PRIMARY" : "DISCOVERY STATUS UNAVAILABLE"}
          </span>
        </div>
        <DiscoveredFixturesPanel status={liveRefresh} available={liveRefreshAvailable} />
      </section>

      <section className="ops-section">
        <div className="section-label">
          <span>1 · Tracked markets</span>
          <span className={watchlistAvailable ? "status-badge" : "demo-chip"}>
            {watchlistAvailable
              ? liveTracked.length
                ? "LIVE WATCHLIST · TRACKED"
                : "LIVE WATCHLIST · EMPTY"
              : "WATCHLIST UNAVAILABLE"}
          </span>
        </div>
        <p className="section-copy">
          Canonical fixtures/markets currently on the paper watchlist. Current net margin, configured backend trigger
          and distance to trigger are backend-calculated and refresh as repeated read-only collections persist new
          observations. Negative net margin stays visible as below break-even. Source, last updated and quote age at
          last evaluation are shown per row; ages are not ticked on screen between refreshes. Strike narrative is the
          observed distance sequence only — not a causal claim. Collection → alerts → paper fills → ledger is not wired.
        </p>
        <TrackedMarketsBoard items={liveTracked} available={watchlistAvailable} />
      </section>

      <section className="ops-section">
        <div className="section-label">
          <span>2 · Near-arb watchlist</span>
          <span className={watchlistAvailable ? "status-badge" : "demo-chip"}>
            {watchlistAvailable
              ? liveWatchlist.length
                ? "LIVE WATCHLIST · NOT EXECUTABLE"
                : "LIVE WATCHLIST · EMPTY"
              : "DEMO / FIXTURE · WATCHLIST UNAVAILABLE"}
          </span>
        </div>
        <p className="section-copy">
          WATCHING / APPROACHING candidates have already passed semantics, costs, FX, depth, freshness and risk gates.
          They remain below the configured backend trigger and are not guaranteed arbitrage until the strict
          trigger/solver condition is met.
        </p>
        {watchlist.length === 0 ? (
          <div className="empty-live">No near-threshold opportunities in the current scan window.</div>
        ) : (
          <div className="opp-stack">
            {watchlist.map((item) => (
              <OpportunityCard item={item} key={item.id} />
            ))}
          </div>
        )}
      </section>

      <section className="ops-section">
        <div className="section-label">
          <span>3 · Triggered / executable</span>
          <span className={watchlistAvailable ? "status-badge" : "demo-chip"}>
            {liveExecutable.length ? "LIVE WATCHLIST · TRIGGERED" : watchlistAvailable ? "NO TRIGGERS" : "DEMO / FIXTURE"}
          </span>
        </div>
        <p className="section-copy">
          Guaranteed profit is shown only for solver-validated triggered complete-set opportunities. Paper fill
          lifecycle remains simulation-only.
        </p>
        {watchlistAvailable && liveExecutable.length === 0 ? (
          <div className="empty-live">
            No paper-eligible opportunities in the current scan window. The monitor does not invent triggered
            arbitrage.
          </div>
        ) : (
          <div className="opp-stack">
            {executable.map((item) => (
              <OpportunityCard item={item} executable key={item.id} />
            ))}
          </div>
        )}
      </section>

      <section className="ops-section">
        <div className="section-label">
          <span>4 · Manual-external paper state</span>
          <span className={liveConnected ? "status-badge" : "demo-chip"}>
            {liveConnected ? "LIVE PAPER · EMPTY UNLESS BACKEND HAS A TICKET" : "DEMO / FIXTURE"}
          </span>
        </div>
        <p className="section-copy">
          Distinct from Near-Arb and validated paper arbs. MANUAL_EXTERNAL does not consume standing liquidity.
          Live Priority Alert rows stay empty when the backend has none. The Newcastle–Arsenal walkthrough is
          not shown here when the live console is connected.
        </p>
        {liveConnected ? (
          <div className="empty-live">
            No live MANUAL_EXTERNAL ticket in this operations path. Open the demo walkthrough section below for the
            labelled fictional example.
          </div>
        ) : (
          <OpportunityCard item={DEMO_MANUAL_EXTERNAL} />
        )}
        {!liveConnected && externalAlert ? <ExternalLegWorkflow alert={externalAlert} /> : null}
      </section>

      <section className="ops-section grid-2">
        <ActivityFeed items={activity.items} usedFixture={activity.usedFixture} />
        <div className="stack-gap">
          <CapitalSummary live={capital.live} fixture={capital.fixture} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Paper scan history</div>
            <div className="panel-meta">
              Accepted and rejected matched-market decisions, newest first. Scanner audit, not a second near-arb classifier.
            </div>
          </div>
          <span className="status-badge">{apiAvailable ? "SCANNER DATA" : "NO API CONNECTION"}</span>
        </div>

        {apiAvailable && scans.length === 0 ? (
          <div className="empty-live">
            No paper scans have been recorded yet. Run a read-only collection cycle to populate this monitor.
          </div>
        ) : null}

        {!apiAvailable ? (
          <div className="empty-live">
            The FastAPI service is not reachable. The dashboard is showing no fabricated fallback opportunities
            for live scan history.
          </div>
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

      <section className="ops-section">
        <div className="section-label">
          <span>Demo walkthrough · not live operations</span>
          <span className="demo-chip">DEMO / FIXTURE · NOT LIVE DISCOVERY</span>
        </div>
        <p className="section-copy">
          Fictional operator-training content, including the Newcastle United v Arsenal MANUAL_EXTERNAL ticket.
          It is not mixed into Matchbook fixture discovery, tracked markets, near-arb or triggered lists when the
          live paper API is connected.
        </p>
        <OpportunityCard item={DEMO_MANUAL_EXTERNAL} />
        {externalAlert ? <ExternalLegWorkflow alert={externalAlert} /> : null}
        <LiquidityPools pools={DEMO_LIQUIDITY_POOLS} />
      </section>
    </>
  );
}
