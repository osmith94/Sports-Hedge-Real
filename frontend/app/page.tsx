import { RunPaperScan } from "../components/run-paper-scan";
import { getPaperScans, getPaperScanSummary, PaperScanRecord } from "../lib/api";

export const dynamic = "force-dynamic";

function number(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function percent(value: string | number | null | undefined): string {
  const parsed = number(value);
  return parsed === null ? "—" : `${(parsed * 100).toFixed(2)}%`;
}

function money(value: string | number | null | undefined): string {
  const parsed = number(value);
  return parsed === null
    ? "—"
    : new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }).format(parsed);
}

function marketLabel(item: PaperScanRecord): string {
  const family = item.market_family.replaceAll("_", " ");
  const line = item.line === null || item.line === undefined ? "" : ` ${item.line}`;
  return `${family}${line}`;
}

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

  const metrics = [
    { label: "Scans today", value: summary ? String(summary.scan_count) : "—", foot: "Matched market scan decisions" },
    { label: "Arbitrage detected", value: summary ? String(summary.arbitrage_count) : "—", foot: "Before final paper eligibility gates" },
    { label: "Paper eligible", value: summary ? String(summary.eligible_count) : "—", foot: "Passed mapping, costs and risk", positive: true },
    { label: "Top net edge", value: summary ? percent(summary.top_net_edge) : "—", foot: "Best persisted scan in today’s window", positive: true },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Arbitrage</div>
          <h1>Cross-venue opportunity monitor</h1>
          <p className="page-subtitle">
            Persisted paper scans after canonical matching, executable depth, fee assumptions, FX and execution-risk checks.
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
            The FastAPI service is not reachable. The dashboard is showing no fabricated fallback opportunities.
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
