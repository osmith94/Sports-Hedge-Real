import { opportunities } from "../lib/mock-data";

const metrics = [
  { label: "Paper bankroll", value: "£15,000", foot: "Across simulated venue balances" },
  { label: "Locked paper profit", value: "£22.28", foot: "Open matched positions", positive: true },
  { label: "Capital deployed", value: "£1,910", foot: "12.7% paper utilization" },
  { label: "Opportunities today", value: "37", foot: "9 passed executable filters" },
];

export default function ArbitragePage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Arbitrage</div>
          <h1>Cross-venue opportunity monitor</h1>
          <p className="page-subtitle">
            Compare executable football prices after depth, fees, FX and simulated execution risk.
          </p>
        </div>
        <div className="demo-label">ILLUSTRATIVE PAPER DATA</div>
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

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Paper opportunities</div>
            <div className="panel-meta">Ranked by executable net edge and capital efficiency</div>
          </div>
          <span className="status-badge">SCANNER READY</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Event</th><th>Market</th><th>Venues</th><th>Gross edge</th><th>Net edge</th>
                <th>Executable</th><th>Guaranteed profit</th><th>Risk</th><th>Mapping</th>
              </tr>
            </thead>
            <tbody>
              {opportunities.map((item) => (
                <tr key={`${item.event}-${item.market}`}>
                  <td className="row-title">{item.event}</td>
                  <td>{item.market}</td>
                  <td className="muted">{item.venues}</td>
                  <td>{item.grossEdge}</td>
                  <td className="edge">{item.netEdge}</td>
                  <td>{item.executable}</td>
                  <td className="edge">{item.profit}</td>
                  <td className={item.risk === "Low" ? "risk-low" : "risk-medium"}>{item.risk}</td>
                  <td>{item.confidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
