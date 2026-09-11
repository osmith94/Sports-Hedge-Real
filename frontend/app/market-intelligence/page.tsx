import { MarketReactionChart } from "../../components/market-reaction-chart";
import { movementPoints, reactions } from "../../lib/mock-data";

export default function MarketIntelligencePage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Market Intelligence</div>
          <h1>Event-driven market movement</h1>
          <p className="page-subtitle">
            Track unusual price, spread and liquidity movement around team sheets, cards, goals, injuries and news.
          </p>
        </div>
        <div className="demo-label">ILLUSTRATIVE PAPER DATA</div>
      </div>

      <section className="metric-grid">
        <div className="metric-card"><div className="metric-label">Peak move</div><div className="metric-value metric-positive">+7.4pp</div><div className="metric-foot">Home implied probability</div></div>
        <div className="metric-card"><div className="metric-label">30m retracement</div><div className="metric-value">42%</div><div className="metric-foot">From post-team-sheet peak</div></div>
        <div className="metric-card"><div className="metric-label">Movement percentile</div><div className="metric-value">94th</div><div className="metric-foot">Comparable pre-match cohort</div></div>
        <div className="metric-card"><div className="metric-label">Cohort sample</div><div className="metric-value">N=38</div><div className="metric-foot">Sufficient · stability high</div></div>
      </section>

      <section className="grid-2">
        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Newcastle United v Arsenal · Match result</div>
              <div className="panel-meta">Implied probability · venue history · T=0 aligned to team sheet</div>
            </div>
            <span className="status-badge">TEAM SHEET</span>
          </div>
          <div className="panel-body">
            <MarketReactionChart points={movementPoints} eventIndex={4} eventLabel="Starting XI announced" />
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Cross-market reaction</div>
              <div className="panel-meta">First response after annotation</div>
            </div>
          </div>
          <div className="panel-body reaction-list">
            {reactions.map((reaction) => (
              <div className="reaction-row" key={reaction.market}>
                <div className="reaction-top">
                  <div className="reaction-name">{reaction.market}</div>
                  <div className="reaction-time">{reaction.firstResponse}</div>
                </div>
                <div className="reaction-meta">
                  <span>peak {reaction.peak}</span>
                  <span>retrace {reaction.retracement}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="grid-equal">
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Event timeline</div><div className="panel-meta">Temporal context, not causation</div></div>
          <div className="panel-body reaction-list">
            <div className="reaction-row"><div className="reaction-top"><div className="reaction-name">17:45 · Team sheet</div><div className="reaction-time">T=0</div></div><div className="reaction-meta"><span>Club source</span><span>confidence 100%</span></div></div>
            <div className="reaction-row"><div className="reaction-top"><div className="reaction-name">17:47 · Match price accelerates</div><div className="reaction-time">+2m</div></div><div className="reaction-meta"><span>+4.7pp</span><span>depth rising</span></div></div>
            <div className="reaction-row"><div className="reaction-top"><div className="reaction-name">17:49 · Corners begins repricing</div><div className="reaction-time">+4m</div></div><div className="reaction-meta"><span>+1.8pp</span><span>spread narrows</span></div></div>
          </div>
        </div>
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Research interpretation</div><div className="panel-meta">Paper-only signal context</div></div>
          <div className="panel-body">
            <div className="empty-live">
              Match-result repricing is unusually large for the selected cohort. Related markets are reacting at different speeds, but this view does not assume that lag implies executable value. The paper engine will later test spread, depth, slippage and persistence before surfacing any actionable research signal.
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
