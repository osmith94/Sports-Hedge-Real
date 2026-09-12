import { ActivityFeed } from "../components/activity-feed";
import { CapitalSummary } from "../components/capital-summary";
import { LiquidityPools } from "../components/liquidity-pools";
import { OpportunityCard } from "../components/opportunity-card";
import { RunPaperScan } from "../components/run-paper-scan";
import { PriorityAlertsSeam } from "../components/arbitrage/priority-alerts/priority-alerts-seam";
import { ExternalLegWorkflow } from "../components/arbitrage/priority-alerts/external-leg-workflow";
import { getPriorityAlert } from "../lib/priority-alerts/provider";
import { getPaperScans, getPaperScanSummary, PaperScanRecord } from "../lib/api";
import {
  DEFAULT_SCANNER_ASSUMPTIONS,
  DEMO_EXECUTABLE,
  DEMO_LIQUIDITY_POOLS,
  DEMO_MANUAL_EXTERNAL,
  DEMO_NEAR_ARB,
  buildActivityFeed,
  buildCapitalSnapshot,
  buildExecutableOpportunities,
  buildNearArbWatchlist,
  marketLabel,
  mergeActivityFeed,
} from "../lib/arbitrage-ops";
import { money, percent } from "../lib/format";

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

  try {
    [scans, summary] = await Promise.all([getPaperScans("limit=100"), getPaperScanSummary()]);
  } catch {
    apiAvailable = false;
  }

  const assumptions = DEFAULT_SCANNER_ASSUMPTIONS;
  const liveWatchlist = apiAvailable ? buildNearArbWatchlist(scans, assumptions) : [];
  const liveExecutable = apiAvailable ? buildExecutableOpportunities(scans, assumptions) : [];
  const watchlist = apiAvailable ? liveWatchlist : DEMO_NEAR_ARB;
  const executable = apiAvailable ? liveExecutable : DEMO_EXECUTABLE;
  const activity = mergeActivityFeed(apiAvailable ? buildActivityFeed(scans) : []);
  const capital = buildCapitalSnapshot(apiAvailable ? scans : [], summary, assumptions);
  const externalAlert = getPriorityAlert("pa-ncl-ars-2026-04-12-mr");

  const metrics = [
    { label: "Scans today", value: summary ? String(summary.scan_count) : "—", foot: "Matched market scan decisions", demo: false },
    { label: "Arbitrage detected", value: summary ? String(summary.arbitrage_count) : "—", foot: "Before final paper eligibility gates", demo: false },
    { label: "Paper eligible", value: summary ? String(summary.eligible_count) : "—", foot: "Passed mapping, costs and risk", positive: true, demo: false },
    { label: "Top net edge", value: summary ? percent(summary.top_net_edge) : "—", foot: "Best persisted scan in today’s window", positive: true, demo: false },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Arbitrage operations</div>
          <h1>Paper arbitrage operations console</h1>
          <p className="page-subtitle">
            Near-threshold watchlist, paper-eligible triggers, activity and native-currency capital. Live
            paper scans stay fail-closed; DEMO/FIXTURE is used only where the backend has no field yet.
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
      <PriorityAlertsSeam />

      <section className="ops-section">
        <div className="section-label">
          <span>1 · Near-arb watchlist</span>
          <span className={liveWatchlist.length ? "status-badge" : "demo-chip"}>
            {liveWatchlist.length ? "LIVE PAPER · NOT EXECUTABLE" : "DEMO / FIXTURE · NO LIVE NEAR-ARB"}
          </span>
        </div>
        <p className="section-copy">
          Closest matched markets below the configured net-arb trigger. These are not called arbitrage until
          settlement, payoff and cost checks pass.
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
          <span>2 · Triggered / executable</span>
          <span className={liveExecutable.length ? "status-badge" : apiAvailable ? "status-badge" : "demo-chip"}>
            {liveExecutable.length ? "LIVE PAPER ELIGIBLE" : apiAvailable ? "NO TRIGGERS" : "DEMO / FIXTURE"}
          </span>
        </div>
        {apiAvailable && liveExecutable.length === 0 ? (
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
          <span>3 · Manual-external paper state</span>
          <span className="demo-chip">DEMO / FIXTURE · NOT AUTO-POOL</span>
        </div>
        <p className="section-copy">
          Distinct from Near-Arb and validated paper arbs. MANUAL_EXTERNAL does not consume standing liquidity.
        </p>
        <OpportunityCard item={DEMO_MANUAL_EXTERNAL} />
        {externalAlert ? <ExternalLegWorkflow alert={externalAlert} /> : null}
      </section>

      <section className="ops-section grid-2">
        <ActivityFeed items={activity.items} usedFixture={activity.usedFixture} />
        <div className="stack-gap">
          <CapitalSummary live={capital.live} fixture={capital.fixture} />
        </div>
      </section>

      <div className="ops-section">
        <LiquidityPools pools={DEMO_LIQUIDITY_POOLS} />
      </div>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Paper scan history</div>
            <div className="panel-meta">
              Accepted and rejected matched-market decisions, newest first
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
                {scans.map((item) => (
                  <tr key={item.record_id}>
                    <td className="row-title">{item.home_team} v {item.away_team}</td>
                    <td>{marketLabel(item)}</td>
                    <td className="muted">{item.venues.join(" / ")}</td>
                    <td>{percent(item.gross_edge)}</td>
                    <td className={item.net_edge !== null && item.net_edge !== undefined ? "edge" : ""}>{percent(item.net_edge)}</td>
                    <td>{money(item.executable_stake_gbp)}</td>
                    <td className={item.guaranteed_profit_gbp !== null && item.guaranteed_profit_gbp !== undefined ? "edge" : ""}>{money(item.guaranteed_profit_gbp)}</td>
                    <td className={item.execution_risk_band === "low" ? "risk-low" : "risk-medium"}>
                      {item.execution_risk_score ?? "—"}{item.execution_risk_band ? ` · ${item.execution_risk_band}` : ""}
                    </td>
                    <td>{(item.mapping_confidence * 100).toFixed(1)}%</td>
                    <td>{statusText(item)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </>
  );
}
