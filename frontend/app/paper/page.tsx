import { paperPositions } from "../../lib/mock-data";

export default function PaperPortfolioPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Paper Portfolio</div>
          <h1>Simulated capital and locked payoff</h1>
          <p className="page-subtitle">
            Track paper bankroll, capital lock, guaranteed payoff and capital velocity before live execution is ever considered.
          </p>
        </div>
        <div className="demo-label">PAPER ONLY</div>
      </div>

      <section className="metric-grid">
        <div className="metric-card"><div className="metric-label">Paper bankroll</div><div className="metric-value">£15,000</div><div className="metric-foot">GBP functional currency</div></div>
        <div className="metric-card"><div className="metric-label">Capital deployed</div><div className="metric-value">£1,000</div><div className="metric-foot">6.7% utilization</div></div>
        <div className="metric-card"><div className="metric-label">Locked profit</div><div className="metric-value metric-positive">£15.82</div><div className="metric-foot">Across open paper positions</div></div>
        <div className="metric-card"><div className="metric-label">Capital velocity</div><div className="metric-value">0.31%/h</div><div className="metric-foot">Illustrative current portfolio</div></div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div><div className="panel-title">Open paper positions</div><div className="panel-meta">No real wagers have been placed</div></div>
          <span className="status-badge">SIMULATED</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Event</th><th>Structure</th><th>Capital</th><th>Locked profit</th><th>Return</th><th>Expected lock</th><th>Status</th></tr></thead>
            <tbody>
              {paperPositions.map((position) => (
                <tr key={position.event}>
                  <td className="row-title">{position.event}</td>
                  <td>{position.strategy}</td>
                  <td>{position.capital}</td>
                  <td className="edge">{position.lockedProfit}</td>
                  <td>{position.return}</td>
                  <td>{position.lock}</td>
                  <td><span className="status-badge">{position.status.toUpperCase()}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="grid-equal">
        <div className="panel"><div className="panel-header"><div className="panel-title">Hold vs rotate</div></div><div className="panel-body"><div className="empty-live">Future paper logic will compare settlement value with the cost of unwinding now and redeploying released capital into a stronger opportunity.</div></div></div>
        <div className="panel"><div className="panel-header"><div className="panel-title">Live execution</div></div><div className="panel-body"><div className="empty-live">Disabled. Phase 1 contains no bet-placement route. Live mode will remain a separate gated engineering phase after realistic paper validation.</div></div></div>
      </section>
    </>
  );
}
